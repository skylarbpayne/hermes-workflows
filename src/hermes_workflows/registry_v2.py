from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .registry_location import (
    RegistryLocationV1,
    RelativeDbPathV1,
    resolve_registry_location,
    resolve_relative_db_path,
)


REGISTRY_IDENTITY_SERVICE_ID = "registry.identity"
REGISTRY_IDENTITY_CONTRACT_VERSION = 1

_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_WORKFLOW_REF_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)
_ROOT_FIELDS = frozenset({"schema_version", "state_root", "dbs", "workflows", "runner"})
_LEGACY_ROOT_FIELDS = frozenset({"schema_version", "dbs", "workflows"})
_WORKFLOW_REQUIRED_FIELDS = frozenset({"workflow_ref", "db"})
_WORKFLOW_OPTIONAL_FIELDS = frozenset(
    {
        "title",
        "description",
        "tags",
        "default_input",
        "trusted_resume",
        "kanban_policy",
        "dashboard_policy",
        "defaults_overlay",
    }
)
_MAX_REGISTRY_BYTES = 1_048_576
_MAX_DEFAULT_INPUT_BYTES = 65_536
_MAX_ERROR_BYTES = 4096
_MAX_ERROR_MESSAGE_BYTES = 256
_MAX_CONSUMERS = 32
_MAX_COLLECTION_ITEMS = 10_000
_MAX_JSON_DEPTH = 32


class RegistryContractError(Exception):
    """Bounded, redacted configuration error suitable for doctor-style exit 2."""

    exit_code = 2

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fields: Mapping[str, object] | None = None,
        conflict_id: str | None = None,
    ) -> None:
        if _ID_PATTERN.fullmatch(code) is None:
            raise ValueError("registry error code must be a canonical id")
        bounded_message = _bounded_text(message, _MAX_ERROR_MESSAGE_BYTES)
        normalized_fields = _bounded_error_fields(fields or {})
        if conflict_id is not None and _ID_PATTERN.fullmatch(conflict_id) is None:
            raise ValueError("registry conflict_id must be a canonical id or None")
        self.code = code
        self.message = bounded_message
        self.fields = normalized_fields
        self.conflict_id = conflict_id
        super().__init__(bounded_message)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "fields": dict(self.fields),
            "conflict_id": self.conflict_id,
        }
        if len(_canonical_json(payload).encode("utf-8")) > _MAX_ERROR_BYTES:
            payload["fields"] = {}
        return payload


@dataclass(frozen=True)
class RegistryDbV2:
    alias: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path}


@dataclass(frozen=True)
class RegistryWorkflowV2:
    alias: str
    workflow_ref: str
    db: str
    title: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    default_input: Mapping[str, Any] = MappingProxyType({})
    trusted_resume: bool = False
    kanban_policy: str = "comment"
    dashboard_policy: str = "receipt"
    defaults_overlay: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "workflow_ref": self.workflow_ref,
            "db": self.db,
        }
        if self.title is not None:
            payload["title"] = self.title
        if self.description is not None:
            payload["description"] = self.description
        if self.tags:
            payload["tags"] = list(self.tags)
        if self.default_input:
            payload["default_input"] = _thaw_json(self.default_input)
        if self.trusted_resume:
            payload["trusted_resume"] = True
        if self.kanban_policy != "comment":
            payload["kanban_policy"] = self.kanban_policy
        if self.dashboard_policy != "receipt":
            payload["dashboard_policy"] = self.dashboard_policy
        if self.defaults_overlay is not None:
            payload["defaults_overlay"] = self.defaults_overlay
        return payload


@dataclass(frozen=True)
class RegistryRunnerV2:
    dbs: tuple[str, ...]
    lease_seconds: int

    def to_dict(self) -> dict[str, object]:
        return {"dbs": list(self.dbs), "lease_seconds": self.lease_seconds}


@dataclass(frozen=True)
class RegistryCatalogV2:
    state_root: str
    dbs: Mapping[str, RegistryDbV2]
    workflows: Mapping[str, RegistryWorkflowV2]
    runner: RegistryRunnerV2
    schema_version: int = 2

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state_root": self.state_root,
            "dbs": {alias: db.to_dict() for alias, db in sorted(self.dbs.items())},
            "workflows": {
                alias: workflow.to_dict() for alias, workflow in sorted(self.workflows.items())
            },
            "runner": self.runner.to_dict(),
        }

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(encode_registry_v2(self).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def resolve_db(self, registry_path: str | Path, db_alias: str) -> "ResolvedRegistryDbV2":
        alias = _require_public_alias(db_alias)
        db = self.dbs.get(alias)
        if db is None:
            raise RegistryContractError(
                "registry_unknown_alias",
                "the requested DB alias is not present in the registry",
                fields={"db_alias": alias},
            )
        try:
            canonical_registry = _canonical_registry_path(registry_path)
            location = resolve_registry_location(
                canonical_registry.parent,
                RegistryLocationV1(
                    registry_file=canonical_registry.name,
                    state_root=self.state_root,
                ),
            )
            if Path(location.registry_path) != canonical_registry:
                raise ValueError("registry location identity mismatch")
            resolved_db = Path(
                resolve_relative_db_path(
                    location,
                    RelativeDbPathV1(alias=alias, path=db.path),
                )
            )
        except RegistryContractError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RegistryContractError(
                "registry_path_invalid",
                "registry state resolves outside its canonical registry-relative root",
            ) from exc
        return ResolvedRegistryDbV2(
            registry_path=canonical_registry,
            state_root=Path(location.state_root_path),
            db_path=resolved_db,
            db_alias=alias,
        )


@dataclass(frozen=True)
class LoadedRegistry:
    catalog: RegistryCatalogV2
    source_schema_version: int


@dataclass(frozen=True)
class ResolvedRegistryDbV2:
    """Private internal receipt. Public consumers use RegistryIdentityV1 instead."""

    registry_path: Path
    state_root: Path
    db_path: Path
    db_alias: str


@dataclass(frozen=True)
class RegistryIdentityV1:
    schema_version: int
    registry_fingerprint: str
    registry_identity: str
    db_alias: str
    resolved_db_identity: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "registry_fingerprint": self.registry_fingerprint,
            "registry_identity": self.registry_identity,
            "db_alias": self.db_alias,
            "resolved_db_identity": self.resolved_db_identity,
        }


@dataclass(frozen=True)
class RegistryMigrationPlanV1:
    catalog: RegistryCatalogV2
    source_schema_version: int = 1
    target_schema_version: int = 2
    schema_version: int = 1
    would_write: bool = False

    @property
    def canonical_target_json(self) -> str:
        return encode_registry_v2(self.catalog)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_schema_version": self.source_schema_version,
            "target_schema_version": self.target_schema_version,
            "would_write": self.would_write,
            "target_fingerprint": self.catalog.fingerprint,
            "target_registry": self.catalog.to_dict(),
        }


class RegistryIdentityServiceV1:
    """One catalog-backed identity service for CLI, plugin, and supervisor adapters."""

    service_id = REGISTRY_IDENTITY_SERVICE_ID
    contract_version = REGISTRY_IDENTITY_CONTRACT_VERSION

    def __init__(
        self,
        registry_path: Path,
        catalog: RegistryCatalogV2,
        *,
        source_schema_version: int,
    ) -> None:
        self.registry_path = registry_path
        self.catalog = catalog
        self.source_schema_version = source_schema_version

    @classmethod
    def from_file(cls, registry_path: str | Path) -> "RegistryIdentityServiceV1":
        canonical_path, payload = _read_registry_file(registry_path)
        loaded = decode_registry(payload)
        return cls(
            canonical_path,
            loaded.catalog,
            source_schema_version=loaded.source_schema_version,
        )

    def resolve_db(self, db_alias: str) -> ResolvedRegistryDbV2:
        self._require_current_catalog()
        return self.catalog.resolve_db(self.registry_path, db_alias)

    def identity(self, db_alias: str) -> RegistryIdentityV1:
        resolved = self.resolve_db(db_alias)
        return RegistryIdentityV1(
            schema_version=1,
            registry_fingerprint=self.catalog.fingerprint,
            registry_identity=_identity_digest("registry", str(resolved.registry_path)),
            db_alias=resolved.db_alias,
            resolved_db_identity=_identity_digest(
                "db",
                self.catalog.fingerprint,
                resolved.db_alias,
                str(resolved.db_path),
            ),
        )

    def _require_current_catalog(self) -> None:
        _, payload = _read_registry_file(self.registry_path)
        current = decode_registry(payload)
        if (
            current.source_schema_version != self.source_schema_version
            or current.catalog.fingerprint != self.catalog.fingerprint
        ):
            raise RegistryContractError(
                "registry_drift",
                "the registry changed after the consumer identity service loaded it",
            )


def decode_registry(value: str | bytes | bytearray) -> LoadedRegistry:
    try:
        payload = _decode_json_object(value)
        schema_version = payload.get("schema_version")
        if schema_version is None or (type(schema_version) is int and schema_version == 1):
            return LoadedRegistry(catalog=_parse_legacy_registry(payload), source_schema_version=1)
        if type(schema_version) is int and schema_version == 2:
            return LoadedRegistry(catalog=_parse_registry_v2(payload), source_schema_version=2)
        raise ValueError("unsupported schema version")
    except RegistryContractError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise RegistryContractError(
            "registry_invalid",
            "registry does not satisfy the canonical registry-v2 contract",
        ) from exc


def load_registry_file(registry_path: str | Path) -> LoadedRegistry:
    _, payload = _read_registry_file(registry_path)
    return decode_registry(payload)


def encode_registry_v2(catalog: RegistryCatalogV2) -> str:
    if not isinstance(catalog, RegistryCatalogV2):
        raise TypeError("catalog must be RegistryCatalogV2")
    return _canonical_json(catalog.to_dict())


def dry_run_migrate_registry_file(registry_path: str | Path) -> RegistryMigrationPlanV1:
    loaded = load_registry_file(registry_path)
    if loaded.source_schema_version != 1:
        raise RegistryContractError(
            "registry_migration_not_required",
            "dry-run migration accepts a read-only registry-v1 source",
        )
    return RegistryMigrationPlanV1(catalog=loaded.catalog)


def require_consumer_parity(
    identities: Mapping[str, RegistryIdentityV1],
) -> RegistryIdentityV1:
    if not isinstance(identities, Mapping) or not identities:
        raise RegistryContractError(
            "registry_invalid_consumer",
            "consumer identities must be a nonempty mapping",
        )
    if len(identities) > _MAX_CONSUMERS:
        raise RegistryContractError(
            "registry_invalid_consumer",
            "consumer identity comparison exceeds its fixed bound",
        )
    validated: list[tuple[str, RegistryIdentityV1]] = []
    for consumer, identity in identities.items():
        if not isinstance(consumer, str) or _ID_PATTERN.fullmatch(consumer) is None:
            raise RegistryContractError(
                "registry_invalid_consumer",
                "consumer names must be bounded canonical ids",
            )
        if not isinstance(identity, RegistryIdentityV1):
            raise RegistryContractError(
                "registry_invalid_consumer",
                "consumer values must be registry identities",
            )
        validated.append((consumer, identity))
    validated.sort(key=lambda item: item[0])
    first = validated[0][1]
    if any(identity != first for _, identity in validated[1:]):
        raise RegistryContractError(
            "registry_drift",
            "registry consumers do not share one registry identity",
            fields={"consumers": [consumer for consumer, _ in validated]},
        )
    return first


def _parse_registry_v2(payload: Mapping[str, Any]) -> RegistryCatalogV2:
    _require_exact_fields(payload, required=_ROOT_FIELDS, optional=frozenset())
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 2:
        raise ValueError("schema_version must equal 2")
    state_root = RegistryLocationV1(
        registry_file="workflows.registry.json",
        state_root=payload["state_root"],
    ).state_root
    dbs = _parse_v2_dbs(payload["dbs"])
    workflows = _parse_workflows(payload["workflows"], dbs=dbs)
    runner = _parse_runner(payload["runner"], dbs=dbs)
    return RegistryCatalogV2(
        state_root=state_root,
        dbs=MappingProxyType(dbs),
        workflows=MappingProxyType(workflows),
        runner=runner,
    )


def _parse_legacy_registry(payload: Mapping[str, Any]) -> RegistryCatalogV2:
    _require_exact_fields(
        payload,
        required=frozenset({"dbs", "workflows"}),
        optional=frozenset({"schema_version"}),
    )
    if "schema_version" in payload and (
        type(payload["schema_version"]) is not int or payload["schema_version"] != 1
    ):
        raise ValueError("legacy schema_version must equal 1")
    raw_dbs = payload["dbs"]
    if not isinstance(raw_dbs, Mapping) or not raw_dbs:
        raise ValueError("legacy dbs must be a nonempty object")

    legacy_paths: dict[str, str] = {}
    for alias, raw_db in raw_dbs.items():
        _validate_alias(alias)
        if isinstance(raw_db, str):
            path = raw_db
        elif isinstance(raw_db, Mapping):
            _require_exact_fields(raw_db, required=frozenset({"path"}), optional=frozenset())
            path = raw_db["path"]
        else:
            raise TypeError("legacy DB entries must be strings or path objects")
        validated = RelativeDbPathV1(alias=alias, path=path).path
        legacy_paths[alias] = validated

    state_roots = {path.split("/", 1)[0] for path in legacy_paths.values() if "/" in path}
    if len(state_roots) != 1 or any("/" not in path for path in legacy_paths.values()):
        raise ValueError("legacy DB paths do not share one migration-safe state root")
    state_root = next(iter(state_roots))
    dbs = {
        alias: RegistryDbV2(alias=alias, path=path.split("/", 1)[1])
        for alias, path in sorted(legacy_paths.items())
    }
    workflows = _parse_workflows(payload["workflows"], dbs=dbs, legacy=True)
    runner = RegistryRunnerV2(dbs=tuple(sorted(dbs)), lease_seconds=30)
    return RegistryCatalogV2(
        state_root=state_root,
        dbs=MappingProxyType(dbs),
        workflows=MappingProxyType(workflows),
        runner=runner,
    )


def _parse_v2_dbs(value: object) -> dict[str, RegistryDbV2]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("dbs must be a nonempty object")
    dbs: dict[str, RegistryDbV2] = {}
    for alias, raw_db in value.items():
        _validate_alias(alias)
        if not isinstance(raw_db, Mapping):
            raise TypeError("registry-v2 DB entries must be objects")
        _require_exact_fields(raw_db, required=frozenset({"path"}), optional=frozenset())
        path = RelativeDbPathV1(alias=alias, path=raw_db["path"]).path
        dbs[alias] = RegistryDbV2(alias=alias, path=path)
    return dict(sorted(dbs.items()))


def _parse_workflows(
    value: object,
    *,
    dbs: Mapping[str, RegistryDbV2],
    legacy: bool = False,
) -> dict[str, RegistryWorkflowV2]:
    if not isinstance(value, Mapping):
        raise TypeError("workflows must be an object")
    workflows: dict[str, RegistryWorkflowV2] = {}
    for alias, raw_workflow in value.items():
        _validate_alias(alias)
        if not isinstance(raw_workflow, Mapping):
            raise TypeError("workflow entries must be objects")
        required = frozenset({"workflow_ref"}) if legacy and len(dbs) == 1 else _WORKFLOW_REQUIRED_FIELDS
        _require_exact_fields(
            raw_workflow,
            required=required,
            optional=_WORKFLOW_OPTIONAL_FIELDS | ({"db"} if legacy else frozenset()),
        )
        workflow_ref = raw_workflow["workflow_ref"]
        if not isinstance(workflow_ref, str) or _WORKFLOW_REF_PATTERN.fullmatch(workflow_ref) is None:
            raise ValueError("workflow_ref must use one importable module:symbol spelling")
        if "db" in raw_workflow:
            db_alias = raw_workflow["db"]
        else:
            db_alias = next(iter(dbs))
        if not isinstance(db_alias, str) or db_alias not in dbs:
            raise ValueError("workflow db must reference a configured alias")

        title = _optional_text(raw_workflow.get("title"), label="title", max_bytes=512)
        description = _optional_text(raw_workflow.get("description"), label="description", max_bytes=4096)
        tags = _parse_tags(raw_workflow.get("tags", []))
        default_input_raw = raw_workflow.get("default_input", {})
        if not isinstance(default_input_raw, Mapping):
            raise TypeError("default_input must be an object")
        normalized_default = _normalize_json(default_input_raw)
        if not isinstance(normalized_default, dict):
            raise TypeError("default_input must normalize to an object")
        if len(_canonical_json(normalized_default).encode("utf-8")) > _MAX_DEFAULT_INPUT_BYTES:
            raise ValueError("default_input exceeds its fixed bound")
        trusted_resume = raw_workflow.get("trusted_resume", False)
        if not isinstance(trusted_resume, bool):
            raise TypeError("trusted_resume must be a boolean")
        kanban_policy = _policy(raw_workflow.get("kanban_policy", "comment"), label="kanban_policy")
        dashboard_policy = _policy(raw_workflow.get("dashboard_policy", "receipt"), label="dashboard_policy")
        defaults_overlay = raw_workflow.get("defaults_overlay")
        if defaults_overlay is not None and defaults_overlay != "local":
            raise ValueError("defaults_overlay must equal local when present")

        workflows[alias] = RegistryWorkflowV2(
            alias=alias,
            workflow_ref=workflow_ref,
            db=db_alias,
            title=title,
            description=description,
            tags=tags,
            default_input=_freeze_json(normalized_default),
            trusted_resume=trusted_resume,
            kanban_policy=kanban_policy,
            dashboard_policy=dashboard_policy,
            defaults_overlay=defaults_overlay,
        )
    return dict(sorted(workflows.items()))


def _parse_runner(value: object, *, dbs: Mapping[str, RegistryDbV2]) -> RegistryRunnerV2:
    if not isinstance(value, Mapping):
        raise TypeError("runner must be an object")
    _require_exact_fields(
        value,
        required=frozenset({"dbs", "lease_seconds"}),
        optional=frozenset(),
    )
    raw_dbs = value["dbs"]
    if not isinstance(raw_dbs, list) or not raw_dbs:
        raise TypeError("runner dbs must be a nonempty list")
    if len(raw_dbs) != len(set(raw_dbs)):
        raise ValueError("runner db aliases must be unique")
    for alias in raw_dbs:
        if not isinstance(alias, str) or alias not in dbs:
            raise ValueError("runner dbs must reference configured aliases")
    lease_seconds = value["lease_seconds"]
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("runner lease_seconds must be an integer from 1 through 3600")
    return RegistryRunnerV2(dbs=tuple(sorted(raw_dbs)), lease_seconds=lease_seconds)


def _parse_tags(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError("tags must be a list")
    if len(value) > 32:
        raise ValueError("tags exceed their fixed bound")
    tags: list[str] = []
    for tag in value:
        if not isinstance(tag, str) or _ALIAS_PATTERN.fullmatch(tag) is None:
            raise ValueError("tags must be canonical ids")
        tags.append(tag)
    if len(tags) != len(set(tags)):
        raise ValueError("tags must be unique")
    return tuple(sorted(tags))


def _decode_json_object(value: str | bytes | bytearray) -> Mapping[str, Any]:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        text = value
    elif isinstance(value, (bytes, bytearray)):
        encoded = bytes(value)
        text = encoded.decode("utf-8", errors="strict")
    else:
        raise TypeError("registry JSON must be text or bytes")
    if len(encoded) > _MAX_REGISTRY_BYTES:
        raise ValueError("registry JSON exceeds its fixed bound")
    payload = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite_constant,
    )
    if not isinstance(payload, Mapping):
        raise TypeError("registry JSON must be an object")
    return payload


def _read_registry_file(registry_path: str | Path) -> tuple[Path, bytes]:
    try:
        canonical = _canonical_registry_path(registry_path)
        required_flags = ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK")
        if not all(hasattr(os, name) for name in required_flags):
            raise OSError("safe registry file primitives unavailable")
        before = canonical.lstat()
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(canonical, flags)
        try:
            descriptor_before = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_before.st_mode):
                raise OSError("registry is not a regular file")
            chunks: list[bytes] = []
            remaining = _MAX_REGISTRY_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            descriptor_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = canonical.lstat()
        if not (
            _stable_stat_identity(before)
            == _stable_stat_identity(descriptor_before)
            == _stable_stat_identity(descriptor_after)
            == _stable_stat_identity(after)
        ):
            raise OSError("registry file changed while being read")
        if len(payload) > _MAX_REGISTRY_BYTES:
            raise ValueError("registry exceeds its fixed bound")
        return canonical, payload
    except RegistryContractError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RegistryContractError(
            "registry_path_invalid",
            "registry_path must identify one stable canonical regular file",
        ) from exc


def _canonical_registry_path(registry_path: str | Path) -> Path:
    if not isinstance(registry_path, (str, Path)):
        raise TypeError("registry_path must be a path")
    raw = Path(registry_path)
    if ".." in raw.parts:
        raise ValueError("registry_path must not contain parent traversal")
    absolute = raw if raw.is_absolute() else Path.cwd() / raw
    if not absolute.exists() or absolute.is_symlink() or not absolute.is_file():
        raise ValueError("registry_path must identify a regular file")
    resolved = absolute.resolve(strict=True)
    if str(absolute) != str(resolved):
        raise ValueError("registry_path must be canonical and symlink-free")
    return resolved


def _stable_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _require_exact_fields(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
) -> None:
    actual = set(value)
    if not required <= actual or actual - required - optional:
        raise ValueError("object fields do not match the canonical registry schema")


def _validate_alias(value: object) -> str:
    if not isinstance(value, str) or _ALIAS_PATTERN.fullmatch(value) is None:
        raise ValueError("alias must be a bounded canonical id")
    return value


def _require_public_alias(value: object) -> str:
    if not isinstance(value, str) or _ALIAS_PATTERN.fullmatch(value) is None:
        raise RegistryContractError(
            "registry_alias_required",
            "public registry consumers require a configured DB alias",
        )
    return value


def _optional_text(value: object, *, label: str, max_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string or absent")
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds its fixed bound")
    return normalized


def _policy(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical id")
    return value


def _normalize_json(value: object, *, depth: int = 0) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON value exceeds its depth bound")
    if value is None or isinstance(value, bool) or type(value) is int:
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("JSON object exceeds its item bound")
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError("JSON keys collide after Unicode normalization")
            normalized[normalized_key] = _normalize_json(item, depth=depth + 1)
        return normalized
    if isinstance(value, list):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("JSON list exceeds its item bound")
        return [_normalize_json(item, depth=depth + 1) for item in value]
    raise TypeError("value is not JSON-compatible")


def _freeze_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("registry JSON contains a duplicate object key")
        value[key] = item
    return value


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError("registry JSON numbers must be finite")


def _identity_digest(kind: str, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"hermes-workflows:{kind}:v1\0".encode("utf-8"))
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _bounded_text(value: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise TypeError("bounded text must be a string")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def _bounded_error_fields(value: Mapping[str, object]) -> Mapping[str, object]:
    normalized = _normalize_json(value)
    if not isinstance(normalized, dict):
        return MappingProxyType({})
    encoded = _canonical_json(normalized).encode("utf-8")
    if len(encoded) > _MAX_ERROR_BYTES // 2:
        return MappingProxyType({})
    return MappingProxyType(normalized)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

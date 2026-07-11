from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_DRIVE_PREFIX_PATTERN = re.compile(r"^[A-Za-z]:")
_MAX_PATH_BYTES = 1024


@dataclass(frozen=True, init=False)
class RegistryLocationV1:
    schema_version: int
    registry_file: str
    state_root: str

    def __init__(self, registry_file: str, state_root: str, schema_version: int = 1) -> None:
        _validate_schema_version(schema_version)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "registry_file", _validate_relative_posix_path(registry_file, label="registry_file"))
        object.__setattr__(self, "state_root", _validate_relative_posix_path(state_root, label="state_root"))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "registry_file": self.registry_file,
            "state_root": self.state_root,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RegistryLocationV1":
        payload = _require_exact_object(
            value,
            fields={"schema_version", "registry_file", "state_root"},
            label="registry location",
        )
        return cls(
            schema_version=payload["schema_version"],
            registry_file=payload["registry_file"],
            state_root=payload["state_root"],
        )


@dataclass(frozen=True, init=False)
class RelativeDbPathV1:
    schema_version: int
    alias: str
    path: str

    def __init__(self, alias: str, path: str, schema_version: int = 1) -> None:
        _validate_schema_version(schema_version)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "alias", _validate_alias(alias))
        object.__setattr__(self, "path", _validate_relative_posix_path(path, label="path"))

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "alias": self.alias, "path": self.path}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RelativeDbPathV1":
        payload = _require_exact_object(
            value,
            fields={"schema_version", "alias", "path"},
            label="relative DB path",
        )
        return cls(
            schema_version=payload["schema_version"],
            alias=payload["alias"],
            path=payload["path"],
        )


@dataclass(frozen=True, init=False)
class ResolvedRegistryLocationV1:
    """Diagnostics-only receipt; callers must redact it from public surfaces."""

    schema_version: int
    registry_path: str
    registry_dir: str
    state_root_path: str

    def __init__(
        self,
        registry_path: str,
        registry_dir: str,
        state_root_path: str,
        schema_version: int = 1,
    ) -> None:
        _validate_schema_version(schema_version)
        normalized_registry_dir = _validate_normalized_absolute_path(registry_dir, label="registry_dir")
        normalized_registry_path = _validate_normalized_absolute_path(registry_path, label="registry_path")
        normalized_state_root = _validate_normalized_absolute_path(state_root_path, label="state_root_path")
        _require_contained(Path(normalized_registry_dir), Path(normalized_registry_path), label="registry_path")
        _require_contained(Path(normalized_registry_dir), Path(normalized_state_root), label="state_root_path")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "registry_path", normalized_registry_path)
        object.__setattr__(self, "registry_dir", normalized_registry_dir)
        object.__setattr__(self, "state_root_path", normalized_state_root)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "registry_path": self.registry_path,
            "registry_dir": self.registry_dir,
            "state_root_path": self.state_root_path,
        }


def encode_registry_location(value: RegistryLocationV1) -> str:
    if not isinstance(value, RegistryLocationV1):
        raise TypeError("value must be RegistryLocationV1")
    return _canonical_json(value.to_dict())


def decode_registry_location(value: str) -> RegistryLocationV1:
    return RegistryLocationV1.from_dict(_decode_json_object(value, label="registry location"))


def encode_relative_db_path(value: RelativeDbPathV1) -> str:
    if not isinstance(value, RelativeDbPathV1):
        raise TypeError("value must be RelativeDbPathV1")
    return _canonical_json(value.to_dict())


def decode_relative_db_path(value: str) -> RelativeDbPathV1:
    return RelativeDbPathV1.from_dict(_decode_json_object(value, label="relative DB path"))


def resolve_registry_location(
    config_root: str | Path,
    value: RegistryLocationV1,
) -> ResolvedRegistryLocationV1:
    if not isinstance(value, RegistryLocationV1):
        raise TypeError("value must be RegistryLocationV1")
    root = Path(config_root)
    if not root.is_absolute():
        raise ValueError("config_root must be absolute")
    resolved_root = root.resolve(strict=False)
    registry_path = _resolve_beneath(resolved_root, value.registry_file, label="registry_file")
    registry_dir = registry_path.parent.resolve(strict=False)
    state_root = _resolve_beneath(registry_dir, value.state_root, label="state_root")
    return ResolvedRegistryLocationV1(
        registry_path=str(registry_path),
        registry_dir=str(registry_dir),
        state_root_path=str(state_root),
    )


def resolve_relative_db_path(
    location: ResolvedRegistryLocationV1,
    value: RelativeDbPathV1,
) -> str:
    if not isinstance(location, ResolvedRegistryLocationV1):
        raise TypeError("location must be ResolvedRegistryLocationV1")
    if not isinstance(value, RelativeDbPathV1):
        raise TypeError("value must be RelativeDbPathV1")
    registry_dir = Path(_validate_normalized_absolute_path(location.registry_dir, label="registry_dir"))
    state_root = Path(_validate_normalized_absolute_path(location.state_root_path, label="state_root_path"))
    _require_contained(registry_dir, state_root, label="state_root_path")
    return str(_resolve_beneath(state_root, value.path, label=f"DB path for alias {value.alias!r}"))


def resolve_relative_db_paths(
    location: ResolvedRegistryLocationV1,
    values: Iterable[RelativeDbPathV1],
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for value in values:
        if not isinstance(value, RelativeDbPathV1):
            raise TypeError("values must contain only RelativeDbPathV1 objects")
        if value.alias in resolved:
            raise ValueError(f"duplicate alias: {value.alias}")
        resolved[value.alias] = resolve_relative_db_path(location, value)
    return resolved


def _validate_schema_version(value: object) -> None:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must equal 1")


def _validate_alias(value: object) -> str:
    if not isinstance(value, str) or _ALIAS_PATTERN.fullmatch(value) is None:
        raise ValueError(f"alias must match {_ALIAS_PATTERN.pattern}")
    return value


def _validate_relative_posix_path(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value or value.isspace():
        raise ValueError(f"{label} must be nonblank")
    if len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ValueError(f"{label} must be at most {_MAX_PATH_BYTES} UTF-8 bytes")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    if "\\" in value:
        raise ValueError(f"{label} must use POSIX '/' separators")
    if "~" in value:
        raise ValueError(f"{label} must not contain '~'")
    if value.startswith("/") or _DRIVE_PREFIX_PATTERN.match(value):
        raise ValueError(f"{label} must be relative and have no drive prefix")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{label} must not contain empty, '.', or '..' segments")
    return value


def _validate_normalized_absolute_path(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    resolved = path.resolve(strict=False)
    if str(path) != str(resolved):
        raise ValueError(f"{label} must be a normalized, symlink-resolved absolute path")
    return str(resolved)


def _resolve_beneath(parent: Path, relative_path: str, *, label: str) -> Path:
    candidate = (parent / relative_path).resolve(strict=False)
    _require_contained(parent, candidate, label=label)
    _require_contained(parent, candidate.resolve(strict=False), label=label)
    return candidate


def _require_contained(parent: Path, candidate: Path, *, label: str) -> None:
    try:
        candidate.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside its declared parent (symlink escape)") from exc


def _require_exact_object(
    value: Mapping[str, Any],
    *,
    fields: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    actual = set(value.keys())
    if actual != fields:
        raise ValueError(f"{label} fields must be exactly {sorted(fields)}")
    return value


def _decode_json_object(value: str, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise TypeError(f"encoded {label} must be a string")
    payload = json.loads(value, object_pairs_hook=_reject_duplicate_object_keys)
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must decode to an object")
    return payload


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key: {key}")
        value[key] = item
    return value


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

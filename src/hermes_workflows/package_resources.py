from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Tuple, Union


_SCHEMA_VERSION = 1
_DISTRIBUTION_NAME = "hermes-workflows"
_MANIFEST_RESOURCE = "plugin_payload_manifest.v1.json"
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FILE_FIELDS = frozenset({"schema_version", "path", "sha256", "size_bytes"})
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "owner_id",
        "package_name",
        "package_version",
        "payload_root",
        "files",
    }
)
PathLike = Union[str, Path]


def _validate_schema_version(value: object) -> None:
    if type(value) is not int or value != _SCHEMA_VERSION:
        raise ValueError("schema_version must equal 1")


def _validate_relative_posix_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a nonempty relative POSIX path")
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")
    if str(PurePosixPath(value)) != value:
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")
    return value


@dataclass(frozen=True)
class PackageResourceFileV1:
    schema_version: int
    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_relative_posix_path(self.path, "path")
        if not isinstance(self.sha256, str) or _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a nonnegative integer")

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class PackageResourceManifestV1:
    schema_version: int
    owner_id: str
    package_name: str
    package_version: str
    payload_root: str
    files: Tuple[PackageResourceFileV1, ...]

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        for field_name, value in (
            ("owner_id", self.owner_id),
            ("package_name", self.package_name),
        ):
            if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
                raise ValueError(f"{field_name} is not a valid package resource identifier")
        if not isinstance(self.package_version, str) or not self.package_version.strip():
            raise ValueError("package_version must be nonblank")
        if self.package_version != installed_package_version(self.package_name):
            raise ValueError("package_version must equal the installed distribution version")

        payload_root = _validate_relative_posix_path(self.payload_root, "payload_root")
        if not isinstance(self.files, tuple):
            raise ValueError("files must be a tuple")
        paths = []
        for entry in self.files:
            if not isinstance(entry, PackageResourceFileV1):
                raise ValueError("files must contain PackageResourceFileV1 entries")
            if not entry.path.startswith(payload_root + "/"):
                raise ValueError("every resource path must be beneath payload_root")
            paths.append(entry.path)
        if len(paths) != len(set(paths)):
            raise ValueError("resource paths must not contain duplicates")
        if paths != sorted(paths):
            raise ValueError("resource entries must be lexicographically sorted by path")

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "owner_id": self.owner_id,
            "package_name": self.package_name,
            "package_version": self.package_version,
            "payload_root": self.payload_root,
            "files": [entry.to_dict() for entry in self.files],
        }


def installed_package_version(package_name: str = _DISTRIBUTION_NAME) -> str:
    version = metadata.version(package_name)
    if not version.strip():
        raise ValueError("installed distribution version must be nonblank")
    return version


def canonical_manifest_json(manifest: PackageResourceManifestV1) -> str:
    if not isinstance(manifest, PackageResourceManifestV1):
        raise TypeError("manifest must be a PackageResourceManifestV1")
    return json.dumps(
        manifest.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset, kind: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{kind} fields must exactly match the version-1 schema")


def manifest_from_json(value: str) -> PackageResourceManifestV1:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    _require_exact_fields(payload, _MANIFEST_FIELDS, "manifest")
    raw_files = payload["files"]
    if not isinstance(raw_files, list):
        raise ValueError("manifest files must be a JSON array")

    entries = []
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict):
            raise ValueError("each manifest file must be a JSON object")
        _require_exact_fields(raw_entry, _FILE_FIELDS, "resource file")
        entries.append(
            PackageResourceFileV1(
                schema_version=raw_entry["schema_version"],
                path=raw_entry["path"],
                sha256=raw_entry["sha256"],
                size_bytes=raw_entry["size_bytes"],
            )
        )

    manifest = PackageResourceManifestV1(
        schema_version=payload["schema_version"],
        owner_id=payload["owner_id"],
        package_name=payload["package_name"],
        package_version=payload["package_version"],
        payload_root=payload["payload_root"],
        files=tuple(entries),
    )
    if value != canonical_manifest_json(manifest):
        raise ValueError("manifest must use canonical JSON encoding")
    return manifest


def foundation_manifest() -> PackageResourceManifestV1:
    text = resources.files("hermes_workflows").joinpath(_MANIFEST_RESOURCE).read_text(encoding="utf-8")
    manifest = manifest_from_json(text)
    if manifest.owner_id != _DISTRIBUTION_NAME or manifest.package_name != _DISTRIBUTION_NAME:
        raise ValueError("foundation manifest owner and package must be hermes-workflows")
    if manifest.payload_root != "plugin_payload" or manifest.files:
        raise ValueError("foundation manifest must declare the empty plugin_payload foundation")
    if text != canonical_manifest_json(manifest):
        raise ValueError("foundation manifest resource must contain canonical JSON")
    return manifest


def ownership_key(manifest: PackageResourceManifestV1) -> str:
    canonical = canonical_manifest_json(manifest)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_package_payload(
    manifest: PackageResourceManifestV1,
    destination: PathLike,
) -> Tuple[Path, ...]:
    """Return without touching the filesystem for the empty foundation payload."""

    if not isinstance(manifest, PackageResourceManifestV1):
        raise TypeError("manifest must be a PackageResourceManifestV1")
    if manifest.files:
        raise NotImplementedError("package payload copying is outside the foundation seam")
    return ()

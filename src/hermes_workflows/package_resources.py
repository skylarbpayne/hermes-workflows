from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Tuple, Union


_SCHEMA_VERSION = 1
_DISTRIBUTION_NAME = "hermes-workflows"
_MANIFEST_RESOURCE = "plugin_payload_manifest.v1.json"
_CANONICAL_PAYLOAD_PATHS = (
    "plugin_payload/hermes-workflows-approvals/__init__.py",
    "plugin_payload/hermes-workflows-approvals/dashboard/dist/index.js",
    "plugin_payload/hermes-workflows-approvals/dashboard/dist/style.css",
    "plugin_payload/hermes-workflows-approvals/dashboard/manifest.json",
    "plugin_payload/hermes-workflows-approvals/dashboard/plugin_api.py",
    "plugin_payload/hermes-workflows-approvals/plugin.yaml",
)
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
        if self.package_name != _DISTRIBUTION_NAME:
            raise ValueError("package_name must equal hermes-workflows")
        if not isinstance(self.package_version, str) or not self.package_version.strip():
            raise ValueError("package_version must be nonblank")
        if self.package_version != installed_package_version():
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
            "files": [PackageResourceFileV1.to_dict(entry) for entry in self.files],
        }


def installed_package_version() -> str:
    version = metadata.version(_DISTRIBUTION_NAME)
    if not version.strip():
        raise ValueError("installed distribution version must be nonblank")
    return version


def canonical_manifest_json(manifest: PackageResourceManifestV1) -> str:
    if type(manifest) is not PackageResourceManifestV1:
        raise TypeError("manifest must be exactly PackageResourceManifestV1")
    if any(type(entry) is not PackageResourceFileV1 for entry in manifest.files):
        raise TypeError("manifest files must be exactly PackageResourceFileV1")
    return json.dumps(
        PackageResourceManifestV1.to_dict(manifest),
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


def _resource_bytes(relative: str) -> bytes:
    resource = resources.files("hermes_workflows")
    for part in relative.split("/"):
        resource = resource.joinpath(part)
    return resource.read_bytes()


def _validated_payload_bytes(manifest: PackageResourceManifestV1) -> Tuple[Tuple[PackageResourceFileV1, bytes], ...]:
    validated = []
    for entry in manifest.files:
        try:
            data = _resource_bytes(entry.path)
        except (FileNotFoundError, IsADirectoryError, OSError) as exc:
            raise ValueError(f"packaged payload resource is missing or unreadable: {entry.path}") from exc
        if len(data) != entry.size_bytes or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError(f"packaged payload resource does not match its manifest: {entry.path}")
        validated.append((entry, data))
    return tuple(validated)


def foundation_manifest() -> PackageResourceManifestV1:
    text = resources.files("hermes_workflows").joinpath(_MANIFEST_RESOURCE).read_text(encoding="utf-8")
    manifest = manifest_from_json(text)
    if manifest.owner_id != _DISTRIBUTION_NAME or manifest.package_name != _DISTRIBUTION_NAME:
        raise ValueError("foundation manifest owner and package must be hermes-workflows")
    if manifest.payload_root != "plugin_payload":
        raise ValueError("foundation manifest payload_root must equal plugin_payload")
    if tuple(entry.path for entry in manifest.files) != _CANONICAL_PAYLOAD_PATHS:
        raise ValueError("foundation manifest must declare the exact canonical plugin payload")
    if text != canonical_manifest_json(manifest):
        raise ValueError("foundation manifest resource must contain canonical JSON")
    _validated_payload_bytes(manifest)
    return manifest


def ownership_key(manifest: PackageResourceManifestV1) -> str:
    canonical = canonical_manifest_json(manifest)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_new_destination(root: Path) -> None:
    if not root.is_absolute():
        raise ValueError("package payload destination must be absolute")
    if ".." in root.parts:
        raise ValueError("package payload destination must not contain traversal components")

    current = Path(root.anchor)
    for component in root.parts[1:-1]:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise ValueError(f"package payload destination parent does not exist: {current}")
        if stat.S_ISLNK(mode):
            raise ValueError(f"package payload destination contains symlink ancestor: {current}")
        if not stat.S_ISDIR(mode):
            raise ValueError(f"package payload destination ancestor is not a directory: {current}")


def _directory_open_flags() -> int:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required_flags):
        raise OSError("package payload writes require no-follow directory descriptors")
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        raise OSError("package payload writes require descriptor-relative filesystem operations")
    if os.stat not in os.supports_follow_symlinks:
        raise OSError("package payload writes require no-follow descriptor-relative stat")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_path(path: Path) -> int:
    flags = _directory_open_flags()
    descriptor = os.open(path.anchor, flags)
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current = current / component
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                try:
                    mode = os.stat(component, dir_fd=descriptor, follow_symlinks=False).st_mode
                except OSError:
                    raise ValueError(f"package payload destination parent changed during validation: {current}") from exc
                if stat.S_ISLNK(mode):
                    raise ValueError(f"package payload destination contains symlink ancestor: {current}") from exc
                if not stat.S_ISDIR(mode):
                    raise ValueError(f"package payload destination ancestor is not a directory: {current}") from exc
                raise ValueError(f"package payload destination parent changed during validation: {current}") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _entry_matches_stat(parent_fd: int, name: str, expected: os.stat_result) -> bool:
    try:
        actual = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return _same_file_identity(actual, expected)


def _entry_matches_fd(parent_fd: int, name: str, descriptor: int) -> bool:
    try:
        actual = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        expected = os.fstat(descriptor)
    except OSError:
        return False
    return stat.S_ISDIR(actual.st_mode) and _same_file_identity(actual, expected)


def _path_matches_fd(path: Path, descriptor: int) -> bool:
    try:
        current = _open_directory_path(path)
    except (OSError, ValueError):
        return False
    try:
        return _same_file_identity(os.fstat(current), os.fstat(descriptor))
    finally:
        os.close(current)


def _remove_created_entries(
    files: list[Tuple[int, str, os.stat_result]],
    directories: list[Tuple[int, str, os.stat_result]],
) -> None:
    for parent_fd, name, expected in reversed(files):
        if _entry_matches_stat(parent_fd, name, expected):
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
    for parent_fd, name, expected in reversed(directories):
        if _entry_matches_stat(parent_fd, name, expected):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass


def write_package_payload(
    manifest: PackageResourceManifestV1,
    destination: PathLike,
) -> Tuple[Path, ...]:
    """Copy the exact validated package payload into a new destination root."""

    canonical = canonical_manifest_json(manifest)
    packaged = foundation_manifest()
    if canonical != canonical_manifest_json(packaged):
        raise ValueError("manifest must exactly match the packaged payload manifest")
    payload = _validated_payload_bytes(packaged)
    root = Path(destination)
    _validate_new_destination(root)
    if not root.name:
        raise FileExistsError(f"refusing to replace existing destination: {root}")

    parent_fd = _open_directory_path(root.parent)
    root_fd = None
    directory_fds = {}
    created_files = []
    created_directories = []
    written = []
    try:
        try:
            os.mkdir(root.name, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to replace existing destination: {root}") from exc
        root_stat = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        created_directories.append((parent_fd, root.name, root_stat))
        root_fd = os.open(root.name, _directory_open_flags(), dir_fd=parent_fd)
        if not _entry_matches_fd(parent_fd, root.name, root_fd):
            raise ValueError("package payload destination changed during creation")
        directory_fds[()] = root_fd

        for entry, data in payload:
            parts = PurePosixPath(entry.path).parts
            parent_parts = ()
            for component in parts[:-1]:
                child_parts = parent_parts + (component,)
                if child_parts not in directory_fds:
                    component_parent_fd = directory_fds[parent_parts]
                    os.mkdir(component, dir_fd=component_parent_fd)
                    component_stat = os.stat(component, dir_fd=component_parent_fd, follow_symlinks=False)
                    created_directories.append((component_parent_fd, component, component_stat))
                    child_fd = os.open(component, _directory_open_flags(), dir_fd=component_parent_fd)
                    if not _entry_matches_fd(component_parent_fd, component, child_fd):
                        os.close(child_fd)
                        raise ValueError("package payload destination directory changed during creation")
                    directory_fds[child_parts] = child_fd
                parent_parts = child_parts

            file_parent_fd = directory_fds[parent_parts]
            file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            file_fd = os.open(parts[-1], file_flags, 0o666, dir_fd=file_parent_fd)
            file_stat = os.fstat(file_fd)
            created_files.append((file_parent_fd, parts[-1], file_stat))
            with os.fdopen(file_fd, "wb") as handle:
                handle.write(data)
            written.append(root / entry.path)

        if not _path_matches_fd(root.parent, parent_fd) or not _entry_matches_fd(parent_fd, root.name, root_fd):
            raise ValueError("package payload destination changed before completion")
    except Exception:
        _remove_created_entries(created_files, created_directories)
        raise
    finally:
        for parts, descriptor in reversed(tuple(directory_fds.items())):
            if parts:
                os.close(descriptor)
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)
    return tuple(written)

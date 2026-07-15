from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from . import package_resources
from .package_resources import installed_package_version


PLUGIN_NAME = "hermes-workflows-approvals"
PACKAGE_NAME = "hermes-workflows"
OWNER_ID = PACKAGE_NAME
PACKAGE_VERSION = installed_package_version()
RECEIPT_NAME = ".hermes-workflows-owner.json"
RECEIPT_SCHEMA_VERSION = 1
RESCAN_ENDPOINT = "/api/dashboard/plugins/rescan"
PAYLOAD_FILES = (
    "__init__.py",
    "dashboard/dist/index.js",
    "dashboard/dist/style.css",
    "dashboard/manifest.json",
    "dashboard/plugin_api.py",
    "plugin.yaml",
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "owner_id",
        "package_name",
        "package_version",
        "plugin_name",
        "plugin_version",
        "ownership_key",
        "files",
    }
)
_FILE_FIELDS = frozenset({"path", "sha256", "size_bytes"})
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION_LINE = re.compile(r'^version:\s*["\']?([^"\'\s#]+)["\']?\s*(?:#.*)?$')
_NAME_LINE = re.compile(r"^name:\s*([a-z0-9][a-z0-9_.-]*)\s*(?:#.*)?$")
PathLike = Union[str, Path]


class PluginInstallError(RuntimeError):
    pass


class PayloadValidationError(PluginInstallError):
    pass


class OwnershipError(PluginInstallError):
    pass


class UserFileConflictError(OwnershipError):
    pass


class RollbackUnavailableError(PluginInstallError):
    pass


@dataclass(frozen=True)
class PayloadDescriptor:
    package_version: str
    plugin_version: str
    files: Tuple[str, ...]
    ownership_key: str
    dashboard_manifest: Mapping[str, Any]


@dataclass(frozen=True)
class PluginDiscovery:
    plugin_name: str
    plugin_version: str
    package_version: str
    plugin_path: str
    manifest_path: str
    api_path: str
    entry_path: str
    css_path: str
    api_route: str
    asset_routes: Tuple[str, ...]
    ownership_key: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "plugin_name": self.plugin_name,
            "plugin_version": self.plugin_version,
            "package_version": self.package_version,
            "plugin_path": self.plugin_path,
            "manifest_path": self.manifest_path,
            "api_path": self.api_path,
            "entry_path": self.entry_path,
            "css_path": self.css_path,
            "api_route": self.api_route,
            "asset_routes": list(self.asset_routes),
            "ownership_key": self.ownership_key,
        }


@dataclass(frozen=True)
class PluginLifecycleReport:
    action: str
    profile_home: str
    plugin_path: str
    package_version: str
    plugin_version: str
    ownership_key: str
    enabled: bool
    previous_version: Optional[str]
    rollback_available: bool
    restart_required: bool
    rescan_supported: bool
    rescan_endpoint: str
    reload_note: str
    files: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "action": self.action,
            "profile_home": self.profile_home,
            "plugin_path": self.plugin_path,
            "package_version": self.package_version,
            "plugin_version": self.plugin_version,
            "ownership_key": self.ownership_key,
            "enabled": self.enabled,
            "previous_version": self.previous_version,
            "rollback_available": self.rollback_available,
            "restart_required": self.restart_required,
            "rescan_supported": self.rescan_supported,
            "rescan_endpoint": self.rescan_endpoint,
            "reload_note": self.reload_note,
            "files": list(self.files),
        }


def canonical_payload_root() -> Path:
    return Path(__file__).resolve().parent / "plugin_payload" / PLUGIN_NAME


def _validate_relative_path(value: object, *, error_type: type[PluginInstallError]) -> str:
    if not isinstance(value, str) or not value:
        raise error_type("owned file path must be a nonempty relative POSIX path")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise error_type("owned file path must be a normalized relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or str(PurePosixPath(value)) != value:
        raise error_type("owned file path must be a normalized relative POSIX path")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _ownership_key(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "ownership_key"}
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _regular_file(path: Path, *, error_type: type[PluginInstallError], label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise error_type(f"{label} must be a regular non-symlink file")


def _tree_files(root: Path, *, error_type: type[PluginInstallError]) -> Tuple[str, ...]:
    if root.is_symlink() or not root.is_dir():
        raise error_type("plugin root must be a regular directory, not a symlink")
    discovered = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in directory_names:
            child = directory_path / name
            if child.is_symlink():
                raise error_type(f"plugin directory contains symlink: {child.relative_to(root).as_posix()}")
        for name in file_names:
            child = directory_path / name
            if child.is_symlink() or not child.is_file():
                raise error_type(f"plugin payload contains non-regular file: {child.relative_to(root).as_posix()}")
            discovered.append(child.relative_to(root).as_posix())
    return tuple(sorted(discovered))


def _is_installer_generated_bytecode(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return "__pycache__" in parts and relative.endswith(".pyc")


def _plugin_yaml_identity(path: Path) -> Tuple[str, str]:
    _regular_file(path, error_type=PayloadValidationError, label="plugin.yaml")
    name = None
    version = None
    for line in path.read_text(encoding="utf-8").splitlines():
        name_match = _NAME_LINE.fullmatch(line)
        if name_match:
            if name is not None:
                raise PayloadValidationError("plugin.yaml must declare name exactly once")
            name = name_match.group(1)
        version_match = _VERSION_LINE.fullmatch(line)
        if version_match:
            if version is not None:
                raise PayloadValidationError("plugin.yaml must declare version exactly once")
            version = version_match.group(1)
    if name != PLUGIN_NAME:
        raise PayloadValidationError(f"plugin.yaml name must equal {PLUGIN_NAME}")
    if not version:
        raise PayloadValidationError("plugin.yaml must declare a nonblank version")
    return name, version


def _dashboard_manifest(path: Path) -> Mapping[str, Any]:
    _regular_file(path, error_type=PayloadValidationError, label="dashboard manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadValidationError("dashboard manifest must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PayloadValidationError("dashboard manifest must be a JSON object")
    expected = {
        "name": PLUGIN_NAME,
        "api": "plugin_api.py",
        "entry": "dist/index.js",
        "css": "dist/style.css",
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise PayloadValidationError(f"dashboard manifest {key} must equal {expected_value}")
    if not isinstance(value.get("version"), str) or not value["version"].strip():
        raise PayloadValidationError("dashboard manifest must declare a nonblank version")
    return value


def inspect_payload(
    payload_root: Optional[PathLike] = None,
    *,
    expected_package_version: Optional[str] = None,
) -> PayloadDescriptor:
    packaged_payload = payload_root is None
    root = canonical_payload_root() if payload_root is None else Path(payload_root)
    if not root.is_absolute():
        root = root.resolve()
    actual_files = _tree_files(root, error_type=PayloadValidationError)
    if packaged_payload:
        actual_files = tuple(relative for relative in actual_files if not _is_installer_generated_bytecode(relative))
    if actual_files != PAYLOAD_FILES:
        missing = sorted(set(PAYLOAD_FILES) - set(actual_files))
        unexpected = sorted(set(actual_files) - set(PAYLOAD_FILES))
        raise PayloadValidationError(f"payload file set mismatch; missing={missing}, unexpected={unexpected}")

    _, plugin_version = _plugin_yaml_identity(root / "plugin.yaml")
    dashboard_manifest = _dashboard_manifest(root / "dashboard" / "manifest.json")
    package_version = PACKAGE_VERSION if expected_package_version is None else expected_package_version
    if not isinstance(package_version, str) or not package_version.strip():
        raise PayloadValidationError("expected package version must be nonblank")
    if plugin_version != package_version or dashboard_manifest["version"] != package_version:
        raise PayloadValidationError("package, plugin, and dashboard manifest versions must match")

    files = []
    for relative in PAYLOAD_FILES:
        path = root / relative
        _regular_file(path, error_type=PayloadValidationError, label=relative)
        files.append({"path": relative, "sha256": _sha256_file(path), "size_bytes": path.stat().st_size})
    if packaged_payload:
        try:
            manifest = package_resources.foundation_manifest()
        except (TypeError, ValueError, OSError) as exc:
            raise PayloadValidationError(f"package payload manifest validation failed: {exc}") from exc
        manifest_paths = tuple(f"plugin_payload/{PLUGIN_NAME}/{relative}" for relative in PAYLOAD_FILES)
        if tuple(entry.path for entry in manifest.files) != manifest_paths:
            raise PayloadValidationError("package payload manifest paths do not match the canonical payload")
        for entry, actual in zip(manifest.files, files):
            if entry.sha256 != actual["sha256"] or entry.size_bytes != actual["size_bytes"]:
                raise PayloadValidationError(f"package payload manifest bytes do not match: {entry.path}")
    unsigned = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "owner_id": OWNER_ID,
        "package_name": PACKAGE_NAME,
        "package_version": package_version,
        "plugin_name": PLUGIN_NAME,
        "plugin_version": plugin_version,
        "files": files,
    }
    return PayloadDescriptor(
        package_version=package_version,
        plugin_version=plugin_version,
        files=PAYLOAD_FILES,
        ownership_key=_ownership_key(unsigned),
        dashboard_manifest=dashboard_manifest,
    )


def _receipt_for_payload(root: Path, descriptor: PayloadDescriptor) -> Dict[str, Any]:
    files = [
        {
            "path": relative,
            "sha256": _sha256_file(root / relative),
            "size_bytes": (root / relative).stat().st_size,
        }
        for relative in descriptor.files
    ]
    receipt: Dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "owner_id": OWNER_ID,
        "package_name": PACKAGE_NAME,
        "package_version": descriptor.package_version,
        "plugin_name": PLUGIN_NAME,
        "plugin_version": descriptor.plugin_version,
        "files": files,
    }
    receipt["ownership_key"] = _ownership_key(receipt)
    return receipt


def _load_receipt(root: Path) -> Mapping[str, Any]:
    receipt_path = root / RECEIPT_NAME
    if not receipt_path.exists():
        raise UserFileConflictError(f"refusing to manage user-owned plugin directory without {RECEIPT_NAME}: {root}")
    _regular_file(receipt_path, error_type=OwnershipError, label="ownership receipt")
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OwnershipError("ownership receipt must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
        raise OwnershipError("ownership receipt fields do not match schema version 1")
    if value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise OwnershipError("ownership receipt schema_version must equal 1")
    for key, expected in (("owner_id", OWNER_ID), ("package_name", PACKAGE_NAME), ("plugin_name", PLUGIN_NAME)):
        if value.get(key) != expected:
            raise OwnershipError(f"ownership receipt {key} must equal {expected}")
    package_version = value.get("package_version")
    plugin_version = value.get("plugin_version")
    if not isinstance(package_version, str) or not package_version or plugin_version != package_version:
        raise OwnershipError("ownership receipt package and plugin versions must be equal and nonblank")
    key = value.get("ownership_key")
    if not isinstance(key, str) or _SHA256.fullmatch(key) is None:
        raise OwnershipError("ownership receipt key must be a lowercase SHA-256")
    if not hmac.compare_digest(key, _ownership_key(value)):
        raise OwnershipError("ownership receipt key does not match its contents")
    return value


def _validate_owned_tree(root: Path) -> Mapping[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise UserFileConflictError(f"refusing to manage non-directory or symlink plugin path: {root}")
    receipt = _load_receipt(root)
    actual_files = _tree_files(root, error_type=OwnershipError)
    expected_actual = tuple(sorted(PAYLOAD_FILES + (RECEIPT_NAME,)))
    if actual_files != expected_actual:
        extras = sorted(set(actual_files) - set(expected_actual))
        missing = sorted(set(expected_actual) - set(actual_files))
        if extras:
            raise UserFileConflictError(f"refusing to overwrite or delete user-owned plugin files: {extras}")
        raise OwnershipError(f"managed plugin is missing owned files: {missing}")

    raw_files = receipt.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != len(PAYLOAD_FILES):
        raise OwnershipError("ownership receipt files must list the complete payload")
    paths = []
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != _FILE_FIELDS:
            raise OwnershipError("ownership receipt file fields do not match schema version 1")
        relative = _validate_relative_path(item.get("path"), error_type=OwnershipError)
        paths.append(relative)
        sha256 = item.get("sha256")
        size_bytes = item.get("size_bytes")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise OwnershipError("ownership receipt file hash must be a lowercase SHA-256")
        if type(size_bytes) is not int or size_bytes < 0:
            raise OwnershipError("ownership receipt file size must be a nonnegative integer")
        path = root / relative
        _regular_file(path, error_type=OwnershipError, label=relative)
        if path.stat().st_size != size_bytes or not hmac.compare_digest(_sha256_file(path), sha256):
            raise OwnershipError(f"owned plugin file no longer matches receipt: {relative}")
    if tuple(paths) != PAYLOAD_FILES:
        raise OwnershipError("ownership receipt paths must equal the canonical sorted payload paths")
    return receipt


def _validated_profile_home(profile_home: PathLike) -> Path:
    raw_home = os.fspath(profile_home)
    if not isinstance(raw_home, str) or not raw_home or "\x00" in raw_home:
        raise UserFileConflictError("profile home must be a nonempty filesystem path")
    expanded_home = os.path.expanduser(raw_home)
    if raw_home.startswith("~") and expanded_home == raw_home:
        raise UserFileConflictError("profile home user expansion could not be resolved")
    if not os.path.isabs(expanded_home):
        raise UserFileConflictError("profile home must be absolute after user expansion")

    component_text = os.path.splitdrive(expanded_home)[1]
    if os.altsep:
        component_text = component_text.replace(os.altsep, os.sep)
    if any(component in {".", ".."} for component in component_text.split(os.sep)):
        raise UserFileConflictError("profile home traversal components are not allowed")

    home = Path(expanded_home)
    current = Path(home.anchor)
    candidates = [current]
    for component in home.parts[1:]:
        current = current / component
        candidates.append(current)

    for candidate in candidates:
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise UserFileConflictError(f"profile home path contains symlink component: {candidate}")
        if not stat.S_ISDIR(mode):
            raise UserFileConflictError(f"profile home path component must be a directory: {candidate}")

    return home.resolve()


def _validated_config_path(home: Path) -> Path:
    config = home / "config.yaml"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise UserFileConflictError("profile config.yaml must be a regular non-symlink file")
    return config


def _profile_paths(profile_home: PathLike) -> Tuple[Path, Path, Path, Path]:
    home = _validated_profile_home(profile_home)
    home.mkdir(parents=True, exist_ok=True)
    _validated_config_path(home)
    plugins = home / "plugins"
    if plugins.is_symlink() or (plugins.exists() and not plugins.is_dir()):
        raise UserFileConflictError("profile plugin root must be a directory, not a file or symlink")
    plugins.mkdir(exist_ok=True)
    destination = plugins / PLUGIN_NAME
    rollback = plugins / f".{PLUGIN_NAME}.rollback"
    for managed in (destination, rollback):
        if managed.is_symlink() or (managed.exists() and not managed.is_dir()):
            raise UserFileConflictError(f"managed plugin path must be a directory, not a file or symlink: {managed}")
    return home, plugins, destination, rollback


def _write_exclusive(path: Path, data: bytes, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _copy_payload_to_stage(payload_root: Path, stage: Path, descriptor: PayloadDescriptor) -> None:
    stage.mkdir(mode=0o700)
    try:
        for relative in descriptor.files:
            source = payload_root / relative
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_exclusive(destination, source.read_bytes(), mode=0o644)
        staged_descriptor = inspect_payload(stage, expected_package_version=descriptor.package_version)
        if not hmac.compare_digest(staged_descriptor.ownership_key, descriptor.ownership_key):
            raise PayloadValidationError("payload changed while it was being staged")
        receipt = _receipt_for_payload(stage, staged_descriptor)
        _write_exclusive((stage / RECEIPT_NAME), (_canonical_json(receipt) + "\n").encode("utf-8"), mode=0o600)
        _validate_owned_tree(stage)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def _remove_verified_tree(path: Path) -> None:
    _validate_owned_tree(path)
    shutil.rmtree(path)


def _recover_interrupted(plugins: Path, destination: Path, rollback: Path) -> None:
    for stale in sorted(plugins.glob(f".{PLUGIN_NAME}.stage-*")):
        _remove_verified_tree(stale)

    interrupted_swaps = sorted(plugins.glob(f".{PLUGIN_NAME}.swap-*"))
    if len(interrupted_swaps) > 1:
        raise OwnershipError("multiple interrupted rollback trees require operator review")
    if interrupted_swaps:
        swap = interrupted_swaps[0]
        _validate_owned_tree(swap)
        if destination.exists() and rollback.exists():
            raise OwnershipError("interrupted rollback conflicts with current and retained plugin trees")
        if destination.exists():
            _validate_owned_tree(destination)
            os.replace(destination, rollback)
        os.replace(swap, destination)

    interrupted_current = sorted(plugins.glob(f".{PLUGIN_NAME}.remove-current-*"))
    interrupted_rollback = sorted(plugins.glob(f".{PLUGIN_NAME}.remove-rollback-*"))
    if len(interrupted_current) > 1 or len(interrupted_rollback) > 1:
        raise OwnershipError("multiple interrupted uninstall trees require operator review")
    if interrupted_current:
        _validate_owned_tree(interrupted_current[0])
        if destination.exists():
            raise OwnershipError("interrupted uninstall conflicts with an installed plugin tree")
        os.replace(interrupted_current[0], destination)
    if interrupted_rollback:
        _validate_owned_tree(interrupted_rollback[0])
        if rollback.exists():
            raise OwnershipError("interrupted uninstall conflicts with an existing rollback tree")
        os.replace(interrupted_rollback[0], rollback)
    if not destination.exists() and rollback.exists():
        _validate_owned_tree(rollback)
        os.replace(rollback, destination)


def _list_section(lines: Sequence[str], start: int, end: int, key: str) -> Tuple[Optional[Tuple[int, int]], list[str]]:
    header = re.compile(rf"^  {re.escape(key)}:\s*(?:#.*)?$")
    unsupported = re.compile(rf"^  {re.escape(key)}:\s*\S")
    found = None
    values: list[str] = []
    for index in range(start + 1, end):
        line = lines[index].rstrip("\n")
        if unsupported.match(line) and not header.fullmatch(line):
            raise UserFileConflictError(f"config plugins.{key} must use a block list")
        if not header.fullmatch(line):
            continue
        if found is not None:
            raise UserFileConflictError(f"config contains duplicate plugins.{key} sections")
        section_end = index + 1
        while section_end < end:
            candidate = lines[section_end].rstrip("\n")
            if candidate.strip() and not candidate.lstrip().startswith("#") and len(candidate) - len(candidate.lstrip(" ")) <= 2:
                break
            section_end += 1
        for item_index in range(index + 1, section_end):
            candidate = lines[item_index].rstrip("\n")
            if not candidate.strip() or candidate.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r"\s{4}-\s+([a-z0-9][a-z0-9_.-]{0,127})\s*(?:#.*)?", candidate)
            if match is None:
                raise UserFileConflictError(f"config plugins.{key} contains an unsupported list item")
            values.append(match.group(1))
        found = (index, section_end)
    return found, values


def _unique(values: Sequence[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _update_enablement_text(text: str, *, enabled: bool) -> str:
    if "\t" in text:
        raise UserFileConflictError("config.yaml tabs are unsupported for safe plugin enablement editing")
    lines = text.splitlines(keepends=True)
    inline_plugins = [
        line
        for line in lines
        if line.startswith("plugins:") and re.fullmatch(r"plugins:\s*(?:#.*)?\n?", line) is None
    ]
    if inline_plugins:
        raise UserFileConflictError("config plugins must use a block mapping")
    starts = [index for index, line in enumerate(lines) if re.fullmatch(r"plugins:\s*(?:#.*)?\n?", line)]
    if len(starts) > 1:
        raise UserFileConflictError("config contains duplicate top-level plugins sections")
    if not starts:
        if not enabled:
            return text
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        return prefix + f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n"

    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        candidate = lines[index]
        if candidate.strip() and not candidate.lstrip().startswith("#") and not candidate.startswith(" "):
            end = index
            break
    enabled_range, enabled_values = _list_section(lines, start, end, "enabled")
    disabled_range, disabled_values = _list_section(lines, start, end, "disabled")
    ranges = [item for item in (enabled_range, disabled_range) if item is not None]
    covered = set()
    for first, last in ranges:
        covered.update(range(first, last))
    remaining = [lines[index] for index in range(start + 1, end) if index not in covered]
    enabled_values = [value for value in _unique(enabled_values) if value != PLUGIN_NAME]
    disabled_values = [value for value in _unique(disabled_values) if value != PLUGIN_NAME]
    if enabled:
        enabled_values.append(PLUGIN_NAME)

    block = [lines[start] if lines[start].endswith("\n") else lines[start] + "\n"]
    if enabled_values:
        block.append("  enabled:\n")
        block.extend(f"    - {value}\n" for value in enabled_values)
    if disabled_values:
        block.append("  disabled:\n")
        block.extend(f"    - {value}\n" for value in disabled_values)
    block.extend(remaining)
    meaningful_body = [line for line in block[1:] if line.strip() and not line.lstrip().startswith("#")]
    if not meaningful_body:
        block = []
    return "".join(lines[:start] + block + lines[end:])


def _config_update(home: Path, *, enabled: bool) -> Tuple[Path, bytes]:
    config = _validated_config_path(home)
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    updated = _update_enablement_text(text, enabled=enabled).encode("utf-8")
    temporary = home / f".config.yaml.hermes-workflows-{uuid.uuid4().hex}.tmp"
    _write_exclusive(temporary, updated, mode=0o600)
    return temporary, updated


def _lifecycle_report(
    *,
    action: str,
    home: Path,
    destination: Path,
    receipt: Mapping[str, Any],
    enabled: bool,
    previous_version: Optional[str],
    rollback_available: bool,
) -> PluginLifecycleReport:
    return PluginLifecycleReport(
        action=action,
        profile_home=str(home),
        plugin_path=str(destination),
        package_version=str(receipt["package_version"]),
        plugin_version=str(receipt["plugin_version"]),
        ownership_key=str(receipt["ownership_key"]),
        enabled=enabled,
        previous_version=previous_version,
        rollback_available=rollback_available,
        restart_required=True,
        rescan_supported=True,
        rescan_endpoint=RESCAN_ENDPOINT,
        reload_note="Rescan refreshes the manifest and static assets; restart the dashboard process to mount plugin_api.py.",
        files=tuple(item["path"] for item in receipt["files"]),
    )


def _install_or_upgrade(
    profile_home: PathLike,
    *,
    payload_root: Optional[PathLike],
    expected_package_version: Optional[str],
    require_existing: bool,
) -> PluginLifecycleReport:
    validated_home = _validated_profile_home(profile_home)
    source = canonical_payload_root() if payload_root is None else Path(payload_root).resolve()
    descriptor = inspect_payload(payload_root, expected_package_version=expected_package_version)
    home, plugins, destination, rollback = _profile_paths(validated_home)
    _recover_interrupted(plugins, destination, rollback)
    if require_existing and not destination.exists():
        raise UserFileConflictError("upgrade requires an existing owned plugin installation")

    previous_receipt = _validate_owned_tree(destination) if destination.exists() else None
    if rollback.exists():
        _validate_owned_tree(rollback)
    config_temporary, _ = _config_update(home, enabled=True)
    stage = plugins / f".{PLUGIN_NAME}.stage-{uuid.uuid4().hex}"
    moved_previous = False
    try:
        _copy_payload_to_stage(source, stage, descriptor)
        if destination.exists():
            if rollback.exists():
                _remove_verified_tree(rollback)
            os.replace(destination, rollback)
            moved_previous = True
        os.replace(stage, destination)
        try:
            os.replace(config_temporary, home / "config.yaml")
        except Exception:
            _remove_verified_tree(destination)
            if moved_previous and rollback.exists():
                os.replace(rollback, destination)
            raise
    except Exception as exc:
        if not destination.exists() and moved_previous and rollback.exists():
            os.replace(rollback, destination)
        if stage.exists():
            _remove_verified_tree(stage)
        if config_temporary.exists():
            config_temporary.unlink()
        if isinstance(exc, PluginInstallError):
            raise
        raise PluginInstallError(str(exc)) from exc

    receipt = _validate_owned_tree(destination)
    action = "upgrade" if previous_receipt is not None else "install"
    return _lifecycle_report(
        action=action,
        home=home,
        destination=destination,
        receipt=receipt,
        enabled=True,
        previous_version=str(previous_receipt["plugin_version"]) if previous_receipt else None,
        rollback_available=rollback.exists(),
    )


def install_plugin(
    profile_home: PathLike,
    *,
    payload_root: Optional[PathLike] = None,
    expected_package_version: Optional[str] = None,
) -> PluginLifecycleReport:
    return _install_or_upgrade(
        profile_home,
        payload_root=payload_root,
        expected_package_version=expected_package_version,
        require_existing=False,
    )


def upgrade_plugin(
    profile_home: PathLike,
    *,
    payload_root: Optional[PathLike] = None,
    expected_package_version: Optional[str] = None,
) -> PluginLifecycleReport:
    return _install_or_upgrade(
        profile_home,
        payload_root=payload_root,
        expected_package_version=expected_package_version,
        require_existing=True,
    )


def rollback_plugin(profile_home: PathLike) -> PluginLifecycleReport:
    home, plugins, destination, rollback = _profile_paths(profile_home)
    _recover_interrupted(plugins, destination, rollback)
    if not destination.exists() or not rollback.exists():
        raise RollbackUnavailableError("one owned rollback is required")
    current = _validate_owned_tree(destination)
    previous = _validate_owned_tree(rollback)
    config_temporary, _ = _config_update(home, enabled=True)
    swap = plugins / f".{PLUGIN_NAME}.swap-{uuid.uuid4().hex}"
    swapped = False
    try:
        os.replace(destination, swap)
        os.replace(rollback, destination)
        os.replace(swap, rollback)
        swapped = True
        os.replace(config_temporary, home / "config.yaml")
    except Exception as exc:
        if swapped:
            os.replace(destination, swap)
            os.replace(rollback, destination)
            os.replace(swap, rollback)
        elif swap.exists():
            if destination.exists() and not rollback.exists():
                os.replace(destination, rollback)
            if not destination.exists():
                os.replace(swap, destination)
        if config_temporary.exists():
            config_temporary.unlink()
        if isinstance(exc, PluginInstallError):
            raise
        raise PluginInstallError(str(exc)) from exc

    installed = _validate_owned_tree(destination)
    _validate_owned_tree(rollback)
    return _lifecycle_report(
        action="rollback",
        home=home,
        destination=destination,
        receipt=installed,
        enabled=True,
        previous_version=str(current["plugin_version"]),
        rollback_available=True,
    )


def uninstall_plugin(profile_home: PathLike) -> PluginLifecycleReport:
    home, plugins, destination, rollback = _profile_paths(profile_home)
    _recover_interrupted(plugins, destination, rollback)
    current = _validate_owned_tree(destination) if destination.exists() else None
    retained = _validate_owned_tree(rollback) if rollback.exists() else None
    if current is None and retained is None:
        raise UserFileConflictError("uninstall requires an existing owned plugin installation")
    config_temporary, _ = _config_update(home, enabled=False)
    removed = []
    try:
        for kind, source in (("current", destination), ("rollback", rollback)):
            if not source.exists():
                continue
            temporary = plugins / f".{PLUGIN_NAME}.remove-{kind}-{uuid.uuid4().hex}"
            os.replace(source, temporary)
            removed.append((source, temporary))
        os.replace(config_temporary, home / "config.yaml")
    except Exception as exc:
        for source, temporary in reversed(removed):
            if temporary.exists() and not source.exists():
                os.replace(temporary, source)
        if config_temporary.exists():
            config_temporary.unlink()
        if isinstance(exc, PluginInstallError):
            raise
        raise PluginInstallError(str(exc)) from exc
    for _, temporary in removed:
        _remove_verified_tree(temporary)

    receipt = current if current is not None else retained
    assert receipt is not None
    return _lifecycle_report(
        action="uninstall",
        home=home,
        destination=destination,
        receipt=receipt,
        enabled=False,
        previous_version=str(receipt["plugin_version"]),
        rollback_available=False,
    )


def discover_installed_plugin(profile_home: PathLike) -> PluginDiscovery:
    home = _validated_profile_home(profile_home)
    destination = home / "plugins" / PLUGIN_NAME
    receipt = _validate_owned_tree(destination)
    manifest = _dashboard_manifest(destination / "dashboard" / "manifest.json")
    dashboard = destination / "dashboard"
    entry = str(manifest["entry"])
    css = str(manifest["css"])
    api = str(manifest["api"])
    for relative in (entry, css, api):
        _validate_relative_path(relative, error_type=OwnershipError)
        _regular_file(dashboard / relative, error_type=OwnershipError, label=relative)
    return PluginDiscovery(
        plugin_name=PLUGIN_NAME,
        plugin_version=str(receipt["plugin_version"]),
        package_version=str(receipt["package_version"]),
        plugin_path=str(destination),
        manifest_path=str(dashboard / "manifest.json"),
        api_path=str(dashboard / api),
        entry_path=str(dashboard / entry),
        css_path=str(dashboard / css),
        api_route=f"/api/plugins/{PLUGIN_NAME}",
        asset_routes=(f"/dashboard-plugins/{PLUGIN_NAME}/{entry}", f"/dashboard-plugins/{PLUGIN_NAME}/{css}"),
        ownership_key=str(receipt["ownership_key"]),
    )

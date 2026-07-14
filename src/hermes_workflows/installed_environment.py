from __future__ import annotations

import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

from .package_resources import foundation_manifest, installed_package_version, ownership_key


PathLike = Union[str, Path]


@dataclass(frozen=True)
class InstalledExecution:
    """The interpreter and environment owned by the installed console script."""

    python_executable: str
    environment: Dict[str, str]
    visible_uv: Optional[str]


@dataclass(frozen=True)
class InstalledEnvironmentReport:
    """Non-secret identity fields for an installed workflow process."""

    schema_version: int
    python_executable: str
    virtual_env: Optional[str]
    visible_uv: Optional[str]
    package_origin: str
    package_version: str
    package_manifest_sha256: str
    package_ownership_key: str
    registry_path: str
    registry_sha256: str
    db_alias: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "python_executable": self.python_executable,
            "virtual_env": self.virtual_env,
            "visible_uv": self.visible_uv,
            "package_origin": self.package_origin,
            "package_version": self.package_version,
            "package_manifest_sha256": self.package_manifest_sha256,
            "package_ownership_key": self.package_ownership_key,
            "registry_path": self.registry_path,
            "registry_sha256": self.registry_sha256,
            "db_alias": self.db_alias,
        }


def resolve_installed_execution(
    *,
    environ: Optional[Mapping[str, str]] = None,
    python_executable: Optional[str] = None,
) -> InstalledExecution:
    """Retain the current interpreter and environment without project discovery.

    The returned mapping is a copy so callers may add explicit child-process
    settings without mutating the console script's process environment.
    """

    source = os.environ if environ is None else environ
    environment = {str(key): str(value) for key, value in source.items()}
    executable = sys.executable if python_executable is None else str(python_executable)
    if not executable:
        raise ValueError("python_executable must be nonblank")
    visible_uv = shutil.which("uv", path=environment.get("PATH"))
    return InstalledExecution(
        python_executable=executable,
        environment=environment,
        visible_uv=visible_uv,
    )


def installed_environment_report(
    *,
    registry_path: PathLike,
    db_alias: str,
    environ: Optional[Mapping[str, str]] = None,
    python_executable: Optional[str] = None,
) -> InstalledEnvironmentReport:
    """Resolve safe package, interpreter, registry, and DB-alias identity."""

    alias = str(db_alias)
    if not alias.strip():
        raise ValueError("db_alias must be nonblank")
    registry = Path(registry_path).expanduser().resolve(strict=True)
    if not registry.is_file():
        raise ValueError("registry_path must identify a file")

    execution = resolve_installed_execution(
        environ=environ,
        python_executable=python_executable,
    )
    registry_sha256 = hashlib.sha256(registry.read_bytes()).hexdigest()
    package_manifest_sha256 = ownership_key(foundation_manifest())
    return InstalledEnvironmentReport(
        schema_version=1,
        python_executable=execution.python_executable,
        virtual_env=execution.environment.get("VIRTUAL_ENV"),
        visible_uv=execution.visible_uv,
        package_origin=str(Path(__file__).resolve()),
        package_version=installed_package_version(),
        package_manifest_sha256=package_manifest_sha256,
        package_ownership_key=package_manifest_sha256,
        registry_path=str(registry),
        registry_sha256=registry_sha256,
        db_alias=alias,
    )

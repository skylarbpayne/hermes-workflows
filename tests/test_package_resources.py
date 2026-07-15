from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_workflows.package_resources import (
    PackageResourceManifestV1,
    foundation_manifest,
    write_package_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _manifest_with(*, files):
    manifest = foundation_manifest()
    return PackageResourceManifestV1(
        schema_version=manifest.schema_version,
        owner_id=manifest.owner_id,
        package_name=manifest.package_name,
        package_version=manifest.package_version,
        payload_root=manifest.payload_root,
        files=files,
    )


@pytest.mark.parametrize("change", ["missing", "extra", "hash", "size"])
def test_copy_refuses_manifest_drift_without_touching_destination(tmp_path: Path, change: str):
    manifest = foundation_manifest()
    files = list(manifest.files)
    if change == "missing":
        files.pop()
    elif change == "extra":
        last = files[-1]
        files.append(last.__class__(1, "plugin_payload/z-extra.txt", "0" * 64, 0))
    elif change == "hash":
        first = files[0]
        files[0] = first.__class__(1, first.path, "0" * 64, first.size_bytes)
    else:
        first = files[0]
        files[0] = first.__class__(1, first.path, first.sha256, first.size_bytes + 1)
    hostile = _manifest_with(files=tuple(files))
    destination = tmp_path / "destination"

    with pytest.raises(ValueError, match="packaged payload manifest"):
        write_package_payload(hostile, destination)

    assert not destination.exists()


def test_copy_refuses_user_owned_destination_without_deleting_files(tmp_path: Path):
    destination = tmp_path / "destination"
    destination.mkdir()
    user_file = destination / "mine.txt"
    user_file.write_bytes(b"keep me")

    with pytest.raises(FileExistsError, match="destination"):
        write_package_payload(foundation_manifest(), destination)

    assert user_file.read_bytes() == b"keep me"


def test_copy_refuses_symlinked_destination_ancestor_without_writing_through_it(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        write_package_payload(foundation_manifest(), alias / "destination")

    assert tuple(outside.iterdir()) == ()


def test_clean_installed_wheel_reads_validates_copies_and_installs_without_source_checkout(tmp_path: Path):
    outdir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir)],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wheel = next(outdir.glob("*.whl"))
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    scratch = tmp_path / "outside-source-checkout"
    scratch.mkdir()
    script = """
import json
from pathlib import Path
import hermes_workflows
from hermes_workflows.package_resources import foundation_manifest, write_package_payload
from hermes_workflows.plugin_install import install_plugin, inspect_payload

root = Path.cwd()
manifest = foundation_manifest()
written = write_package_payload(manifest, root / "copied")
descriptor = inspect_payload()
report = install_plugin(root / "profile")
print(json.dumps({
    "package_file": hermes_workflows.__file__,
    "manifest_paths": [entry.path for entry in manifest.files],
    "written": [path.relative_to(root / "copied").as_posix() for path in written],
    "inspected": list(descriptor.files),
    "installed": list(report.files),
}, sort_keys=True))
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(python), "-c", script],
        cwd=scratch,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    receipt = json.loads(completed.stdout)

    assert str(REPO_ROOT) not in receipt["package_file"]
    assert receipt["manifest_paths"] == receipt["written"]
    assert [path.split("hermes-workflows-approvals/", 1)[1] for path in receipt["manifest_paths"]] == receipt["inspected"]
    assert receipt["inspected"] == receipt["installed"]

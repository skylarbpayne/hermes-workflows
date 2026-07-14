from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_workflows.installed_environment import installed_environment_report, resolve_installed_execution


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "install_smoke_registry_v2.json"
PROBE = REPO_ROOT / "tests" / "probes" / "installed_cli_smoke.py"
FORBIDDEN_BYPASSES = ("_run-engine", "--direct", "HERMES_WORKFLOWS_UV_CHILD")
FIXTURE_SHA256 = "aad6a8614967b60ebdd8fd349cbc2612c8fd8063ae613186a96c1d4b3f4417ee"


def test_resolver_retains_exact_interpreter_and_environment_without_running_path_uv(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "fake-uv-called"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(f"#!/bin/sh\ntouch {marker}\nexit 97\n", encoding="utf-8")
    fake_uv.chmod(0o755)
    environment = {
        "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))),
        "VIRTUAL_ENV": str(tmp_path / "clean-venv"),
        "CUSTOM_SENTINEL": "retained",
    }

    resolved = resolve_installed_execution(environ=environment)

    assert resolved.python_executable == sys.executable
    assert resolved.environment == environment
    assert resolved.environment is not environment
    assert resolved.environment["VIRTUAL_ENV"] == environment["VIRTUAL_ENV"]
    assert resolved.visible_uv == str(fake_uv)
    assert not marker.exists()


def test_identity_reports_package_origin_registry_fingerprint_manifest_and_db_alias(tmp_path):
    registry = tmp_path / "workflows.registry.json"
    registry_bytes = b'{"db":"smoke-db","schema_version":2}\n'
    registry.write_bytes(registry_bytes)

    report = installed_environment_report(registry_path=registry, db_alias="smoke-db").to_dict()

    assert report["schema_version"] == 1
    assert report["python_executable"] == sys.executable
    assert Path(report["package_origin"]).is_file()
    assert "hermes_workflows" in Path(report["package_origin"]).parts
    assert report["package_version"]
    assert report["registry_path"] == str(registry.resolve())
    assert report["registry_sha256"] == hashlib.sha256(registry_bytes).hexdigest()
    assert report["package_manifest_sha256"] == report["package_ownership_key"]
    assert report["db_alias"] == "smoke-db"
    assert "environment" not in report


def test_fixture_has_reviewed_byte_identity():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_clean_wheel_probe_reaches_typed_wait_under_installed_interpreter_without_uv(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--work-root", str(tmp_path / "probe")],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["status"] == "waiting"
    assert payload["waiting_on"] == "signal:operator.response:review_release_note"
    assert payload["python_executable"] == payload["installed_cli_executable"]
    assert payload["installed_cli_shebang"] == "#!" + payload["python_executable"]
    assert Path(payload["installed_cli_path"]).name == "hermes-workflows"
    assert payload["interpreter_under_venv"] is True
    assert payload["package_origin_under_venv"] is True
    assert payload["db_alias"] == "smoke-db"
    assert payload["registry_sha256"] == FIXTURE_SHA256
    assert payload["fixture_sha256"] == FIXTURE_SHA256
    assert payload["fake_uv_visible"] is True
    assert payload["fake_uv_called"] is False
    assert payload["hidden_bypasses_used"] == []
    assert completed.stderr == ""


def test_contract_and_probe_do_not_use_hidden_run_bypasses():
    source_paths = (
        REPO_ROOT / "src" / "hermes_workflows" / "installed_environment.py",
        PROBE,
    )
    for path in source_paths:
        source = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_BYPASSES:
            assert token not in source, f"{path.relative_to(REPO_ROOT)} must not use {token}"

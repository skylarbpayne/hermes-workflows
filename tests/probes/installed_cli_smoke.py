from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "install_smoke_registry_v2.json"
WORKFLOW_ID = "wf_installed_cli_smoke"
EXPECTED_WAIT = "signal:operator.response:review_release_note"


def _run(command: List[str], *, cwd: Path, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _venv_executable(venv: Path, name: str) -> Path:
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    return scripts / f"{name}{suffix}"


def _is_beneath(path: Path, parent: Path) -> bool:
    absolute_path = Path(os.path.abspath(str(path)))
    absolute_parent = Path(os.path.abspath(str(parent)))
    candidates = (
        (absolute_path, absolute_parent),
        (absolute_path.resolve(), absolute_parent.resolve()),
    )
    for candidate, candidate_parent in candidates:
        try:
            candidate.relative_to(candidate_parent)
        except ValueError:
            continue
        return True
    return False


def _identity_command(python: Path, registry: Path, db_alias: str) -> List[str]:
    program = (
        "import json; "
        "from hermes_workflows.installed_environment import installed_environment_report; "
        "print(json.dumps(installed_environment_report("
        "registry_path=" + repr(str(registry)) + ",db_alias=" + repr(db_alias) + ").to_dict(),sort_keys=True))"
    )
    return [str(python), "-c", program]


def run_probe(work_root: Path) -> Dict[str, Any]:
    work_root.mkdir(parents=True, exist_ok=False)
    dist = work_root / "dist"
    dist.mkdir()
    build = _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=REPO_ROOT,
    )
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one built wheel, found {len(wheels)}")
    wheel = wheels[0]

    venv = work_root / "venv"
    _run([sys.executable, "-m", "venv", str(venv)], cwd=work_root)
    python = _venv_executable(venv, "python")
    installed_cli = _venv_executable(venv, "hermes-workflows")
    install = _run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", str(wheel)],
        cwd=work_root,
    )

    fake_bin = work_root / "fake-bin"
    fake_bin.mkdir()
    marker = work_root / "fake-uv-called"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\nexit 97\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    workspace = work_root / "workspace"
    registry = workspace / ".hermes" / "workflows.registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_bytes(FIXTURE.read_bytes())
    fixture = json.loads(registry.read_text(encoding="utf-8"))
    db_path = registry.parent / fixture["db"]["relative_path"]
    db_path.parent.mkdir(parents=True)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PATH"] = os.pathsep.join((str(fake_bin), env.get("PATH", "")))
    env["VIRTUAL_ENV"] = str(venv)
    input_json = json.dumps(fixture["input"], sort_keys=True, separators=(",", ":"))

    started = _run(
        [
            str(python),
            "-m",
            "hermes_workflows.examples.install_smoke",
            "--db",
            str(db_path),
            "--id",
            WORKFLOW_ID,
            "--input-json",
            input_json,
        ],
        cwd=workspace,
        env=env,
    )
    worker = _run(
        [
            str(installed_cli),
            "worker",
            fixture["workflow"]["workflow_ref"],
            "--db",
            str(db_path),
            "--id",
            WORKFLOW_ID,
            "--max-commands",
            "10",
        ],
        cwd=workspace,
        env=env,
    )
    status = _run(
        [
            str(installed_cli),
            "status",
            "--db",
            str(db_path),
            "--id",
            WORKFLOW_ID,
        ],
        cwd=workspace,
        env=env,
    )
    identity = _run(
        _identity_command(python, registry, fixture["db"]["alias"]),
        cwd=workspace,
        env=env,
    )

    started_payload = json.loads(started.stdout)
    worker_payload = json.loads(worker.stdout)
    status_payload = json.loads(status.stdout)
    identity_payload = json.loads(identity.stdout)
    if started_payload["status"] != "running":
        raise RuntimeError(f"unexpected start status: {started_payload}")
    if worker_payload["status"] != "waiting" or worker_payload["waiting_on"] != EXPECTED_WAIT:
        raise RuntimeError(f"typed wait was not reached: {worker_payload}")
    if status_payload["status"] != "waiting" or status_payload["waiting_on"] != EXPECTED_WAIT:
        raise RuntimeError(f"status did not preserve typed wait: {status_payload}")

    package_origin = Path(identity_payload["package_origin"])
    cli_shebang = installed_cli.read_text(encoding="utf-8").splitlines()[0]
    expected_shebang = "#!" + str(python)
    if cli_shebang != expected_shebang:
        raise RuntimeError(f"installed console script uses an unexpected interpreter: {cli_shebang!r}")
    result = {
        **identity_payload,
        "status": status_payload["status"],
        "waiting_on": status_payload["waiting_on"],
        "installed_cli_executable": str(python),
        "installed_cli_path": str(installed_cli),
        "installed_cli_shebang": cli_shebang,
        "interpreter_under_venv": _is_beneath(Path(identity_payload["python_executable"]), venv),
        "package_origin_under_venv": _is_beneath(package_origin, venv),
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "fake_uv_visible": identity_payload["visible_uv"] == str(fake_uv),
        "fake_uv_called": marker.exists(),
        "hidden_bypasses_used": [],
        "commands": {
            "build": build.args,
            "install": install.args,
            "start": started.args,
            "worker": worker.args,
            "status": status.args,
        },
    }
    if result["fake_uv_called"]:
        raise RuntimeError("the unrelated uv executable was invoked")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args(argv)
    if args.work_root is not None:
        payload = run_probe(args.work_root)
    else:
        with tempfile.TemporaryDirectory(prefix="hermes-workflows-installed-") as temp_dir:
            payload = run_probe(Path(temp_dir) / "probe")
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

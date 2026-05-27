import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_github_actions_runs_declared_python_floor():
    """CI must exercise the package's declared Python 3.9 compatibility floor."""

    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")

    assert "3.9" in workflow


def test_declared_python39_support_imports_package_under_python39():
    """The project advertises Python 3.9 support, so import-time type aliases must work there."""

    if sys.version_info[:2] != (3, 9):
        pytest.skip(f"current interpreter is {sys.version_info.major}.{sys.version_info.minor}, not 3.9")

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}:{env.get('PYTHONPATH', '')}"
    result = subprocess.run(
        [sys.executable, "-c", "import hermes_workflows; print(hermes_workflows.__name__)"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hermes_workflows"

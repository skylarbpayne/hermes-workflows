import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_declared_python39_support_imports_package_under_system_python39():
    """The project advertises Python 3.9 support, so import-time type aliases must work there."""

    python = shutil.which("python3")
    if python is None:
        pytest.skip("no system python3 available for compatibility smoke")

    version = subprocess.run(
        [python, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    if version != "3.9":
        pytest.skip(f"system python3 is {version}, not 3.9")

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}:{env.get('PYTHONPATH', '')}"
    result = subprocess.run(
        [python, "-c", "import hermes_workflows; print(hermes_workflows.__name__)"],
        cwd=Path.cwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hermes_workflows"

import inspect
from pathlib import Path

from hermes_workflows import cli

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


def test_pyproject_declares_uv_default_dev_group_for_plain_uv_run_pytest():
    data = tomllib.loads(Path('pyproject.toml').read_text())
    dev_group = data.get('dependency-groups', {}).get('dev', [])
    assert any(str(dep).startswith('pytest') for dep in dev_group), (
        'plain `uv run pytest` must install/use the project pytest, not leak to an active external venv PATH pytest'
    )


def test_public_run_uses_installed_environment_without_uv_trampoline_or_hidden_bypass():
    installed_run_source = inspect.getsource(cli.run_installed_cli)
    cli_source = Path(cli.__file__).read_text()

    assert "resolve_installed_execution()" in installed_run_source
    for obsolete_token in (
        "run_via_uv",
        "_run-engine",
        "--direct",
        "HERMES_WORKFLOWS_UV_CHILD",
        'shutil.which("uv")',
        '"uv", "run", "python"',
    ):
        assert obsolete_token not in cli_source

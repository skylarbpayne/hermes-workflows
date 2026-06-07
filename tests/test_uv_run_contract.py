from pathlib import Path

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

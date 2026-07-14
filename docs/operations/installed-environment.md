# Installed environment contract

The `hermes-workflows` console script belongs to the Python environment that installed the wheel. Normal workflow execution must retain that process's `sys.executable`, `VIRTUAL_ENV`, and environment. Discovering an unrelated `uv` on `PATH` is diagnostic only; it must not cause project discovery, a subprocess trampoline, or environment mutation.

`hermes_workflows.installed_environment.resolve_installed_execution()` captures this contract without executing anything found on `PATH`. `installed_environment_report()` emits the non-secret identity needed for operations:

- exact Python executable and virtual environment;
- installed package origin and version;
- foundation package-manifest SHA-256 / ownership key;
- absolute registry path and byte fingerprint;
- configured DB alias; and
- the visible `uv` path, if any, as evidence that its presence does not control execution.

The report deliberately omits the full environment because it may contain credentials. It reports the DB alias, not registry-v2 catalog semantics.

## Verification

Run:

    uv run pytest -q tests/test_installed_cli_environment.py
    python tests/probes/installed_cli_smoke.py

The probe builds a wheel, installs it without an editable checkout into a new virtual environment, puts a marker-writing fake `uv` first on `PATH`, and exercises the packaged typed quickstart through the installed interpreter and console script. Success means the workflow reaches `signal:operator.response:review_release_note`, both the interpreter and package origin are beneath the temporary virtual environment, identity fingerprints are present, and the fake marker is absent.

The probe may not use private parser switches, private engine commands, or child-marker environment variables to evade the public execution contract. Central CLI deletion and wiring remain owned by INT-C1; this module is the isolated resolver and evidence seam that integration consumes.

## Rollback

Rollback may remove this resolver only together with its unconsumed integration. It must not restore implicit `uv` execution or environment stripping. A project-specific environment runner, if ever needed, must be a separate explicit command and contract.

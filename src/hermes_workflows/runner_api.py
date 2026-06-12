from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from .engine import JsonCodec, RunResult, WorkflowEngine


def infer_project_root(explicit: str | Path | None = None, *, start: str | Path | None = None) -> Path:
    """Return the project root used for default workflow state.

    The root is deliberately boring and local: an explicit --project-root wins;
    otherwise walk upward from cwd/start until a normal project marker appears.
    """

    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    current = Path(start or Path.cwd()).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in ("pyproject.toml", ".git", ".hermes/workflows.registry.json")):
            return candidate
    return current


def default_db_path(project_root: str | Path | None = None) -> Path:
    return infer_project_root(project_root) / ".hermes" / "workflows.sqlite"


def default_workflow_id(workflow_ref: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", workflow_ref).strip("-._:") or "workflow"
    digest = hashlib.sha256(workflow_ref.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:64]}-{digest}"


def run_workflow_function(
    workflow_fn: Callable[..., Any],
    *,
    input_payload: Any | None = None,
    db: str | Path | None = None,
    workflow_id: str | None = None,
    workflow_ref: str | None = None,
    project_root: str | Path | None = None,
    drain: bool = False,
) -> RunResult:
    ref = workflow_ref or getattr(workflow_fn, "__workflow_name__", getattr(workflow_fn, "__name__", "workflow"))
    resolved_db = Path(db).expanduser() if db is not None else default_db_path(project_root)
    resolved_id = workflow_id or default_workflow_id(ref)
    payload = {} if input_payload is None else input_payload
    engine = WorkflowEngine(resolved_db)
    if drain:
        return engine.run_until_idle(workflow_fn, payload, workflow_id=resolved_id, workflow_ref=ref)
    return engine.start(workflow_fn, payload, workflow_id=resolved_id, workflow_ref=ref)


def run_result_payload(result: RunResult) -> dict[str, Any]:
    return {
        "workflow_id": result.workflow_id,
        "status": result.status,
        "waiting_on": result.waiting_on,
        "result": result.result,
        "error": result.error,
    }


def workflow_run_cli(workflow_fn: Callable[..., Any], argv: list[str] | None = None, *, workflow_ref: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{getattr(workflow_fn, '__name__', 'workflow')}.run")
    parser.add_argument("--db", type=Path, help="Workflow SQLite DB. Defaults to <project-root>/.hermes/workflows.sqlite")
    parser.add_argument("--project-root", type=Path, help="Root used for default DB discovery")
    parser.add_argument("--id", dest="workflow_id", help="Workflow instance id. Defaults to a stable id derived from the workflow ref")
    parser.add_argument("--input-json", default="{}", help="JSON object/value passed to the workflow. Defaults to {}")
    parser.add_argument("--no-drain", action="store_true", help="Deprecated no-op; runs always enqueue workflow work for workers")
    args = parser.parse_args(argv)
    resolved_ref = workflow_ref
    module = sys.modules.get(getattr(workflow_fn, "__module__", ""))
    module_file = getattr(module, "__file__", None)
    if module_file:
        resolved_ref = f"{Path(module_file).expanduser().resolve()}:{getattr(workflow_fn, '__name__', getattr(workflow_fn, '__workflow_name__', 'workflow'))}"
    project_root = args.project_root
    if project_root is None and module_file:
        project_root = infer_project_root(start=module_file)
    result = run_workflow_function(
        workflow_fn,
        input_payload=json.loads(args.input_json),
        db=args.db,
        workflow_id=args.workflow_id,
        workflow_ref=resolved_ref,
        project_root=project_root,
        drain=False,
    )
    print(JsonCodec.dumps(run_result_payload(result)))
    return 0

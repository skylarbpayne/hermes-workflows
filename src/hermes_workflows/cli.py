from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .agent_runner import build_agent_runner
from .approvals import ApprovalDecisionInput
from .dashboard import render_dashboard
from .dashboard_server import serve_dashboard
from .engine import JsonCodec, RunResult, WorkflowEngine
from .invocation import InvocationService, TrustedResumer
from .registry import WorkflowRegistry, looks_like_path
from .runner_api import default_db_path, default_workflow_id, infer_project_root, run_workflow_function
from .worker_service import WorkflowWorkerService
from .workflow_loading import canonical_workflow_ref, discover_workflow_refs, load_workflow_ref, resolve_discovered_workflow


def load_workflow(ref: str) -> Callable[..., Any]:
    return load_workflow_ref(ref)


def result_payload(result: RunResult) -> dict[str, Any]:
    return {
        "workflow_id": result.workflow_id,
        "status": result.status,
        "waiting_on": result.waiting_on,
        "result": result.result,
        "error": result.error,
    }


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def add_agent_runner_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent-command", help="Provider command for agent(...) jobs; also reads HERMES_WORKFLOWS_AGENT_COMMAND")
    parser.add_argument("--agent-arg", action="append", default=[], help="Argument passed to --agent-command; repeat for multiple args")
    parser.add_argument(
        "--agent-model-arg",
        action="append",
        default=[],
        help=(
            "Argument template appended to --agent-command only when agent(..., model=...) is set; "
            "repeat for multiple args, using {model} as the placeholder. Also reads "
            "HERMES_WORKFLOWS_AGENT_MODEL_ARGS_JSON."
        ),
    )
    parser.add_argument("--agent-timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--agent-request-stdin",
        choices=["prompt", "json"],
        default=None,
        help="What the worker's configured agent command receives on stdin: rendered prompt (default) or raw agent.runner_request.v1 JSON.",
    )
    parser.add_argument("--max-agent-stdout-bytes", type=positive_int, default=1_000_000)
    parser.add_argument("--max-agent-stderr-bytes", type=positive_int, default=4096)


def _load_string_list_env(name: str) -> list[str]:
    loaded = json.loads(os.environ[name])
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise SystemExit(f"{name} must be a JSON array of strings")
    return loaded


def agent_runner_from_args(args: argparse.Namespace):
    command = getattr(args, "agent_command", None) or os.environ.get("HERMES_WORKFLOWS_AGENT_COMMAND")
    agent_args = list(getattr(args, "agent_arg", []) or [])
    if not agent_args and os.environ.get("HERMES_WORKFLOWS_AGENT_ARGS_JSON"):
        agent_args = _load_string_list_env("HERMES_WORKFLOWS_AGENT_ARGS_JSON")
    agent_model_args = list(getattr(args, "agent_model_arg", []) or [])
    if not agent_model_args and os.environ.get("HERMES_WORKFLOWS_AGENT_MODEL_ARGS_JSON"):
        agent_model_args = _load_string_list_env("HERMES_WORKFLOWS_AGENT_MODEL_ARGS_JSON")
    agent_request_stdin = getattr(args, "agent_request_stdin", None) or os.environ.get("HERMES_WORKFLOWS_AGENT_REQUEST_STDIN") or "prompt"
    if not command:
        return None
    return build_agent_runner(
        agent_command=command,
        agent_args=agent_args,
        agent_model_args=agent_model_args,
        agent_request_stdin=agent_request_stdin,
        timeout_seconds=float(getattr(args, "agent_timeout_seconds", 120.0)),
        max_stdout_bytes=int(getattr(args, "max_agent_stdout_bytes", 1_000_000)),
        max_stderr_bytes=int(getattr(args, "max_agent_stderr_bytes", 4096)),
    )


def normalize_agent_value_options(argv: list[str]) -> list[str]:
    """Allow provider argv values like `--model` after repeatable agent options."""

    normalized: list[str] = []
    index = 0
    while index < len(argv):
        item = str(argv[index])
        if item in {"--agent-arg", "--agent-model-arg"} and index + 1 < len(argv):
            normalized.append(f"{item}={argv[index + 1]}")
            index += 2
            continue
        normalized.append(item)
        index += 1
    return normalized


def print_json(payload: Any) -> None:
    print(JsonCodec.dumps(payload))


def maybe_write_json(path: Path | None, payload: Any) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(JsonCodec.dumps(payload) + "\n")


def human_source_from_args(args: argparse.Namespace) -> dict[str, str]:
    source = {"kind": "human", "id": args.by, "channel": args.channel}
    if args.message_url:
        source["message_url"] = args.message_url
    if args.message_id:
        source["message_id"] = args.message_id
    if args.event_id:
        source["event_id"] = args.event_id
    if not any(key in source for key in ("message_url", "message_id", "event_id")):
        raise SystemExit("approval shortcuts require --message-url, --message-id, or --event-id for provenance")
    return source


def approval_payload_from_args(args: argparse.Namespace, action: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": action, "by": args.by}
    if args.note:
        payload["note"] = args.note
    if getattr(args, "reason", None):
        payload["reason"] = args.reason
    return payload


def run_doctor(args: argparse.Namespace) -> int:
    import sqlite3

    checks = {
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "db_exists": args.db.exists() if args.db else None,
        "db_parent_writable": args.db.parent.exists() and args.db.parent.is_dir() if args.db else None,
    }
    if args.workflow_ref:
        try:
            load_workflow(args.workflow_ref)
        except Exception as exc:  # pragma: no cover - exact import errors are environment-specific.
            checks["workflow_ref_importable"] = False
            checks["workflow_ref_error"] = str(exc)
        else:
            checks["workflow_ref_importable"] = True
    checks["ok"] = bool(
        checks["python"]
        and checks["sqlite"]
        and checks["db_parent_writable"]
        and checks.get("workflow_ref_importable", True)
    )
    print_json({"doctor": checks})
    return 0


def _uv_cwd_for_run(args: argparse.Namespace) -> Path:
    if args.project_root is not None:
        return infer_project_root(args.project_root)
    if args.config is not None:
        return infer_project_root(start=args.config)
    ref = str(args.workflow_ref)
    path_candidate = ref.rsplit(":", 1)[0] if ":" in ref and ref.rsplit(":", 1)[0].endswith(".py") else ref
    path = Path(path_candidate).expanduser()
    if path.suffix == ".py" or path.exists():
        return infer_project_root(start=path)
    return Path.cwd()


def run_via_uv(raw_argv: list[str], args: argparse.Namespace) -> int:
    uv = shutil.which("uv")
    if uv is None:
        # Keep the CLI usable in minimal environments; tests and normal installs
        # with uv still exercise the blessed uv path.
        return main(["_run-engine", *raw_argv[1:]])
    child_args = ["_run-engine", *raw_argv[1:]]
    cmd = [uv, "run", "python", "-m", "hermes_workflows", *child_args]
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["HERMES_WORKFLOWS_UV_CHILD"] = "1"
    completed = subprocess.run(cmd, env=env, cwd=_uv_cwd_for_run(args))
    return int(completed.returncode)


def resolve_run_invocation(args: argparse.Namespace) -> tuple[Callable[..., Any], str, Path, str, Any]:
    config_path = args.config
    if args.project_root is not None:
        project_root = infer_project_root(args.project_root)
    elif config_path is not None:
        project_root = infer_project_root(start=config_path)
    else:
        project_root = infer_project_root()
    default_registry = project_root / ".hermes" / "workflows.registry.json"
    if config_path is None and default_registry.exists():
        config_path = default_registry
    registry_obj = WorkflowRegistry.from_sources(config_path=config_path)
    workflow_config = None
    workflow_ref = args.workflow_ref
    default_input: Any = {}

    try:
        resolved_config = registry_obj.resolve_workflow(args.workflow_ref)
    except ValueError:
        discovered = resolve_discovered_workflow(args.workflow_ref, project_root=project_root)
        if discovered is not None:
            workflow_ref = discovered
        elif looks_like_path(args.workflow_ref) or args.workflow_ref.endswith(".py") or ":" in args.workflow_ref:
            workflow_ref = args.workflow_ref
        else:
            raise SystemExit(f"Unknown workflow alias or path {args.workflow_ref!r}")
    else:
        if args.workflow_ref in registry_obj.workflows:
            workflow_config = resolved_config
            workflow_ref = workflow_config.workflow_ref
            default_input = dict(workflow_config.default_input)
        else:
            workflow_ref = resolved_config.workflow_ref

    workflow = load_workflow(workflow_ref)
    workflow_ref = canonical_workflow_ref(workflow_ref, workflow)
    module = sys.modules.get(getattr(workflow, "__module__", ""))
    module_file = getattr(module, "__file__", None)
    db_project_root = project_root
    if args.project_root is None and workflow_config is None:
        ref_path = Path(workflow_ref.split(":", 1)[0]).expanduser()
        if ref_path.suffix == ".py" or ref_path.exists():
            db_project_root = infer_project_root(start=ref_path)
        elif module_file:
            db_project_root = infer_project_root(start=module_file)

    if args.db:
        try:
            db_path = Path(registry_obj.resolve_db(args.db).path)
        except ValueError:
            if looks_like_path(args.db):
                db_path = Path(args.db).expanduser()
            else:
                raise
    elif workflow_config is not None and workflow_config.db:
        db_path = Path(registry_obj.resolve_db(workflow_config.db).path)
    else:
        db_path = default_db_path(db_project_root)

    input_payload = default_input
    if args.input_json is not None:
        loaded_input = json.loads(args.input_json)
        if isinstance(input_payload, dict) and isinstance(loaded_input, dict):
            input_payload = {**input_payload, **loaded_input}
        else:
            input_payload = loaded_input
    if workflow_config is not None and isinstance(input_payload, dict):
        input_payload.setdefault("_registry_name", workflow_config.name)

    workflow_id = args.workflow_id or default_workflow_id(workflow_ref)
    return workflow, workflow_ref, db_path, workflow_id, input_payload


def run_engine_cli(args: argparse.Namespace) -> int:
    workflow, workflow_ref, db_path, workflow_id, input_payload = resolve_run_invocation(args)
    drain = False
    result = run_workflow_function(
        workflow,
        input_payload=input_payload,
        db=db_path,
        workflow_id=workflow_id,
        workflow_ref=workflow_ref,
        drain=drain,
    )
    resumes = 0
    while args.watch and result.status not in {"completed", "failed", "cancelled"}:
        if args.max_resumes is not None and resumes >= args.max_resumes:
            break
        time.sleep(args.poll_interval)
        engine = WorkflowEngine(db_path)
        if drain:
            result = engine.resume(workflow, workflow_id)
        else:
            result = engine.start(workflow, input_payload, workflow_id=workflow_id, workflow_ref=workflow_ref)
        resumes += 1
    print_json(result_payload(result))
    return 0


def run_worker_registry_cli(args: argparse.Namespace) -> int:
    registry_obj = WorkflowRegistry.from_sources(config_path=args.config)
    try:
        service = WorkflowWorkerService.from_registry(
            registry_obj,
            db=args.db,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            agent_runner=agent_runner_from_args(args),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.once:
        result = service.serve(poll_interval=args.poll_interval, max_commands=1, idle_exit_after=0)
    else:
        result = service.serve(
            poll_interval=args.poll_interval,
            max_commands=args.max_commands,
            idle_exit_after=args.idle_exit_after,
        )
    print_json(result.to_payload())
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    argv = normalize_agent_value_options(raw_argv)
    parser = argparse.ArgumentParser(prog="hermes-workflows")
    visible_commands = (
        "{registry,invoke,resume-trusted,resume-pending,start,run,worker,signal,"
        "reconcile-child,reconcile-children,cancel,status,list,events,outbox,"
        "dashboard,serve-dashboard,doctor,approve,reject}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar=visible_commands)

    registry = sub.add_parser("registry", help="Inspect workflow registry aliases")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_list = registry_sub.add_parser("list", help="List configured DB and workflow aliases")
    registry_list.add_argument("--config", type=Path)
    registry_doctor = registry_sub.add_parser("doctor", help="Validate configured workflow refs and DB aliases")
    registry_doctor.add_argument("--config", type=Path)
    registry_discover = registry_sub.add_parser("discover", help="Crawl a project for @workflow files")
    registry_discover.add_argument("--project-root", type=Path)

    invoke = sub.add_parser("invoke", help="Registry-aware workflow invocation with receipt output")
    invoke.add_argument("workflow", help="workflow alias or module:function ref")
    invoke.add_argument("--config", type=Path)
    invoke.add_argument("--db", help="configured DB alias or explicit local DB path")
    invoke.add_argument("--id", required=True, dest="workflow_id")
    invoke.add_argument("--input-json")
    invoke.add_argument("--source-json")
    invoke.add_argument("--receipt-json", type=Path)
    invoke.add_argument("--dashboard-out", type=Path)

    resume_trusted = sub.add_parser("resume-trusted", help="Resume one workflow after a record-only approval decision")
    resume_trusted.add_argument("workflow", help="workflow alias or module:function ref")
    resume_trusted.add_argument("--config", type=Path)
    resume_trusted.add_argument("--db", help="configured DB alias or explicit local DB path")
    resume_trusted.add_argument("--id", required=True, dest="workflow_id")
    resume_trusted.add_argument("--receipt-json", type=Path)
    resume_trusted.add_argument("--dashboard-out", type=Path)

    resume_pending = sub.add_parser("resume-pending", help="Resume allowlisted pending workflows with recorded decisions")
    resume_pending.add_argument("--config", type=Path)
    resume_pending.add_argument("--db", help="configured DB alias or explicit local DB path")
    resume_pending.add_argument("--registry-name", required=True)
    resume_pending.add_argument("--limit", type=positive_int, default=10)
    resume_pending.add_argument("--receipt-json", type=Path)

    start = sub.add_parser("start", help="Start/replay a workflow decider without draining step commands")
    start.add_argument("workflow_ref", help="module:function")
    start.add_argument("--db", required=True, type=Path)
    start.add_argument("--id", required=True, dest="workflow_id")
    start.add_argument("--input-json", required=True)

    run = sub.add_parser("run", help="Run or resume a workflow through uv using a registry name, module ref, or file path")
    run.add_argument("workflow_ref", help="registry alias, module:function, workflow.py, or workflow.py:function")
    run.add_argument("--config", type=Path)
    run.add_argument("--db", help="configured DB alias or explicit local DB path; defaults to <project-root>/.hermes/workflows.sqlite")
    run.add_argument("--id", dest="workflow_id", help="workflow instance id; defaults to a stable id derived from the workflow ref")
    run.add_argument("--input-json", default="{}")
    run.add_argument("--project-root", type=Path, help="Root used for registry discovery and the default DB")
    run.add_argument("--no-drain", action="store_true", help="Deprecated no-op; run always enqueues workflow work for workers")
    run.add_argument("--watch", action="store_true", help="Keep re-invoking the same workflow entrypoint/DB until terminal or --max-resumes")
    run.add_argument("--poll-interval", type=float, default=1.0)
    run.add_argument("--max-resumes", type=positive_int)
    run.add_argument("--direct", action="store_true", help=argparse.SUPPRESS)

    run_engine = sub.add_parser("_run-engine", help=argparse.SUPPRESS)
    run_engine.add_argument("workflow_ref", help=argparse.SUPPRESS)
    run_engine.add_argument("--config", type=Path)
    run_engine.add_argument("--db")
    run_engine.add_argument("--id", dest="workflow_id")
    run_engine.add_argument("--input-json", default="{}")
    run_engine.add_argument("--project-root", type=Path)
    run_engine.add_argument("--no-drain", action="store_true")
    run_engine.add_argument("--watch", action="store_true")
    run_engine.add_argument("--poll-interval", type=float, default=1.0)
    run_engine.add_argument("--max-resumes", type=positive_int)
    sub._choices_actions = [action for action in sub._choices_actions if action.dest != "_run-engine"]

    worker = sub.add_parser("worker", help="Run the Workflow Worker in resident or scoped mode")
    worker.add_argument("workflow_ref", nargs="?", help="scoped debug/recovery mode: module:function to drain one workflow id")
    worker.add_argument("--config", type=Path, help="resident mode: workflow registry/config to scan for runnable work")
    worker.add_argument("--db", help="configured DB alias, explicit local DB path, or scoped-mode DB path")
    worker.add_argument("--id", dest="workflow_id", help="scoped debug/recovery mode: workflow instance id")
    worker.add_argument("--worker-id", default="workflow-worker")
    worker.add_argument("--lease-seconds", type=int, default=30)
    worker.add_argument("--poll-interval", type=float, default=1.0, help="resident mode: sleep interval while waiting for work")
    worker.add_argument("--once", action="store_true", help="Execute at most one command")
    worker.add_argument("--max-commands", type=positive_int, help="Exit after executing this many commands")
    worker.add_argument(
        "--idle-exit-after",
        type=float,
        help="resident mode: exit after this many idle seconds; omit for an always-on process",
    )
    add_agent_runner_args(worker)

    signal = sub.add_parser("signal", help="Record a signal and enqueue workflow continuation")
    signal.add_argument("workflow_ref", help="module:function; imported so the decider is registered")
    signal.add_argument("--db", required=True, type=Path)
    signal.add_argument("--id", required=True, dest="workflow_id")
    signal.add_argument("--type", required=True, dest="signal_type")
    signal.add_argument("--key", required=True)
    signal.add_argument("--payload-json", required=True)
    signal.add_argument("--source-json")
    signal.add_argument("--idempotency-key")

    reconcile_child = sub.add_parser("reconcile-child", help="Reconcile one requested child workflow result into its parent")
    reconcile_child.add_argument("workflow_ref", help="module:function; imported so the parent decider is registered")
    reconcile_child.add_argument("--db", required=True, type=Path)
    reconcile_child.add_argument("--id", required=True, dest="workflow_id")
    reconcile_child.add_argument("--child-key", required=True)

    reconcile_children = sub.add_parser("reconcile-children", help="Reconcile all pending child workflow results into a parent")
    reconcile_children.add_argument("workflow_ref", help="module:function; imported so the parent decider is registered")
    reconcile_children.add_argument("--db", required=True, type=Path)
    reconcile_children.add_argument("--id", required=True, dest="workflow_id")

    cancel = sub.add_parser("cancel", help="Cancel a workflow instance while preserving audit history")
    cancel.add_argument("--db", required=True, type=Path)
    cancel.add_argument("--id", required=True, dest="workflow_id")
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--source-json")
    cancel.add_argument("--superseded-by")

    status = sub.add_parser("status", help="Inspect one workflow instance without replaying it")
    status.add_argument("--db", required=True, type=Path)
    status.add_argument("--id", required=True, dest="workflow_id")
    status.add_argument("--recent-events", type=int, default=20)
    status.add_argument("--commands", choices=["failed", "recent", "all"], help="Include bounded command history in the status packet")
    status.add_argument("--command-limit", type=positive_int, default=20, help="Maximum command-history rows to include")
    status.add_argument("--command-payload-chars", type=positive_int, default=500, help="Maximum serialized payload preview chars per command-history row")

    list_cmd = sub.add_parser("list", help="List workflow instances in a workflow DB")
    list_cmd.add_argument("--db", required=True, type=Path)
    list_cmd.add_argument("--status", help="Only include workflow instances with this status")

    events = sub.add_parser("events", help="Inspect one workflow instance's event log without replaying it")
    events.add_argument("--db", required=True, type=Path)
    events.add_argument("--id", required=True, dest="workflow_id")
    events.add_argument("--limit", type=positive_int, help="Return only the most recent N events")

    outbox = sub.add_parser("outbox", help="Inspect workflow command outbox rows without replaying workflows")
    outbox.add_argument("--db", required=True, type=Path)
    outbox.add_argument("--id", dest="workflow_id", help="Only include commands for this workflow id")
    outbox.add_argument("--status", help="Only include commands with this status")

    dashboard = sub.add_parser("dashboard", help="Render a read-only local HTML workflow dashboard")
    dashboard.add_argument("--db", required=True, type=Path)
    dashboard.add_argument("--out", required=True, type=Path)
    dashboard.add_argument("--status", help="Only include workflow instances with this status")
    dashboard.add_argument("--recent-events", type=positive_int, default=5)

    serve_dashboard_cmd = sub.add_parser(
        "serve-dashboard",
        help="Serve a read-only local workflow dashboard; approval POST forms require --enable-approval-actions",
    )
    serve_dashboard_cmd.add_argument("workflow_ref", help="module:function; imported so explicit approval actions can resume the workflow")
    serve_dashboard_cmd.add_argument("--db", required=True, type=Path)
    serve_dashboard_cmd.add_argument("--host", default="127.0.0.1")
    serve_dashboard_cmd.add_argument("--port", type=int, default=8765)
    serve_dashboard_cmd.add_argument("--once", action="store_true", help="Stop after one approval POST; useful for tests/smokes")
    serve_dashboard_cmd.add_argument(
        "--enable-approval-actions",
        action="store_true",
        help="Enable local /approve POST forms; omitted by default so serve-dashboard stays read-only.",
    )

    doctor = sub.add_parser("doctor", help="Check local install, SQLite, DB path, and optional workflow import")
    doctor.add_argument("--db", type=Path, default=Path(".hermes/workflows.sqlite"))
    doctor.add_argument("--workflow-ref", help="Optional module:function import smoke")

    for action_name in ("approve", "reject"):
        approval = sub.add_parser(action_name, help=f"Send a human-provenance {action_name} decision to an approval gate")
        approval.add_argument("workflow_ref", help="module:function; imported so the decider is registered")
        approval.add_argument("--db", required=True, type=Path)
        approval.add_argument("--id", required=True, dest="workflow_id")
        approval.add_argument("--key", required=True)
        approval.add_argument("--by", required=True, help="Decision actor id/name for receipt provenance")
        approval.add_argument("--channel", required=True, help="Where this approval was captured, e.g. discord, cli, local-dashboard")
        approval.add_argument("--message-url")
        approval.add_argument("--message-id")
        approval.add_argument("--event-id")
        approval.add_argument("--note")
        approval.add_argument("--idempotency-key")
        if action_name == "reject":
            approval.add_argument("--reason")

    args = parser.parse_args(argv)
    if args.command == "registry":
        if args.registry_command == "discover":
            project_root = infer_project_root(args.project_root)
            print_json({"project_root": str(project_root), "workflows": discover_workflow_refs(project_root)})
            return 0
        registry_obj = WorkflowRegistry.from_sources(config_path=args.config)
        if args.registry_command == "list":
            print_json(registry_obj.to_payload())
            return 0
        if args.registry_command == "doctor":
            payload = registry_obj.to_payload()
            checks = []
            for workflow_cfg in registry_obj.workflows.values():
                check = {"name": workflow_cfg.name, "workflow_ref": workflow_cfg.workflow_ref, "importable": False, "db_resolved": False}
                try:
                    load_workflow(workflow_cfg.workflow_ref)
                except Exception as exc:  # pragma: no cover - exact import errors are environment-specific.
                    check["import_error"] = str(exc)
                else:
                    check["importable"] = True
                try:
                    registry_obj.resolve_db(workflow_cfg.db)
                except Exception as exc:
                    check["db_error"] = str(exc)
                else:
                    check["db_resolved"] = True
                checks.append(check)
            print_json({**payload, "checks": checks, "ok": all(item["importable"] and item["db_resolved"] for item in checks)})
            return 0
    if args.command == "invoke":
        registry_obj = WorkflowRegistry.from_sources(config_path=args.config)
        payload = InvocationService(registry_obj).invoke(
            args.workflow,
            db=args.db,
            workflow_id=args.workflow_id,
            input_payload=json.loads(args.input_json) if args.input_json else None,
            source=json.loads(args.source_json) if args.source_json else None,
            dashboard_out=args.dashboard_out,
        )
        maybe_write_json(args.receipt_json, payload)
        print_json(payload)
        return 0
    if args.command == "resume-trusted":
        registry_obj = WorkflowRegistry.from_sources(config_path=args.config)
        payload = TrustedResumer(registry_obj).resume_trusted(
            args.workflow,
            db=args.db,
            workflow_id=args.workflow_id,
            dashboard_out=args.dashboard_out,
        )
        maybe_write_json(args.receipt_json, payload)
        print_json(payload)
        return 0
    if args.command == "resume-pending":
        registry_obj = WorkflowRegistry.from_sources(config_path=args.config)
        payload = {"resumed": TrustedResumer(registry_obj).resume_pending(args.registry_name, db=args.db, limit=args.limit)}
        maybe_write_json(args.receipt_json, payload)
        print_json(payload)
        return 0
    if args.command == "doctor":
        return run_doctor(args)
    if args.command == "run":
        if args.direct:
            args.command = "_run-engine"
            return run_engine_cli(args)
        return run_via_uv(raw_argv, args)
    if args.command == "_run-engine":
        return run_engine_cli(args)
    if args.command == "worker" and args.config is not None:
        return run_worker_registry_cli(args)
    if args.command == "worker" and (not args.workflow_ref or not args.db or not args.workflow_id):
        raise SystemExit("worker scoped mode requires workflow_ref, --db, and --id unless --config is supplied")

    read_only_commands = {"status", "list", "events", "outbox", "dashboard", "serve-dashboard"}
    engine = WorkflowEngine(
        args.db,
        read_only=args.command in read_only_commands,
        agent_runner=agent_runner_from_args(args) if args.command == "worker" else None,
    )
    workflow = None
    if hasattr(args, "workflow_ref") and not (args.command == "serve-dashboard" and not args.enable_approval_actions):
        workflow = load_workflow(args.workflow_ref)

    if args.command == "start":
        result = engine.start(
            workflow,
            json.loads(args.input_json),
            workflow_id=args.workflow_id,
            workflow_ref=args.workflow_ref,
        )
        print_json(result_payload(result))
    elif args.command == "run":
        result = engine.run_until_idle(
            workflow,
            json.loads(args.input_json),
            workflow_id=args.workflow_id,
            workflow_ref=args.workflow_ref,
        )
        print_json(result_payload(result))
    elif args.command == "worker":
        if args.once:
            result = engine.worker_once(
                args.workflow_id,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
            )
        else:
            result = engine.worker_until_idle(
                args.workflow_id,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                max_commands=args.max_commands,
            )
        print_json(result_payload(result))
    elif args.command == "signal":
        result = engine.signal(
            args.workflow_id,
            args.signal_type,
            key=args.key,
            payload=json.loads(args.payload_json),
            source=json.loads(args.source_json) if args.source_json else None,
            idempotency_key=args.idempotency_key,
        )
        print_json(result_payload(result))
    elif args.command in {"approve", "reject"}:
        receipt = engine.submit_approval_decision(
            ApprovalDecisionInput(
                workflow_id=args.workflow_id,
                key=args.key,
                action=args.command,
                by=args.by,
                source=human_source_from_args(args),
                note=args.note,
                reason=getattr(args, "reason", None),
                idempotency_key=args.idempotency_key
                or f"{args.channel}:{args.workflow_id}:{args.key}:{args.command}:{args.message_url or args.message_id or args.event_id}",
            ),
            resume=True,
        )
        print_json(
            {
                "workflow_id": receipt.workflow_id,
                "status": receipt.status,
                "waiting_on": receipt.waiting_on,
                "approval": {
                    "key": receipt.key,
                    "action": receipt.action,
                    "by": receipt.by,
                    "source": receipt.source,
                },
                "result": receipt.result_summary,
                "error": None,
            }
        )
    elif args.command == "reconcile-child":
        result = engine.reconcile_child_result(args.workflow_id, args.child_key)
        print_json(result_payload(result))
    elif args.command == "reconcile-children":
        result = engine.reconcile_children(args.workflow_id)
        print_json(result_payload(result))
    elif args.command == "cancel":
        result = engine.cancel_workflow(
            args.workflow_id,
            reason=args.reason,
            source=json.loads(args.source_json) if args.source_json else None,
            superseded_by=args.superseded_by,
        )
        print_json(result_payload(result))
    elif args.command == "status":
        print_json(
            engine.workflow_status(
                args.workflow_id,
                recent_events=args.recent_events,
                command_history=args.commands,
                command_limit=args.command_limit,
                command_payload_chars=args.command_payload_chars,
            )
        )
    elif args.command == "list":
        print_json({"workflows": engine.list_workflows(status=args.status)})
    elif args.command == "events":
        print_json({"events": engine.events(args.workflow_id, limit=args.limit)})
    elif args.command == "outbox":
        print_json({"commands": engine.outbox_commands(workflow_id=args.workflow_id, status=args.status)})
    elif args.command == "dashboard":
        out_path = render_dashboard(engine, args.out, status=args.status, recent_events=args.recent_events)
        print_json({"dashboard": str(out_path)})
    elif args.command == "serve-dashboard":
        if args.enable_approval_actions and workflow is None:  # pragma: no cover - argparse always supplies workflow_ref here.
            raise SystemExit("serve-dashboard approval actions require workflow_ref")
        serve_dashboard(
            db_path=args.db,
            workflow=workflow,
            workflow_ref=args.workflow_ref,
            host=args.host,
            port=args.port,
            once=args.once,
            approval_actions=args.enable_approval_actions,
        )
    else:  # pragma: no cover - argparse prevents this.
        raise SystemExit(f"unknown command: {args.command}")

    return 0

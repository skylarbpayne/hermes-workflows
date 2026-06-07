from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any, Callable

from .approvals import ApprovalDecisionInput
from .dashboard import render_dashboard
from .dashboard_server import serve_dashboard
from .engine import JsonCodec, RunResult, WorkflowEngine
from .invocation import InvocationService, TrustedResumer
from .registry import WorkflowRegistry


def load_workflow(ref: str) -> Callable[..., Any]:
    if ":" not in ref:
        raise SystemExit("workflow ref must look like module:function")
    module_name, attr = ref.split(":", 1)
    module = importlib.import_module(module_name)
    workflow = getattr(module, attr)
    return workflow


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
    import sys

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
    checks["ok"] = bool(checks["python"] and checks["sqlite"] and checks["db_parent_writable"])
    print_json({"doctor": checks})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-workflows")
    sub = parser.add_subparsers(dest="command", required=True)

    registry = sub.add_parser("registry", help="Inspect workflow registry aliases")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_list = registry_sub.add_parser("list", help="List configured DB and workflow aliases")
    registry_list.add_argument("--config", type=Path)
    registry_doctor = registry_sub.add_parser("doctor", help="Validate configured workflow refs and DB aliases")
    registry_doctor.add_argument("--config", type=Path)

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

    run = sub.add_parser("run", help="Run a workflow until idle")
    run.add_argument("workflow_ref", help="module:function")
    run.add_argument("--db", required=True, type=Path)
    run.add_argument("--id", required=True, dest="workflow_id")
    run.add_argument("--input-json", required=True)

    worker = sub.add_parser("worker", help="Execute leased run_step commands for a workflow")
    worker.add_argument("workflow_ref", help="module:function; imported so the decider and steps are registered")
    worker.add_argument("--db", required=True, type=Path)
    worker.add_argument("--id", required=True, dest="workflow_id")
    worker.add_argument("--worker-id", default="cli-worker")
    worker.add_argument("--lease-seconds", type=int, default=30)
    worker.add_argument("--once", action="store_true", help="Execute at most one command")
    worker.add_argument("--max-commands", type=int)

    signal = sub.add_parser("signal", help="Send a signal to a workflow and drain runnable steps")
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
        approval.add_argument("--by", required=True, help="Human id; must match human:<id> approver when specified")
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

    read_only_commands = {"status", "list", "events", "outbox", "dashboard", "serve-dashboard"}
    engine = WorkflowEngine(args.db, read_only=args.command in read_only_commands)
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

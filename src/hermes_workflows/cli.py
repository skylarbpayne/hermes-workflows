from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any, Callable

from .engine import JsonCodec, RunResult, WorkflowEngine


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-workflows")
    sub = parser.add_subparsers(dest="command", required=True)

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

    args = parser.parse_args(argv)
    engine = WorkflowEngine(args.db)
    workflow = load_workflow(args.workflow_ref) if hasattr(args, "workflow_ref") else None

    if args.command == "start":
        result = engine.start(
            workflow,
            json.loads(args.input_json),
            workflow_id=args.workflow_id,
        )
        print_json(result_payload(result))
    elif args.command == "run":
        result = engine.run_until_idle(
            workflow,
            json.loads(args.input_json),
            workflow_id=args.workflow_id,
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
    elif args.command == "cancel":
        result = engine.cancel_workflow(
            args.workflow_id,
            reason=args.reason,
            source=json.loads(args.source_json) if args.source_json else None,
            superseded_by=args.superseded_by,
        )
        print_json(result_payload(result))
    elif args.command == "status":
        print_json(engine.workflow_status(args.workflow_id, recent_events=args.recent_events))
    elif args.command == "list":
        print_json({"workflows": engine.list_workflows(status=args.status)})
    elif args.command == "events":
        print_json({"events": engine.events(args.workflow_id, limit=args.limit)})
    elif args.command == "outbox":
        print_json({"commands": engine.outbox_commands(workflow_id=args.workflow_id, status=args.status)})
    else:  # pragma: no cover - argparse prevents this.
        raise SystemExit(f"unknown command: {args.command}")

    return 0

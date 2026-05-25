from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any, Callable

from .engine import RunResult, WorkflowEngine


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

    args = parser.parse_args(argv)
    engine = WorkflowEngine(args.db)
    workflow = load_workflow(args.workflow_ref)

    if args.command == "start":
        result = engine.start(
            workflow,
            json.loads(args.input_json),
            workflow_id=args.workflow_id,
        )
    elif args.command == "run":
        result = engine.run_until_idle(
            workflow,
            json.loads(args.input_json),
            workflow_id=args.workflow_id,
        )
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
    elif args.command == "signal":
        result = engine.signal(
            args.workflow_id,
            args.signal_type,
            key=args.key,
            payload=json.loads(args.payload_json),
            source=json.loads(args.source_json) if args.source_json else None,
            idempotency_key=args.idempotency_key,
        )
    else:  # pragma: no cover - argparse prevents this.
        raise SystemExit(f"unknown command: {args.command}")

    print(json.dumps(result_payload(result), sort_keys=True))
    return 0

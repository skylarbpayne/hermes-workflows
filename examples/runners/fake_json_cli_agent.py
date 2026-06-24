from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any


GENERATED_WORKFLOW_SOURCE = '''
from hermes_workflows import workflow

@workflow
async def process_item(item):
    return {"processed": item}
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic fake JSON CLI agent for adapter tests/examples.")
    parser.add_argument("--fail-invalid-json", action="store_true")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--stderr", default="")
    parser.add_argument("--stdout", default="")
    parser.add_argument("--provenance-note", default="")
    parser.add_argument("--provenance-transcript", default="")
    parser.add_argument("--provenance-message", default="")
    parser.add_argument("--huge-stdout-bytes", type=int, default=0)
    # Secret-looking flags are accepted so adapter tests can verify argv redaction.
    parser.add_argument("--api-key")
    parser.add_argument("--token")
    parser.add_argument("--password")
    parser.add_argument("--secret")
    parser.add_argument("--auth")
    parser.add_argument("--cookie")
    parser.add_argument("-k")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    prompt = sys.stdin.read()

    if args.stderr:
        sys.stderr.write(args.stderr)
        sys.stderr.flush()
    if args.sleep_seconds:
        time.sleep(args.sleep_seconds)
    if args.huge_stdout_bytes:
        sys.stdout.write("x" * args.huge_stdout_bytes)
        sys.stdout.flush()
        return args.exit_code
    if args.stdout:
        sys.stdout.write(args.stdout)
        sys.stdout.flush()
        return args.exit_code
    if args.fail_invalid_json or "FAIL_INVALID_JSON" in prompt:
        sys.stdout.write("not json")
        return args.exit_code
    if args.exit_code:
        return args.exit_code

    if "WORKFLOW_OUTPUT" in prompt:
        output = {"source": GENERATED_WORKFLOW_SOURCE, "symbol": "process_item"}
    else:
        output = {"kind": "fake.agent_response.v1", "prompt_seen": "agent(...) request:" in prompt}
    provider_provenance: dict[str, Any] = {"runner": "fake_json_cli_agent", "model": "fake-1"}
    if args.provenance_note:
        provider_provenance["notes"] = args.provenance_note
    if args.provenance_transcript:
        provider_provenance["transcript"] = args.provenance_transcript
    if args.provenance_message:
        provider_provenance["messages"] = [{"role": "assistant", "content": args.provenance_message}]
    json.dump({"output": output, "provenance": provider_provenance}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

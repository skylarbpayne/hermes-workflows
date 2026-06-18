from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_workflows import WorkflowEngine, bash, workflow
from hermes_workflows.bash import BashResult


@workflow
async def bash_success_workflow(inputs):
    return await bash("printf 'hello'", name="success")


@workflow
async def bash_failure_workflow(inputs):
    return await bash("printf 'badout'; printf 'baderr' >&2; exit 7", key="fail")


@workflow
async def bash_timeout_workflow(inputs):
    return await bash("sleep 1", key="timeout", timeout_seconds=0.05)


@workflow
async def bash_redaction_workflow(inputs):
    return await bash(
        "printf 'secret=s3cr3t token=abc123'",
        key="redact",
        redact_values=["s3cr3t"],
        redact_patterns=[r"token=[A-Za-z0-9]+"],
    )


@workflow
async def bash_replay_workflow(inputs):
    return await bash("printf 'run\\n' >> counter.txt; printf 'done'", key="once", cwd=inputs["cwd"])


@workflow
async def bash_cwd_workflow(inputs):
    return await bash("pwd", key="pwd", cwd=inputs["cwd"])


def command_row(db: Path, key: str) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM workflow_commands_outbox WHERE key = ?", (key,)).fetchone()
        assert row is not None
        return dict(row)
    finally:
        con.close()


def event_payload(engine: WorkflowEngine, workflow_id: str, event_type: str, key: str) -> dict:
    for event in engine.events(workflow_id):
        if event["type"] == event_type and event["key"] == key:
            return event["payload"]
    raise AssertionError(f"missing {event_type} event for {key}")


def test_bash_success_captures_output_and_metadata(tmp_path):
    db = tmp_path / "workflow.sqlite"
    result = WorkflowEngine(db).run_until_idle(bash_success_workflow, {}, workflow_id="wf_bash_success")

    assert result.status == "completed"
    bash_result = BashResult.from_value(result.result)
    assert bash_result.command == "printf 'hello'"
    assert bash_result.exit_code == 0
    assert bash_result.stdout == "hello"
    assert bash_result.stderr == ""
    assert bash_result.timed_out is False
    assert bash_result.duration_seconds >= 0


def test_bash_nonzero_exit_fails_with_structured_details(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(bash_failure_workflow, {}, workflow_id="wf_bash_fail")

    assert engine.worker_once("wf_bash_fail", worker_id="worker-a", lease_seconds=60).status == "waiting"
    result = engine.worker_once("wf_bash_fail", worker_id="worker-b", lease_seconds=60)

    assert result.status == "failed"
    assert "BashStepError: bash command exited with status 7" in (result.error or "")
    row = command_row(db, "fail")
    error = json.loads(row["last_error_json"])
    assert error["type"] == "BashStepError"
    assert error["details"]["exit_code"] == 7
    assert error["details"]["stdout"] == "badout"
    assert error["details"]["stderr"] == "baderr"
    failed = event_payload(engine, "wf_bash_fail", "StepFailed", "fail")
    assert failed["error"]["details"]["command"].endswith("exit 7")


def test_bash_timeout_fails_and_marks_timed_out(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(bash_timeout_workflow, {}, workflow_id="wf_bash_timeout")

    assert engine.worker_once("wf_bash_timeout", worker_id="worker-a", lease_seconds=60).status == "waiting"
    result = engine.worker_once("wf_bash_timeout", worker_id="worker-b", lease_seconds=60)

    assert result.status == "failed"
    assert "timed out" in (result.error or "")
    error = json.loads(command_row(db, "timeout")["last_error_json"])
    assert error["details"]["timed_out"] is True
    assert error["details"]["exit_code"] is None


def test_bash_redacts_captured_output(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    result = engine.run_until_idle(bash_redaction_workflow, {}, workflow_id="wf_bash_redact")

    assert result.status == "completed"
    bash_result = BashResult.from_value(result.result)
    assert bash_result.stdout == "secret=[REDACTED] [REDACTED]"
    assert "s3cr3t" not in bash_result.stdout
    assert "token=abc123" not in bash_result.stdout
    completed = event_payload(engine, "wf_bash_redact", "StepCompleted", "redact")
    assert completed["output"]["stdout"] == "secret=[REDACTED] [REDACTED]"


def test_bash_replay_does_not_rerun_completed_command(tmp_path):
    db = tmp_path / "workflow.sqlite"
    workdir = tmp_path / "work"
    workdir.mkdir()

    result = WorkflowEngine(db).run_until_idle(
        bash_replay_workflow,
        {"cwd": str(workdir)},
        workflow_id="wf_bash_replay",
    )

    assert result.status == "completed"
    assert (workdir / "counter.txt").read_text() == "run\n"


def test_bash_honors_cwd(tmp_path):
    db = tmp_path / "workflow.sqlite"
    workdir = tmp_path / "cwd"
    workdir.mkdir()

    result = WorkflowEngine(db).run_until_idle(bash_cwd_workflow, {"cwd": str(workdir)}, workflow_id="wf_bash_cwd")

    assert result.status == "completed"
    bash_result = BashResult.from_value(result.result)
    assert bash_result.cwd == str(workdir)
    assert Path(bash_result.stdout.strip()).resolve() == workdir.resolve()

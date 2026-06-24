import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from hermes_workflows import WorkflowEngine, approve, cancel_workflow, step, workflow


STEP_RUNS = []


@step
async def cancellation_side_effect_step(value):
    STEP_RUNS.append(value)
    return {"value": value}


@workflow
async def step_wait_workflow(inputs):
    return await cancellation_side_effect_step(inputs["item"])


@workflow
async def approval_only_workflow(inputs):
    decision = await approve(
        "Approve the thing?",
        key="approve_thing",
        artifact={"item": inputs["item"]},
        approver="human:skylar",
    )
    return {"approved_by": decision["by"]}


@workflow
async def immediate_success_workflow(inputs):
    return {"item": inputs["item"], "ok": True}


@workflow
async def immediate_failure_workflow(inputs):
    raise RuntimeError(f"boom {inputs['item']}")


@workflow
async def cancels_itself_then_returns(inputs):
    cancel_workflow(reason="operator cancelled during decider")
    return {"should_not": "complete"}


@workflow
async def cancels_itself_then_requests_approval(inputs):
    cancel_workflow(reason="operator cancelled before new wait")
    await approve(
        "This should not be enqueued after cancellation",
        key="late_approval",
        artifact={"item": inputs["item"]},
        approver="human:skylar",
    )
    return {"should_not": "wait"}


def run_cli(tmp_path, *args):
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}:{Path.cwd()}:{tmp_path}:{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "hermes_workflows", *args],
        cwd=Path.cwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_engine_cancel_workflow_marks_instance_outbox_and_status_audit(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    started = engine.run_until_idle(approval_only_workflow, {"item": "plan"}, workflow_id="wf_cancel")
    assert started.status == "waiting"

    with sqlite3.connect(db) as con:
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET status = 'running', claimed_by = 'notify-worker', lease_expires_at = 9999999999
            WHERE workflow_id = 'wf_cancel' AND key = 'approval:approve_thing'
            """
        )

    cancelled = engine.cancel_workflow(
        "wf_cancel",
        reason="superseded by fresh plan workflow",
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "m-123"},
        superseded_by="wf_replacement",
    )

    assert cancelled.status == "cancelled"
    assert cancelled.waiting_on is None

    status = engine.workflow_status("wf_cancel")
    assert status["status"] == "cancelled"
    assert status["waiting_on"] is None
    assert status["pending_commands"] == []
    assert status["terminal_reason"] == {
        "type": "cancelled",
        "reason": "superseded by fresh plan workflow",
        "source": {"kind": "human", "id": "skylar", "channel": "discord", "message_id": "m-123"},
        "superseded_by": "wf_replacement",
    }
    assert status["events"][-1]["type"] == "WorkflowCancelled"
    assert status["events"][-1]["payload"] == status["terminal_reason"]

    outbox = engine.outbox_commands(workflow_id="wf_cancel", status="cancelled")
    assert len(outbox) == 1
    assert outbox[0]["key"] == "approval:approve_thing"
    assert outbox[0]["status"] == "cancelled"
    assert outbox[0]["claimed_by"] == "notify-worker"
    assert outbox[0]["lease_expires_at"] is None
    assert outbox[0]["workflow_status"] == "cancelled"

    assert engine.list_workflows(status="cancelled") == [
        {
            "workflow_id": "wf_cancel",
            "workflow_name": "approval_only_workflow",
            "status": "cancelled",
            "waiting_on": None,
            "terminal_reason": status["terminal_reason"],
        }
    ]


def test_late_signal_after_cancel_does_not_resume_or_append_signal_event(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(approval_only_workflow, {"item": "plan"}, workflow_id="wf_cancel")
    engine.cancel_workflow("wf_cancel", reason="stale", source={"kind": "operator"})

    before = engine.events("wf_cancel")
    late = engine.signal(
        "wf_cancel",
        "approval.decision",
        key="approve_thing",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "late"},
        idempotency_key="late-approval",
    )

    assert late.status == "cancelled"
    assert late.waiting_on is None
    assert engine.workflow_status("wf_cancel")["terminal_reason"]["reason"] == "stale"
    assert engine.events("wf_cancel") == before


def test_cancel_terminal_completed_workflow_is_noop(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    completed = engine.run_until_idle(immediate_success_workflow, {"item": "plan"}, workflow_id="wf_done")
    assert completed.status == "completed"

    cancelled = engine.cancel_workflow("wf_done", reason="too late")

    assert cancelled.status == "completed"
    status = engine.workflow_status("wf_done")
    assert status["status"] == "completed"
    assert status["result"] == {"item": "plan", "ok": True}
    assert status["terminal_reason"] is None
    assert [event["type"] for event in engine.events("wf_done")] == [
        "WorkflowStarted",
        "CommandClaimed",
        "WorkflowCompleted",
    ]


def test_cancel_terminal_failed_workflow_is_noop(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    failed = engine.run_until_idle(immediate_failure_workflow, {"item": "plan"}, workflow_id="wf_failed")
    assert failed.status == "failed"

    cancelled = engine.cancel_workflow("wf_failed", reason="too late")

    assert cancelled.status == "failed"
    status = engine.workflow_status("wf_failed")
    assert status["status"] == "failed"
    assert "RuntimeError: boom plan" in status["error"]
    assert status["terminal_reason"] is None
    assert [event["type"] for event in engine.events("wf_failed")] == [
        "WorkflowStarted",
        "CommandClaimed",
    ]


def test_claimed_step_cancelled_before_execution_does_not_run_body(tmp_path):
    STEP_RUNS.clear()
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(step_wait_workflow, {"item": "plan"}, workflow_id="wf_claimed_cancel")
    engine.worker_once("wf_claimed_cancel", worker_id="worker-start")
    command = engine.claim_command("wf_claimed_cancel", worker_id="worker-a", lease_seconds=60)
    assert command is not None

    engine.cancel_workflow("wf_claimed_cancel", reason="operator cancelled claimed command")
    result = engine._execute_run_step_command("wf_claimed_cancel", command)

    assert result.status == "cancelled"
    assert STEP_RUNS == []
    assert [event["type"] for event in engine.events("wf_claimed_cancel")] == [
        "WorkflowStarted",
        "CommandClaimed",
        "StepRequested",
        "CommandClaimed",
        "WorkflowCancelled",
    ]


def test_claim_command_refuses_stale_pending_command_on_cancelled_workflow(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(step_wait_workflow, {"item": "plan"}, workflow_id="wf_stale_pending")
    engine.worker_once("wf_stale_pending", worker_id="worker-start")
    engine.cancel_workflow("wf_stale_pending", reason="operator cancelled")
    with sqlite3.connect(db) as con:
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET status = 'pending'
            WHERE workflow_id = 'wf_stale_pending'
            """
        )

    assert engine.claim_command("wf_stale_pending", worker_id="worker-a", lease_seconds=60) is None
    assert [event["type"] for event in engine.events("wf_stale_pending")] == [
        "WorkflowStarted",
        "CommandClaimed",
        "StepRequested",
        "WorkflowCancelled",
    ]


def test_cancel_committed_during_decider_cannot_be_overwritten_by_completion(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(cancels_itself_then_returns, {"item": "plan"}, workflow_id="wf_race")

    assert result.status == "cancelled"
    status = engine.workflow_status("wf_race")
    assert status["status"] == "cancelled"
    assert status["terminal_reason"]["reason"] == "operator cancelled during decider"
    assert [event["type"] for event in engine.events("wf_race")] == [
        "WorkflowStarted",
        "CommandClaimed",
        "WorkflowCancelled",
    ]


def test_cancelled_decider_cannot_enqueue_new_waits_after_cancel(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(cancels_itself_then_requests_approval, {"item": "plan"}, workflow_id="wf_late_wait")

    assert result.status == "cancelled"
    status = engine.workflow_status("wf_late_wait")
    assert status["status"] == "cancelled"
    assert status["waiting_on"] is None
    assert status["pending_commands"] == []
    assert [event["type"] for event in engine.events("wf_late_wait")] == [
        "WorkflowStarted",
        "CommandClaimed",
        "WorkflowCancelled",
    ]


def test_cli_cancel_exposes_cancelled_status_list_and_outbox(tmp_path):
    db = tmp_path / "workflow.sqlite"

    run_cli(
        tmp_path,
        "run",
        "tests.test_cancel_workflow:approval_only_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli_cancel",
        "--input-json",
        '{"item":"plan"}',
    )

    cancel_result = run_cli(
        tmp_path,
        "cancel",
        "--db",
        str(db),
        "--id",
        "wf_cli_cancel",
        "--reason",
        "superseded by wf_next",
        "--source-json",
        '{"kind":"human","id":"skylar","channel":"discord","message_id":"m-456"}',
        "--superseded-by",
        "wf_next",
    )
    cancel_payload = json.loads(cancel_result.stdout)
    assert cancel_payload == {
        "workflow_id": "wf_cli_cancel",
        "status": "cancelled",
        "waiting_on": None,
        "result": None,
        "error": None,
    }

    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_cli_cancel").stdout)
    assert status_payload["status"] == "cancelled"
    assert status_payload["terminal_reason"] == {
        "type": "cancelled",
        "reason": "superseded by wf_next",
        "source": {"kind": "human", "id": "skylar", "channel": "discord", "message_id": "m-456"},
        "superseded_by": "wf_next",
    }
    assert status_payload["pending_commands"] == []

    list_payload = json.loads(run_cli(tmp_path, "list", "--db", str(db), "--status", "cancelled").stdout)
    assert list_payload["workflows"][0]["workflow_id"] == "wf_cli_cancel"
    assert list_payload["workflows"][0]["terminal_reason"] == status_payload["terminal_reason"]

    pending_outbox = json.loads(
        run_cli(tmp_path, "outbox", "--db", str(db), "--id", "wf_cli_cancel", "--status", "pending").stdout
    )
    assert pending_outbox == {"commands": []}

    cancelled_outbox = json.loads(
        run_cli(tmp_path, "outbox", "--db", str(db), "--id", "wf_cli_cancel", "--status", "cancelled").stdout
    )
    assert [command["status"] for command in cancelled_outbox["commands"]] == ["cancelled"]

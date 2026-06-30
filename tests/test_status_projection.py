import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

from hermes_workflows import WorkflowEngine, approve, ask, parallel, step, workflow
from hermes_workflows.status_projection import StatusProjection


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str = ""


@step
async def status_projection_step(value):
    return {"value": value}


@workflow
async def status_projection_workflow(inputs):
    decision = await approve(
        "Approve status projection?",
        key="approve_status_projection",
        artifact={"value": inputs["value"]},
    )
    if not decision.approved:
        return {"approved": False}
    return await status_projection_step(inputs["value"])


@workflow
async def parallel_human_wait_workflow(inputs):
    reviews = await parallel(
        [
            ask(
                prompt=f"Review section {item}",
                key=f"review_{item}",
                input={"section": item},
                returns=ReviewDecision,
            )
            for item in inputs["items"]
        ]
    )
    return [review.action for review in reviews]


def _human_source(message_id="projection-approval"):
    return {"channel": "test", "message_id": message_id}


def test_status_projection_matches_workflow_engine_facade_read_models(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(status_projection_workflow, {"value": "green"}, workflow_id="wf_projection")

    assert first.status == "waiting"
    waiting_status = engine.workflow_status("wf_projection")
    assert waiting_status["runtime_state"]["schema_version"] == "runtime_state.v1"
    assert waiting_status["runtime_state"]["primary"] == "waiting_on_human"
    assert waiting_status["runtime_state"]["label"] == "Waiting on Skylar"
    assert waiting_status["runtime_state"]["reason"] == "waiting_on_approval_decision"
    assert waiting_status["runtime_state"]["current_wait"] == {
        "kind": "approval",
        "key": "approve_status_projection",
        "reason": "waiting_on_approval_decision",
        "raw": "signal:approval.decision:approve_status_projection",
    }
    assert waiting_status["runtime_state"]["command"]["type"] == "notify_approval"
    assert waiting_status["runtime_state"]["command"]["diagnostic_labels"] == ["active_wait"]

    projection = StatusProjection(engine)
    assert projection.workflow_status("wf_projection") == engine.workflow_status("wf_projection")
    assert projection.list_workflows(status="waiting") == engine.list_workflows(status="waiting")
    assert projection.outbox_commands(workflow_id="wf_projection", status="pending") == engine.outbox_commands(
        workflow_id="wf_projection",
        status="pending",
    )

    approved = engine.signal(
        "wf_projection",
        "approval.decision",
        key="approve_status_projection",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source(),
        idempotency_key="projection-approval",
    )
    queued_status = engine.workflow_status("wf_projection")
    assert queued_status["runtime_state"]["primary"] == "queued"
    assert queued_status["runtime_state"]["reason"] == "runnable_command_unclaimed"
    assert queued_status["runtime_state"]["command"]["type"] == "run_workflow"
    assert queued_status["runtime_state"]["command"]["key"] == "workflow:run"

    done = engine.drain("wf_projection", initial=approved)

    assert done.status == "completed"
    reader = WorkflowEngine(db, read_only=True)
    read_projection = StatusProjection(reader)
    completed_status = read_projection.workflow_status("wf_projection", recent_events=100, command_history="all")

    assert completed_status == reader.workflow_status("wf_projection", recent_events=100, command_history="all")
    assert completed_status["status"] == "completed"
    assert completed_status["runtime_state"]["primary"] == "completed"
    assert completed_status["runtime_state"]["terminal"] is True
    assert completed_status["runtime_state"]["command"] is None
    assert completed_status["result"] == {"value": "green"}
    assert [command["status"] for command in completed_status["command_history"]] == [
        "completed",
        "completed",
        "completed",
    ]
    assert read_projection.list_workflows(status="completed") == [
        {
            "workflow_id": "wf_projection",
            "workflow_name": "status_projection_workflow",
            "status": "completed",
            "waiting_on": None,
        }
    ]


def test_runtime_state_projects_old_pending_runnable_without_healthy_worker_as_stuck(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(status_projection_workflow, {"value": "green"}, workflow_id="wf_no_worker")
    old = int(time.time()) - 3600
    with sqlite3.connect(db) as con:
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET created_at = ?, updated_at = ?
            WHERE workflow_id = ? AND type = 'run_workflow' AND key = 'workflow:run'
            """,
            (old, old, "wf_no_worker"),
        )

    status = engine.workflow_status("wf_no_worker")

    assert status["runtime_state"]["primary"] == "stuck"
    assert status["runtime_state"]["reason"] == "no_healthy_worker"
    assert status["runtime_state"]["worker_health"]["healthy"] is False
    assert status["runtime_state"]["worker_health"]["active_worker_count"] == 0


def test_runtime_state_keeps_new_pending_runnable_queued_without_worker(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(status_projection_workflow, {"value": "green"}, workflow_id="wf_new_no_worker")

    status = engine.workflow_status("wf_new_no_worker")

    assert status["runtime_state"]["primary"] == "queued"
    assert status["runtime_state"]["reason"] == "runnable_command_unclaimed"
    assert status["runtime_state"]["worker_health"]["healthy"] is False


def test_runtime_state_projects_running_command_with_stale_worker_heartbeat_as_stuck(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(status_projection_workflow, {"value": "green"}, workflow_id="wf_stale_worker")
    engine.record_worker_heartbeat(
        worker_id="resident-worker",
        worker_instance_id="resident-worker:stale",
        heartbeat_ttl_seconds=60,
        identity={"hostname": "test-host"},
    )
    claimed = engine.claim_command(
        "wf_stale_worker",
        worker_id="resident-worker",
        worker_instance_id="resident-worker:stale",
        lease_seconds=3600,
        command_type="run_workflow",
    )
    assert claimed is not None
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE workflow_workers SET heartbeat_expires_at = 1, last_heartbeat_at = 1 WHERE worker_instance_id = ?",
            ("resident-worker:stale",),
        )

    status = engine.workflow_status("wf_stale_worker")

    assert status["runtime_state"]["primary"] == "stuck"
    assert status["runtime_state"]["reason"] == "worker_heartbeat_stale"
    assert status["runtime_state"]["worker"]["status"] == "stale"
    assert status["runtime_state"]["worker_health"]["stale_worker_count"] == 1


def test_runtime_state_projects_repeated_command_failure_as_stuck(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(status_projection_workflow, {"value": "green"}, workflow_id="wf_repeated_failure")
    now = int(time.time())
    with sqlite3.connect(db) as con:
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET attempts = 3, last_error_json = ?, updated_at = ?
            WHERE workflow_id = ? AND type = 'run_workflow' AND key = 'workflow:run'
            """,
            (json.dumps({"type": "RuntimeError", "message": "boom"}), now, "wf_repeated_failure"),
        )

    status = engine.workflow_status("wf_repeated_failure")

    assert status["runtime_state"]["primary"] == "stuck"
    assert status["runtime_state"]["reason"] == "repeated_command_failure"
    assert status["runtime_state"]["command"]["attempts"] == 3
    assert status["runtime_state"]["command"]["last_error"] == {"type": "RuntimeError", "message": "boom"}


def test_runtime_state_projects_expired_claim_as_stuck(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(status_projection_workflow, {"value": "green"}, workflow_id="wf_stuck_projection")
    engine.signal(
        "wf_stuck_projection",
        "approval.decision",
        key="approve_status_projection",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source("stuck-projection-approval"),
        idempotency_key="stuck-projection-approval",
    )

    with engine._connect() as con:
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET status = 'running', claimed_by = 'dead-worker', lease_expires_at = 1, attempts = 1, updated_at = 1
            WHERE workflow_id = ? AND type = 'run_workflow' AND key = 'workflow:run'
            """,
            ("wf_stuck_projection",),
        )

    status = engine.workflow_status("wf_stuck_projection")
    assert status["runtime_state"]["primary"] == "stuck"
    assert status["runtime_state"]["reason"] == "lease_expired"
    assert status["runtime_state"]["command"]["claimed_by"] == "dead-worker"
    assert status["runtime_state"]["command"]["lease_expires_at"] == 1


def test_runtime_state_projects_cancelled_terminal_state(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(status_projection_workflow, {"value": "green"}, workflow_id="wf_cancel_projection")

    cancelled = engine.cancel_workflow("wf_cancel_projection", reason="superseded")

    assert cancelled.status == "cancelled"
    status = engine.workflow_status("wf_cancel_projection")
    assert status["runtime_state"]["primary"] == "cancelled"
    assert status["runtime_state"]["terminal"] is True
    assert status["runtime_state"]["reason"] == "workflow_cancelled"


def test_runtime_state_projects_parallel_human_wait_as_waiting_on_human(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        parallel_human_wait_workflow,
        {"items": ["one", "two"]},
        workflow_id="wf_parallel_human_wait_projection",
    )

    assert first.status == "waiting"
    assert first.waiting_on == "parallel:0"
    status = engine.workflow_status("wf_parallel_human_wait_projection")
    assert status["runtime_state"]["primary"] == "waiting_on_human"
    assert status["runtime_state"]["label"] == "Waiting on Skylar"
    assert status["runtime_state"]["current_wait"] == {
        "kind": "parallel",
        "key": "0",
        "reason": "waiting_on_parallel",
        "raw": "parallel:0",
    }
    assert [request["key"] for request in status["review_requests"]] == ["review_one", "review_two"]

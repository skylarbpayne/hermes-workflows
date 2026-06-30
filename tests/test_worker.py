import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from hermes_workflows import WorkflowEngine, WorkflowRegistry, WorkflowWorkerService, gather, step, wait_for, workflow
from hermes_workflows.engine import WorkflowContext, WorkflowWaiting


RUNS = []
LEASE_OBSERVATIONS = []
HEARTBEAT_OBSERVATIONS = []
SHOULD_FAIL_STALE = False
MIDFLIGHT_RECLAIM_ENGINE = None


@step
async def worker_left(value):
    RUNS.append(("left", value))
    return {"side": "left", "value": value}


@step
async def worker_right(value):
    RUNS.append(("right", value))
    return {"side": "right", "value": value}


@step
async def worker_boom(value):
    raise RuntimeError(f"boom {value}")


@step
async def worker_stale_sensitive():
    if SHOULD_FAIL_STALE:
        raise RuntimeError("stale worker should not win")
    return "fresh result"


@step
async def worker_slow_lease_probe(context):
    key = "step:worker_slow_lease_probe:0"
    row = command_row(context.engine.db_path, key)
    assert row is not None
    before = row["lease_expires_at"]
    time.sleep(2.2)
    row = command_row(context.engine.db_path, key)
    assert row is not None
    during = row["lease_expires_at"]
    LEASE_OBSERVATIONS.append((before, during))
    with sqlite3.connect(context.engine.db_path) as con:
        con.row_factory = sqlite3.Row
        worker_rows = con.execute("SELECT * FROM workflow_workers ORDER BY last_heartbeat_at DESC").fetchall()
    HEARTBEAT_OBSERVATIONS.extend(
        json.loads(worker["metadata_json"])
        for worker in worker_rows
        if worker["metadata_json"]
    )
    return "slow result"


@step
async def worker_reclaims_step_before_return(context):
    expire_claim_and_reclaim(
        context.engine,
        context.workflow_id,
        "step:worker_reclaims_step_before_return:0",
        worker_id="worker-b",
        command_type="run_step",
    )
    return "stale step result"


@step
async def worker_legacy_context_named_handle(runtime_handle, value):
    return {"workflow_id": runtime_handle.workflow_id, "value": value}


@step
async def worker_no_context_varargs(*values):
    return list(values)


@workflow
async def worker_context_compat_workflow(inputs):
    legacy = await worker_legacy_context_named_handle(inputs["value"])
    varargs = await worker_no_context_varargs(1, 2, 3)
    return {"legacy": legacy, "varargs": varargs}


@workflow
async def worker_slow_lease_workflow(inputs):
    return await worker_slow_lease_probe()


@workflow
async def worker_gather_workflow(inputs):
    left, right = await gather(
        worker_left(inputs["left"]),
        worker_right(inputs["right"]),
    )
    return {"left": left, "right": right}


@workflow
async def worker_failure_workflow(inputs):
    return await worker_boom(inputs["value"])


@workflow
async def worker_stale_workflow(inputs):
    return await worker_stale_sensitive()


@workflow
async def worker_immediate_workflow(inputs):
    return {"ok": inputs.get("ok", True)}


@workflow
async def worker_reclaims_before_return_workflow(inputs):
    assert MIDFLIGHT_RECLAIM_ENGINE is not None
    expire_claim_and_reclaim(
        MIDFLIGHT_RECLAIM_ENGINE,
        inputs["workflow_id"],
        "workflow:run",
        worker_id="worker-b",
        command_type="run_workflow",
    )
    return {"ok": True}


@workflow
async def worker_reclaims_step_workflow(inputs):
    return await worker_reclaims_step_before_return()


@workflow
async def worker_signal_wait_workflow(inputs):
    signal = await wait_for("go", key="k")
    return {"signal": signal}


def command_row(db, key):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM workflow_commands_outbox WHERE key = ?", (key,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def command_rows(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in con.execute("SELECT * FROM workflow_commands_outbox ORDER BY id ASC").fetchall()
        ]
    finally:
        con.close()


def expire_claim_and_reclaim(engine, workflow_id, key, *, worker_id, command_type):
    row = command_row(engine.db_path, key)
    assert row is not None
    expired_at = int(time.time()) - 1
    con = sqlite3.connect(engine.db_path)
    try:
        con.execute(
            "UPDATE workflow_commands_outbox SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
            (expired_at, expired_at, row["id"]),
        )
        con.commit()
    finally:
        con.close()
    reclaim_engine = WorkflowEngine(engine.db_path)
    reclaimed = reclaim_engine.claim_command(workflow_id, worker_id=worker_id, lease_seconds=60, command_type=command_type)
    assert reclaimed is not None
    assert reclaimed["key"] == key
    assert reclaimed["claimed_by"] == worker_id
    return reclaimed


def test_worker_preserves_legacy_step_context_and_new_varargs_steps(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(worker_context_compat_workflow, {"value": "ok"}, workflow_id="wf_context_compat")

    assert result.status == "completed"
    assert result.result == {
        "legacy": {"workflow_id": "wf_context_compat", "value": "ok"},
        "varargs": [1, 2, 3],
    }


def test_start_enqueues_workflow_run_without_inline_step_requests(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_worker")

    assert result.status == "running"
    assert result.waiting_on is None
    rows = command_rows(db)
    assert [(row["type"], row["key"], row["status"]) for row in rows] == [
        ("run_workflow", "workflow:run", "pending")
    ]
    assert [event["type"] for event in engine.events("wf_worker")] == ["WorkflowStarted"]


def test_worker_claims_one_pending_workflow_run_with_a_lease(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_worker")

    claimed = engine.claim_command("wf_worker", worker_id="worker-a", lease_seconds=60, command_type=None)

    assert claimed["type"] == "run_workflow"
    assert claimed["key"] == "workflow:run"
    assert claimed["status"] == "running"
    assert claimed["claimed_by"] == "worker-a"
    assert isinstance(claimed["claim_token"], str)
    assert len(claimed["claim_token"]) > 20
    assert claimed["attempts"] == 1
    assert engine.claim_command("wf_worker", worker_id="worker-b", lease_seconds=60, command_type=None) is None
    assert command_row(db, "workflow:run")["claimed_by"] == "worker-a"


def test_expired_running_command_can_be_reclaimed(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_worker")

    first = engine.claim_command("wf_worker", worker_id="worker-a", lease_seconds=-1, command_type=None)
    reclaimed = engine.claim_command("wf_worker", worker_id="worker-b", lease_seconds=60, command_type=None)

    assert reclaimed["id"] == first["id"]
    assert reclaimed["key"] == "workflow:run"
    assert reclaimed["claimed_by"] == "worker-b"
    assert reclaimed["attempts"] == 2
    assert reclaimed["claim_token"] != first["claim_token"]


def test_worker_once_executes_workflow_run_and_steps_until_workflow_completes(tmp_path):
    RUNS.clear()
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_worker")

    first = engine.worker_once("wf_worker", worker_id="worker-a", lease_seconds=60)
    assert first.status == "waiting"
    assert first.waiting_on == "gather:0"
    assert RUNS == []
    assert [(row["type"], row["key"], row["status"]) for row in command_rows(db)] == [
        ("run_workflow", "workflow:run", "completed"),
        ("run_step", "step:worker_left:0", "pending"),
        ("run_step", "step:worker_right:0", "pending"),
    ]

    second = engine.worker_once("wf_worker", worker_id="worker-b", lease_seconds=60)
    assert second.status == "running"
    assert RUNS == [("left", 1)]
    assert command_row(db, "workflow:run")["status"] == "pending"

    third = engine.worker_once("wf_worker", worker_id="worker-c", lease_seconds=60)
    assert third.status == "waiting"
    assert third.waiting_on == "gather:0"
    assert RUNS == [("left", 1)]

    fourth = engine.worker_once("wf_worker", worker_id="worker-d", lease_seconds=60)
    assert fourth.status == "running"
    assert RUNS == [("left", 1), ("right", 2)]

    final = engine.worker_once("wf_worker", worker_id="worker-e", lease_seconds=60)
    assert final.status == "completed"
    assert final.result == {
        "left": {"side": "left", "value": 1},
        "right": {"side": "right", "value": 2},
    }
    assert RUNS == [("left", 1), ("right", 2)]
    assert command_row(db, "workflow:run")["status"] == "completed"
    assert command_row(db, "step:worker_left:0")["status"] == "completed"
    assert command_row(db, "step:worker_right:0")["status"] == "completed"


def test_worker_records_step_failure_and_marks_command_failed(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_failure_workflow, {"value": "bad"}, workflow_id="wf_fail")

    queued_step = engine.worker_once("wf_fail", worker_id="worker-a", lease_seconds=60)
    assert queued_step.status == "waiting"
    assert queued_step.waiting_on == "step:worker_boom:0"

    result = engine.worker_once("wf_fail", worker_id="worker-b", lease_seconds=60)

    assert result.status == "failed"
    assert result.error is not None
    assert "RuntimeError: boom bad" in result.error
    row = command_row(db, "step:worker_boom:0")
    assert row["status"] == "failed"
    assert json.loads(row["last_error_json"]) == {"type": "RuntimeError", "message": "boom bad"}

    reloaded = WorkflowEngine(db).worker_once("wf_fail", worker_id="worker-c", lease_seconds=60)
    assert reloaded.status == "failed"
    assert reloaded.error is not None
    assert "RuntimeError: boom bad" in reloaded.error

    events = engine.events("wf_fail")
    assert [event["type"] for event in events].count("StepFailed") == 1
    assert events[-1]["type"] == "StepFailed"


def test_stale_worker_cannot_overwrite_reclaimed_command_result(tmp_path):
    global SHOULD_FAIL_STALE
    SHOULD_FAIL_STALE = False
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_stale_workflow, {}, workflow_id="wf_stale")
    queued_step = engine.worker_once("wf_stale", worker_id="worker-bootstrap", lease_seconds=60)
    assert queued_step.status == "waiting"
    assert queued_step.waiting_on == "step:worker_stale_sensitive:0"

    stale = engine.claim_command("wf_stale", worker_id="worker-a", lease_seconds=-1)
    fresh = engine.claim_command("wf_stale", worker_id="worker-b", lease_seconds=60)
    assert stale is not None
    assert fresh is not None
    assert fresh["attempts"] == 2

    completed = engine._execute_run_step_command("wf_stale", fresh)
    assert completed.status == "running"
    assert command_row(db, "workflow:run")["status"] == "pending"

    final = engine.worker_once("wf_stale", worker_id="worker-c", lease_seconds=60)
    assert final.status == "completed"
    assert final.result == "fresh result"

    SHOULD_FAIL_STALE = True
    try:
        stale_result = engine._execute_run_step_command("wf_stale", stale)
    finally:
        SHOULD_FAIL_STALE = False

    assert stale_result.status == "completed"
    assert stale_result.result == "fresh result"
    assert command_row(db, "step:worker_stale_sensitive:0")["status"] == "completed"
    assert [event["type"] for event in engine.events("wf_stale")].count("StepFailed") == 0


def test_renewing_command_lease_prevents_premature_reclaim(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_stale_workflow, {}, workflow_id="wf_renew")
    engine.worker_once("wf_renew", worker_id="worker-bootstrap", lease_seconds=60)

    claimed = engine.claim_command("wf_renew", worker_id="worker-a", lease_seconds=60)
    assert claimed is not None
    assert engine.renew_command_lease("wf_renew", claimed, lease_seconds=60) is True

    assert engine.claim_command("wf_renew", worker_id="worker-b", lease_seconds=60) is None
    row = command_row(db, "step:worker_stale_sensitive:0")
    assert row is not None
    assert row["claimed_by"] == "worker-a"
    assert row["attempts"] == 1


def test_expired_command_claim_cannot_renew(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_stale_workflow, {}, workflow_id="wf_expired_renew")
    engine.worker_once("wf_expired_renew", worker_id="worker-bootstrap", lease_seconds=60)

    claimed = engine.claim_command("wf_expired_renew", worker_id="worker-a", lease_seconds=-1)
    assert claimed is not None

    assert engine.renew_command_lease("wf_expired_renew", claimed, lease_seconds=60) is False
    reclaimed = engine.claim_command("wf_expired_renew", worker_id="worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed["claimed_by"] == "worker-b"
    assert reclaimed["attempts"] == 2
    assert reclaimed["claim_token"] != claimed["claim_token"]


def test_expired_step_claim_cannot_execute_without_reclaim(tmp_path):
    global SHOULD_FAIL_STALE
    SHOULD_FAIL_STALE = False
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_stale_workflow, {}, workflow_id="wf_expired_step")
    queued_step = engine.worker_once("wf_expired_step", worker_id="worker-bootstrap", lease_seconds=60)
    assert queued_step.status == "waiting"

    stale = engine.claim_command("wf_expired_step", worker_id="worker-a", lease_seconds=-1)
    assert stale is not None

    SHOULD_FAIL_STALE = True
    try:
        stale_result = engine._execute_run_step_command("wf_expired_step", stale)
    finally:
        SHOULD_FAIL_STALE = False

    assert stale_result.status == "waiting"
    assert stale_result.waiting_on == "step:worker_stale_sensitive:0"
    event_types = [event["type"] for event in engine.events("wf_expired_step")]
    assert event_types.count("StepCompleted") == 0
    assert event_types.count("StepFailed") == 0

    fresh = engine.claim_command("wf_expired_step", worker_id="worker-b", lease_seconds=60)
    assert fresh is not None
    assert fresh["attempts"] == 2
    completed = engine._execute_run_step_command("wf_expired_step", fresh)
    assert completed.status == "running"


def test_expired_workflow_run_claim_cannot_enqueue_steps_without_reclaim(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_expired_run")

    stale = engine.claim_command("wf_expired_run", worker_id="worker-a", lease_seconds=-1, command_type="run_workflow")
    assert stale is not None
    stale_result = engine._execute_run_workflow_command("wf_expired_run", stale)

    assert stale_result.status == "running"
    assert [event["type"] for event in engine.events("wf_expired_run")] == ["WorkflowStarted", "CommandClaimed"]
    assert [row["key"] for row in command_rows(db)] == ["workflow:run"]

    fresh = engine.claim_command("wf_expired_run", worker_id="worker-b", lease_seconds=60, command_type="run_workflow")
    assert fresh is not None
    assert fresh["attempts"] == 2
    fresh_result = engine._execute_run_workflow_command("wf_expired_run", fresh)
    assert fresh_result.status == "waiting"
    assert fresh_result.waiting_on == "gather:0"
    assert [row["key"] for row in command_rows(db)] == [
        "workflow:run",
        "step:worker_left:0",
        "step:worker_right:0",
    ]


def test_midflight_workflow_run_claim_loss_cannot_enqueue_step_requests(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    original_run_step = WorkflowContext.run_step
    injected = {"done": False}

    async def run_step_after_reclaim(self, step_name, args, kwargs, **options):
        if not injected["done"]:
            injected["done"] = True
            expire_claim_and_reclaim(
                self.engine,
                self.workflow_id,
                "workflow:run",
                worker_id="worker-b",
                command_type="run_workflow",
            )
        return await original_run_step(self, step_name, args, kwargs, **options)

    monkeypatch.setattr(WorkflowContext, "run_step", run_step_after_reclaim)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_midflight_enqueue")
    stale = engine.claim_command("wf_midflight_enqueue", worker_id="worker-a", lease_seconds=60, command_type="run_workflow")
    assert stale is not None

    result = engine._execute_command("wf_midflight_enqueue", stale)

    assert result.status == "running"
    assert injected["done"] is True
    assert [event["type"] for event in engine.events("wf_midflight_enqueue")] == [
        "WorkflowStarted",
        "CommandClaimed",
        "CommandClaimed",
    ]
    assert [row["key"] for row in command_rows(db)] == ["workflow:run"]
    assert command_row(db, "workflow:run")["claimed_by"] == "worker-b"


def test_midflight_workflow_run_claim_loss_cannot_complete_workflow(tmp_path):
    global MIDFLIGHT_RECLAIM_ENGINE
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    MIDFLIGHT_RECLAIM_ENGINE = engine
    workflow_id = "wf_midflight_complete"
    try:
        engine.start(worker_reclaims_before_return_workflow, {"workflow_id": workflow_id}, workflow_id=workflow_id)
        stale = engine.claim_command(workflow_id, worker_id="worker-a", lease_seconds=60, command_type="run_workflow")
        assert stale is not None

        result = engine._execute_command(workflow_id, stale)
    finally:
        MIDFLIGHT_RECLAIM_ENGINE = None

    assert result.status == "running"
    assert [event["type"] for event in engine.events(workflow_id)] == [
        "WorkflowStarted",
        "CommandClaimed",
        "CommandClaimed",
    ]
    assert engine.workflow_status(workflow_id)["status"] == "running"
    assert command_row(db, "workflow:run")["claimed_by"] == "worker-b"


def test_midflight_step_claim_loss_cannot_complete_step(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    workflow_id = "wf_midflight_step"
    engine.start(worker_reclaims_step_workflow, {}, workflow_id=workflow_id)
    queued_step = engine.worker_once(workflow_id, worker_id="worker-bootstrap", lease_seconds=60)
    assert queued_step.status == "waiting"
    stale = engine.claim_command(workflow_id, worker_id="worker-a", lease_seconds=60, command_type="run_step")
    assert stale is not None

    result = engine._execute_command(workflow_id, stale)

    assert result.status == "waiting"
    assert result.waiting_on == "step:worker_reclaims_step_before_return:0"
    event_types = [event["type"] for event in engine.events(workflow_id)]
    assert "StepCompleted" not in event_types
    assert "StepFailed" not in event_types
    row = command_row(db, "step:worker_reclaims_step_before_return:0")
    assert row is not None
    assert row["claimed_by"] == "worker-b"


def test_terminal_run_workflow_and_cancelled_commands_clear_claim_token(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_immediate_workflow, {"ok": True}, workflow_id="wf_terminal_token")
    claimed = engine.claim_command("wf_terminal_token", worker_id="worker-a", lease_seconds=60, command_type="run_workflow")
    assert claimed is not None
    assert claimed["claim_token"]

    result = engine._execute_command("wf_terminal_token", claimed)

    assert result.status == "completed"
    completed = command_row(db, "workflow:run")
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["claim_token"] is None

    engine.start(worker_stale_workflow, {}, workflow_id="wf_cancel_token")
    engine.worker_once("wf_cancel_token", worker_id="worker-bootstrap", lease_seconds=60)
    running = engine.claim_command("wf_cancel_token", worker_id="worker-a", lease_seconds=60, command_type="run_step")
    assert running is not None
    assert running["claim_token"]

    engine.cancel_workflow("wf_cancel_token", reason="test", source={"kind": "test"})

    cancelled = command_row(db, "step:worker_stale_sensitive:0")
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["claim_token"] is None


def test_terminal_run_workflow_cleanup_survives_post_terminal_lease_expiry(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    workflow_id = "wf_terminal_expired_cleanup"
    engine.start(worker_immediate_workflow, {"ok": True}, workflow_id=workflow_id)
    claimed = engine.claim_command(workflow_id, worker_id="worker-a", lease_seconds=60, command_type="run_workflow")
    assert claimed is not None
    original_run_decider = engine._run_decider

    def run_decider_then_expire_claim(active_workflow_id, workflow_fn):
        result = original_run_decider(active_workflow_id, workflow_fn)
        expired_at = int(time.time()) - 1
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE workflow_commands_outbox SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (expired_at, expired_at, claimed["id"]),
            )
        return result

    monkeypatch.setattr(engine, "_run_decider", run_decider_then_expire_claim)

    result = engine._execute_command(workflow_id, claimed)

    assert result.status == "completed"
    row = command_row(db, "workflow:run")
    assert row is not None
    assert row["status"] == "completed"
    assert row["claim_token"] is None


def test_old_db_running_commands_without_claim_tokens_are_requeued_on_migration(tmp_path):
    db = tmp_path / "old.sqlite"
    now = int(time.time())
    with sqlite3.connect(db) as con:
        con.execute(
            """
            CREATE TABLE workflow_commands_outbox(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workflow_id TEXT NOT NULL,
              type TEXT NOT NULL,
              key TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              claimed_by TEXT,
              lease_expires_at INTEGER,
              attempts INTEGER NOT NULL DEFAULT 0,
              last_error_json TEXT,
              created_at INTEGER NOT NULL,
              updated_at INTEGER,
              UNIQUE(workflow_id, key)
            )
            """
        )
        con.execute(
            """
            INSERT INTO workflow_commands_outbox(
              workflow_id, type, key, payload_json, status, claimed_by, lease_expires_at, attempts, created_at, updated_at
            ) VALUES (?, 'run_workflow', 'workflow:run', '{}', 'running', 'old-worker', ?, 1, ?, ?)
            """,
            ("wf_old", now + 3600, now, now),
        )

    WorkflowEngine(db)

    row = command_row(db, "workflow:run")
    assert row is not None
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    assert row["claim_token"] is None
    assert row["lease_expires_at"] is None


def test_existing_schema_tokenless_running_commands_are_requeued_on_open(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_immediate_workflow, {}, workflow_id="wf_existing_tokenless")
    claimed = engine.claim_command("wf_existing_tokenless", worker_id="old-worker", lease_seconds=3600, command_type="run_workflow")
    assert claimed is not None
    with sqlite3.connect(db) as con:
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET claim_token = NULL, lease_expires_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(time.time()) + 3600, int(time.time()), claimed["id"]),
        )

    WorkflowEngine(db)

    row = command_row(db, "workflow:run")
    assert row is not None
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    assert row["claim_token"] is None
    assert row["lease_expires_at"] is None


def test_worker_heartbeats_renew_long_running_step_lease(tmp_path):
    LEASE_OBSERVATIONS.clear()
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_slow_lease_workflow, {}, workflow_id="wf_slow")
    queued_step = engine.worker_once("wf_slow", worker_id="worker-bootstrap", lease_seconds=60)
    assert queued_step.status == "waiting"

    result = engine.worker_once("wf_slow", worker_id="worker-a", lease_seconds=2)

    assert result.status == "running"
    assert LEASE_OBSERVATIONS
    before, during = LEASE_OBSERVATIONS[-1]
    assert during > before



def test_workflow_run_wakeup_during_leased_run_is_preserved(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    original_wait_for = WorkflowContext.wait_for
    injected = {"done": False}

    async def wait_for_with_concurrent_signal(self, signal_type, *, key):
        try:
            return await original_wait_for(self, signal_type, key=key)
        except WorkflowWaiting:
            if signal_type == "go" and key == "k" and not injected["done"]:
                injected["done"] = True
                self.engine.signal(
                    self.workflow_id,
                    "go",
                    key="k",
                    payload={"value": "arrived while workflow:run was leased"},
                    source={"kind": "test"},
                    idempotency_key="concurrent-signal",
                )
            raise

    monkeypatch.setattr(WorkflowContext, "wait_for", wait_for_with_concurrent_signal)
    engine.start(worker_signal_wait_workflow, {}, workflow_id="wf_lost_wakeup")

    first = engine.worker_once("wf_lost_wakeup", worker_id="worker-a", lease_seconds=60)

    assert first.status == "waiting"
    assert command_row(db, "workflow:run")["status"] == "pending"
    final = engine.worker_once("wf_lost_wakeup", worker_id="worker-b", lease_seconds=60)
    assert final.status == "completed"
    assert final.result == {"signal": {"value": "arrived while workflow:run was leased"}}
    assert command_row(db, "workflow:run")["status"] == "completed"

def test_worker_service_drains_runnable_commands_across_configured_sources(tmp_path):
    db_one = tmp_path / "one.sqlite"
    db_two = tmp_path / "two.sqlite"
    WorkflowEngine(db_one).start(
        worker_gather_workflow,
        {"left": 1, "right": 2},
        workflow_id="wf_one",
        workflow_ref="tests.test_worker:worker_gather_workflow",
    )
    WorkflowEngine(db_two).start(
        worker_gather_workflow,
        {"left": 3, "right": 4},
        workflow_id="wf_two",
        workflow_ref="tests.test_worker:worker_gather_workflow",
    )

    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"one": str(db_one), "two": str(db_two)},
            "workflows": {
                "one-worker": {"workflow_ref": "tests.test_worker:worker_gather_workflow", "db": "one"},
                "two-worker": {"workflow_ref": "tests.test_worker:worker_gather_workflow", "db": "two"},
            },
        }
    )
    service = WorkflowWorkerService.from_registry(registry, worker_id="resident-worker")

    summary = service.tick()

    assert summary.executed == 10
    assert summary.errors == []
    assert WorkflowEngine(db_one).workflow_status("wf_one")["status"] == "completed"
    assert WorkflowEngine(db_two).workflow_status("wf_two")["status"] == "completed"



def test_worker_service_rejects_db_row_workflow_name_mismatch_for_allowlisted_ref(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(
        worker_gather_workflow,
        {"left": 1, "right": 2},
        workflow_id="wf_tampered",
        workflow_ref="tests.test_worker:worker_gather_workflow",
    )
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE workflow_instances SET workflow_name = ? WHERE id = ?",
            ("worker_signal_wait_workflow", "wf_tampered"),
        )

    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"service": str(db)},
            "workflows": {
                "safe-worker": {"workflow_ref": "tests.test_worker:worker_gather_workflow", "db": "service"}
            },
        }
    )
    service = WorkflowWorkerService.from_registry(registry, worker_id="resident-worker")

    summary = service.tick()

    assert summary.executed == 0
    assert len(summary.errors) == 1
    assert "does not match allowlisted workflow_ref" in summary.errors[0]["error"]
    assert command_row(db, "workflow:run")["status"] == "pending"


def test_worker_service_rejects_db_only_sources_without_allowlisted_workflows(tmp_path):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry.from_sources(config={"dbs": {"service": str(db)}})

    try:
        WorkflowWorkerService.from_registry(registry, db="service")
    except ValueError as exc:
        assert "has no configured workflow refs" in str(exc)
    else:
        raise AssertionError("worker service should reject DB-only sources without workflow ref allowlists")

def test_runnable_workflows_lists_pending_commands_without_known_workflow_id(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(
        worker_gather_workflow,
        {"left": 1, "right": 2},
        workflow_id="wf_runnable",
        workflow_ref="tests.test_worker:worker_gather_workflow",
    )

    runnable = engine.runnable_workflows()

    assert [row["workflow_id"] for row in runnable] == ["wf_runnable"]
    assert [row["command_type"] for row in runnable] == ["run_workflow"]
    assert [row["command_key"] for row in runnable] == ["workflow:run"]

    status = engine.workflow_status("wf_runnable")
    assert [command["diagnostic_label"] for command in status["pending_commands"]] == ["runnable_work"]
    assert status["diagnostics"] == [
        {
            "command_key": "workflow:run",
            "command_type": "run_workflow",
            "label": "runnable_work",
            "message": "Workflow has runnable work queued; a worker must claim this command for autonomous continuation.",
            "severity": "info",
        }
    ]

    first_pass = engine.worker_once("wf_runnable", worker_id="resident-worker", lease_seconds=60)
    assert first_pass.status == "waiting"

    runnable_after_workflow_run = engine.runnable_workflows()
    assert [row["command_type"] for row in runnable_after_workflow_run] == ["run_step", "run_step"]
    assert [row["command_key"] for row in runnable_after_workflow_run] == ["step:worker_left:0", "step:worker_right:0"]


def test_worker_heartbeat_lifecycle_and_runtime_projection(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    heartbeat = engine.record_worker_heartbeat(
        worker_id="resident-worker",
        worker_instance_id="resident-worker:instance-a",
        heartbeat_ttl_seconds=60,
        identity={
            "hostname": "test-host",
            "pid": 12345,
            "cwd": "/tmp/workflows",
            "python_executable": sys.executable,
            "python_version": "3.test",
            "platform": "test-platform",
            "hermes_version": "test-version",
            "agent_runner_enabled": False,
            "metadata": {"unsafe_user_supplied_metadata_is_not_persisted": "value"},
        },
    )

    assert heartbeat["worker_id"] == "resident-worker"
    assert heartbeat["worker_instance_id"] == "resident-worker:instance-a"
    assert heartbeat["status"] == "running"
    assert heartbeat["active"] is True
    assert heartbeat["environment"]["hostname"] == "test-host"
    assert heartbeat["metadata"] == {}
    assert engine.list_workers(active_only=True)[0]["worker_instance_id"] == "resident-worker:instance-a"

    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_worker_identity")
    claimed = engine.claim_command(
        "wf_worker_identity",
        worker_id="resident-worker",
        worker_instance_id="resident-worker:instance-a",
        lease_seconds=60,
        command_type=None,
    )

    assert claimed is not None
    assert claimed["claimed_by"] == "resident-worker"
    assert claimed["claimed_by_instance_id"] == "resident-worker:instance-a"
    status = engine.workflow_status("wf_worker_identity")
    assert status["runtime_state"]["primary"] == "running"
    assert status["runtime_state"]["command"]["claimed_by_instance_id"] == "resident-worker:instance-a"
    assert status["runtime_state"]["worker"]["worker_instance_id"] == "resident-worker:instance-a"
    assert status["runtime_state"]["worker"]["status"] == "running"

    engine.mark_worker_stopped(worker_id="resident-worker", worker_instance_id="resident-worker:instance-a")
    stopped = engine.list_workers()[0]
    assert stopped["status"] == "stopped"
    assert stopped["active"] is False


def test_requeued_workflow_command_clears_stale_worker_instance(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_requeue_identity")
    claimed = engine.claim_command(
        "wf_requeue_identity",
        worker_id="resident-worker",
        worker_instance_id="resident-worker:stale-instance",
        lease_seconds=60,
        command_type=None,
    )
    assert claimed is not None
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE workflow_commands_outbox SET status = 'completed', lease_expires_at = NULL WHERE id = ?",
            (claimed["id"],),
        )
    with engine._connect() as con:
        changed = engine._enqueue_workflow_run_row(con, "wf_requeue_identity", reason="test_requeue")

    assert changed is True
    row = command_row(db, "workflow:run")
    assert row is not None
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    assert row["claimed_by_instance_id"] is None
    status = engine.workflow_status("wf_requeue_identity")
    assert status["runtime_state"]["primary"] == "queued"
    assert status["runtime_state"]["worker"] is None


def test_read_only_old_db_without_worker_table_lists_no_workers(tmp_path):
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE old_schema_marker(id TEXT PRIMARY KEY)")

    assert WorkflowEngine(db, read_only=True).list_workers() == []


def test_worker_heartbeat_projects_stale_workers(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.record_worker_heartbeat(
        worker_id="resident-worker",
        worker_instance_id="resident-worker:stale",
        heartbeat_ttl_seconds=60,
        identity={"hostname": "test-host"},
    )
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE workflow_workers SET heartbeat_expires_at = 1, last_heartbeat_at = 1 WHERE worker_instance_id = ?",
            ("resident-worker:stale",),
        )

    worker = engine.list_workers()[0]
    assert worker["status"] == "stale"
    assert worker["active"] is False
    assert engine.list_workers(active_only=True) == []


def test_worker_service_records_heartbeat_and_claims_with_worker_instance(tmp_path):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).start(
        worker_gather_workflow,
        {"left": 1, "right": 2},
        workflow_id="wf_service_identity",
        workflow_ref="tests.test_worker:worker_gather_workflow",
    )
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"service": str(db)},
            "workflows": {
                "service-worker": {"workflow_ref": "tests.test_worker:worker_gather_workflow", "db": "service"}
            },
        }
    )
    service = WorkflowWorkerService.from_registry(registry, worker_id="resident-worker")

    summary = service.tick(max_commands=1)

    assert summary.executed == 1
    assert summary.worker_id == "resident-worker"
    assert summary.worker_instance_id == service.worker_instance_id
    workers = WorkflowEngine(db).list_workers(active_only=True)
    assert [worker["worker_instance_id"] for worker in workers] == [service.worker_instance_id]
    row = command_row(db, "workflow:run")
    assert row is not None
    assert row["claimed_by"] == "resident-worker"
    assert row["claimed_by_instance_id"] == service.worker_instance_id


def test_worker_service_records_heartbeat_before_scanning_when_idle(tmp_path):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"service": str(db)},
            "workflows": {
                "service-worker": {"workflow_ref": "tests.test_worker:worker_gather_workflow", "db": "service"}
            },
        }
    )
    service = WorkflowWorkerService.from_registry(registry, worker_id="resident-worker")

    summary = service.tick(max_commands=1)

    assert summary.executed == 0
    workers = WorkflowEngine(db).list_workers(active_only=True)
    assert [worker["worker_instance_id"] for worker in workers] == [service.worker_instance_id]
    assert workers[0]["metadata"]["source_db_name"] == "service"
    assert workers[0]["metadata"]["source_db_path"] == str(db)
    assert workers[0]["metadata"]["allowed_workflow_refs_count"] == 1
    assert "package_fingerprint" in workers[0]["metadata"]


def test_worker_service_active_command_heartbeat_matches_claimed_command(tmp_path):
    HEARTBEAT_OBSERVATIONS.clear()
    loaded_module = sys.modules.get("tests.test_worker")
    if loaded_module is not None and hasattr(loaded_module, "HEARTBEAT_OBSERVATIONS"):
        loaded_module.HEARTBEAT_OBSERVATIONS.clear()
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).start(
        worker_slow_lease_workflow,
        {},
        workflow_id="wf_service_active_command",
        workflow_ref="tests.test_worker:worker_slow_lease_workflow",
    )
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"service": str(db)},
            "workflows": {
                "service-worker": {"workflow_ref": "tests.test_worker:worker_slow_lease_workflow", "db": "service"}
            },
        }
    )
    service = WorkflowWorkerService.from_registry(registry, worker_id="resident-worker")
    service.heartbeat_ttl_seconds = 3

    first = service.tick(max_commands=1)
    assert first.executed == 1
    second = service.tick(max_commands=1)

    assert second.executed == 1
    assert second.executions[0].command_id == command_row(db, "step:worker_slow_lease_probe:0")["id"]
    assert second.executions[0].heartbeat_status == "running"
    loaded_observations = getattr(sys.modules.get("tests.test_worker"), "HEARTBEAT_OBSERVATIONS", [])
    observations = [*HEARTBEAT_OBSERVATIONS, *loaded_observations]
    active = [metadata.get("active_command") for metadata in observations if metadata.get("active_command")]
    assert active
    assert active[-1] == {
        "command_id": command_row(db, "step:worker_slow_lease_probe:0")["id"],
        "command_type": "run_step",
        "command_key": "step:worker_slow_lease_probe:0",
        "workflow_id": "wf_service_active_command",
    }
    row = command_row(db, "step:worker_slow_lease_probe:0")
    assert row["claimed_by"] == "resident-worker"
    assert row["claimed_by_instance_id"] == service.worker_instance_id


def test_worker_service_serve_marks_worker_stopped_for_one_shot_execution(tmp_path):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).start(
        worker_gather_workflow,
        {"left": 1, "right": 2},
        workflow_id="wf_service_once_identity",
        workflow_ref="tests.test_worker:worker_gather_workflow",
    )
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"service": str(db)},
            "workflows": {
                "service-worker": {"workflow_ref": "tests.test_worker:worker_gather_workflow", "db": "service"}
            },
        }
    )
    service = WorkflowWorkerService.from_registry(registry, worker_id="resident-worker")

    summary = service.serve(max_commands=1, idle_exit_after=0)

    assert summary.executed == 1
    workers = WorkflowEngine(db).list_workers()
    assert [worker["worker_instance_id"] for worker in workers] == [service.worker_instance_id]
    assert workers[0]["status"] == "stopped"
    assert workers[0]["active"] is False


def test_worker_service_serve_marks_idle_one_shot_worker_stopped(tmp_path):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"service": str(db)},
            "workflows": {
                "service-worker": {"workflow_ref": "tests.test_worker:worker_gather_workflow", "db": "service"}
            },
        }
    )
    service = WorkflowWorkerService.from_registry(registry, worker_id="resident-worker")

    summary = service.serve(max_commands=1, idle_exit_after=0)

    assert summary.executed == 0
    workers = WorkflowEngine(db).list_workers()
    assert [worker["worker_instance_id"] for worker in workers] == [service.worker_instance_id]
    assert workers[0]["status"] == "stopped"
    assert workers[0]["active"] is False


def test_worker_service_sleep_is_capped_by_heartbeat_ttl(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"service": str(db)},
            "workflows": {
                "service-worker": {"workflow_ref": "tests.test_worker:worker_gather_workflow", "db": "service"}
            },
        }
    )
    service = WorkflowWorkerService.from_registry(registry, worker_id="resident-worker")
    service.heartbeat_ttl_seconds = 9
    sleeps = []

    def stop_after_first_sleep(duration):
        sleeps.append(duration)
        raise RuntimeError("stop after sleep observation")

    monkeypatch.setattr("hermes_workflows.worker_service.time.sleep", stop_after_first_sleep)

    try:
        service.serve(poll_interval=999, max_commands=1, idle_exit_after=60)
    except RuntimeError as exc:
        assert str(exc) == "stop after sleep observation"
    else:
        raise AssertionError("serve should have reached the monkeypatched sleep")

    assert sleeps == [3.0]
    assert WorkflowEngine(db).list_workers()[0]["status"] == "stopped"


def test_list_workers_reraises_unexpected_operational_errors(tmp_path):
    db = tmp_path / "broken.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE VIEW workflow_workers AS SELECT * FROM missing_backing_table")

    try:
        WorkflowEngine(db, read_only=True).list_workers()
    except sqlite3.OperationalError as exc:
        assert "missing_backing_table" in str(exc)
    else:
        raise AssertionError("unexpected workflow_workers OperationalError should be re-raised")


CLI_WORKFLOW_MODULE = '''
from hermes_workflows import step, workflow

@step
async def make_worker_plan(inputs):
    return {"summary": f"Plan for {inputs['destination']}"}

@workflow
async def cli_worker_workflow(inputs):
    plan = await make_worker_plan(inputs)
    return {"plan": plan}
'''


def run_cli(tmp_path, *args):
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}:{tmp_path}:{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "hermes_workflows", *args],
        cwd=Path.cwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_cli_worker_process_executes_pending_command_across_processes(tmp_path):
    (tmp_path / "worker_wf.py").write_text(CLI_WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    start_result = run_cli(
        tmp_path,
        "start",
        "worker_wf:cli_worker_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli_worker",
        "--input-json",
        '{"destination":"NYC"}',
    )
    assert json.loads(start_result.stdout)["status"] == "running"
    assert json.loads(start_result.stdout)["waiting_on"] is None

    first_worker_result = run_cli(
        tmp_path,
        "worker",
        "worker_wf:cli_worker_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli_worker",
        "--worker-id",
        "cli-worker-1",
        "--once",
    )
    assert json.loads(first_worker_result.stdout)["waiting_on"] == "step:make_worker_plan:0"

    worker_result = run_cli(
        tmp_path,
        "worker",
        "worker_wf:cli_worker_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli_worker",
        "--worker-id",
        "cli-worker-1",
        "--max-commands",
        "2",
    )

    assert json.loads(worker_result.stdout) == {
        "workflow_id": "wf_cli_worker",
        "status": "completed",
        "waiting_on": None,
        "result": {"plan": {"summary": "Plan for NYC"}},
        "error": None,
    }


def test_cli_worker_config_mode_executes_pending_command_without_workflow_id_or_ref(tmp_path):
    (tmp_path / "worker_wf.py").write_text(CLI_WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    registry = tmp_path / "workflows.registry.json"
    registry.write_text(
        json.dumps(
            {
                "dbs": {"service": str(db)},
                "workflows": {
                    "cli-worker": {"workflow_ref": "worker_wf:cli_worker_workflow", "db": "service"}
                },
            }
        )
    )

    start_result = run_cli(
        tmp_path,
        "start",
        "worker_wf:cli_worker_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli_worker_service",
        "--input-json",
        '{"destination":"SFO"}',
    )
    assert json.loads(start_result.stdout)["status"] == "running"
    assert json.loads(start_result.stdout)["waiting_on"] is None

    worker_result = run_cli(
        tmp_path,
        "worker",
        "--config",
        str(registry),
        "--db",
        "service",
        "--worker-id",
        "resident-cli-worker",
        "--max-commands",
        "3",
    )

    payload = json.loads(worker_result.stdout)
    assert payload["executed"] == 3
    assert payload["errors"] == []
    assert payload["executions"][0]["workflow_id"] == "wf_cli_worker_service"
    assert payload["executions"][-1]["status"] == "completed"

    status_result = run_cli(
        tmp_path,
        "status",
        "--db",
        str(db),
        "--id",
        "wf_cli_worker_service",
    )
    assert json.loads(status_result.stdout)["status"] == "completed"

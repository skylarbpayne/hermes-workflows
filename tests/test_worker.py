import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from hermes_workflows import WorkflowEngine, step, workflow


RUNS = []
SHOULD_FAIL_STALE = False


@step
async def worker_left(ctx, value):
    RUNS.append(("left", value))
    return {"side": "left", "value": value}


@step
async def worker_right(ctx, value):
    RUNS.append(("right", value))
    return {"side": "right", "value": value}


@step
async def worker_boom(ctx, value):
    raise RuntimeError(f"boom {value}")


@step
async def worker_stale_sensitive(ctx):
    if SHOULD_FAIL_STALE:
        raise RuntimeError("stale worker should not win")
    return "fresh result"


@workflow
async def worker_gather_workflow(ctx, inputs):
    left, right = await ctx.gather(
        worker_left(ctx, inputs["left"]),
        worker_right(ctx, inputs["right"]),
    )
    return {"left": left, "right": right}


@workflow
async def worker_failure_workflow(ctx, inputs):
    return await worker_boom(ctx, inputs["value"])


@workflow
async def worker_stale_workflow(ctx, inputs):
    return await worker_stale_sensitive(ctx)


def command_row(db, key):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM workflow_commands_outbox WHERE key = ?", (key,)).fetchone())
    finally:
        con.close()


def test_worker_claims_one_pending_command_with_a_lease(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_worker")

    claimed = engine.claim_command("wf_worker", worker_id="worker-a", lease_seconds=60)

    assert claimed["key"] == "step:worker_left:0"
    assert claimed["status"] == "running"
    assert claimed["claimed_by"] == "worker-a"
    assert claimed["attempts"] == 1
    assert engine.claim_command("wf_worker", worker_id="worker-b", lease_seconds=60)["key"] == "step:worker_right:0"
    assert command_row(db, "step:worker_left:0")["claimed_by"] == "worker-a"


def test_expired_running_command_can_be_reclaimed(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_worker")

    first = engine.claim_command("wf_worker", worker_id="worker-a", lease_seconds=-1)
    reclaimed = engine.claim_command("wf_worker", worker_id="worker-b", lease_seconds=60)

    assert reclaimed["id"] == first["id"]
    assert reclaimed["claimed_by"] == "worker-b"
    assert reclaimed["attempts"] == 2


def test_worker_once_executes_claimed_steps_until_workflow_completes(tmp_path):
    RUNS.clear()
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_gather_workflow, {"left": 1, "right": 2}, workflow_id="wf_worker")

    first = engine.worker_once("wf_worker", worker_id="worker-a", lease_seconds=60)
    assert first.status == "waiting"
    assert RUNS == [("left", 1)]

    second = engine.worker_once("wf_worker", worker_id="worker-b", lease_seconds=60)
    assert second.status == "completed"
    assert second.result == {
        "left": {"side": "left", "value": 1},
        "right": {"side": "right", "value": 2},
    }
    assert RUNS == [("left", 1), ("right", 2)]
    assert command_row(db, "step:worker_left:0")["status"] == "completed"
    assert command_row(db, "step:worker_right:0")["status"] == "completed"


def test_worker_records_step_failure_and_marks_command_failed(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(worker_failure_workflow, {"value": "bad"}, workflow_id="wf_fail")

    result = engine.worker_once("wf_fail", worker_id="worker-a", lease_seconds=60)

    assert result.status == "failed"
    assert result.error is not None
    assert "RuntimeError: boom bad" in result.error
    row = command_row(db, "step:worker_boom:0")
    assert row["status"] == "failed"
    assert json.loads(row["last_error_json"]) == {"type": "RuntimeError", "message": "boom bad"}

    reloaded = WorkflowEngine(db).worker_once("wf_fail", worker_id="worker-b", lease_seconds=60)
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

    stale = engine.claim_command("wf_stale", worker_id="worker-a", lease_seconds=-1)
    fresh = engine.claim_command("wf_stale", worker_id="worker-b", lease_seconds=60)
    assert stale is not None
    assert fresh is not None
    assert fresh["attempts"] == 2

    completed = engine._execute_run_step_command("wf_stale", fresh)
    assert completed.status == "completed"
    assert completed.result == "fresh result"

    SHOULD_FAIL_STALE = True
    try:
        stale_result = engine._execute_run_step_command("wf_stale", stale)
    finally:
        SHOULD_FAIL_STALE = False

    assert stale_result.status == "completed"
    assert stale_result.result == "fresh result"
    assert command_row(db, "step:worker_stale_sensitive:0")["status"] == "completed"
    assert [event["type"] for event in engine.events("wf_stale")].count("StepFailed") == 0


CLI_WORKFLOW_MODULE = '''
from hermes_workflows import step, workflow

@step
async def make_worker_plan(ctx, inputs):
    return {"summary": f"Plan for {inputs['destination']}"}

@workflow
async def cli_worker_workflow(ctx, inputs):
    plan = await make_worker_plan(ctx, inputs)
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
    assert json.loads(start_result.stdout)["waiting_on"] == "step:make_worker_plan:0"

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
        "--once",
    )

    assert json.loads(worker_result.stdout) == {
        "workflow_id": "wf_cli_worker",
        "status": "completed",
        "waiting_on": None,
        "result": {"plan": {"summary": "Plan for NYC"}},
        "error": None,
    }

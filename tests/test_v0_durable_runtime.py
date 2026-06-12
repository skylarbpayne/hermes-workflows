import pytest

from hermes_workflows import WorkflowEngine, workflow, step


@step
async def collect_constraints(ctx, inputs):
    raise AssertionError("step body should not run inside the decider")


@step
async def draft_options(ctx, constraints):
    raise AssertionError("step body should not run inside the decider")


@workflow
async def trip_planning(ctx, inputs):
    constraints = await collect_constraints(ctx, inputs)
    options = await draft_options(ctx, constraints)
    approval = await ctx.wait_for("approval.granted", key="approve_trip_plan")
    return {"options": options, "approved_by": approval["by"]}


def test_workflow_exits_after_enqueueing_workflow_run_and_survives_restart(tmp_path):
    db = tmp_path / "wf.sqlite"

    engine = WorkflowEngine(db)
    started = engine.start(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip")

    assert started.status == "running"
    assert started.waiting_on is None
    assert engine.pending_commands("wf_trip") == [
        {
            "type": "run_workflow",
            "key": "workflow:run",
            "payload": {"reason": "start"},
        }
    ]

    first_decider = engine.worker_once("wf_trip", worker_id="worker-1")
    assert first_decider.status == "waiting"
    assert first_decider.waiting_on == "step:collect_constraints:0"
    assert engine.pending_commands("wf_trip")[-1] == {
        "type": "run_step",
        "key": "step:collect_constraints:0",
        "payload": {
            "step_name": "collect_constraints",
            "args": [{"destination": "NYC"}],
            "kwargs": {},
        },
    }

    restarted = WorkflowEngine(db)
    resumed = restarted.complete_step(
        "wf_trip",
        "step:collect_constraints:0",
        {"hard": ["no red eyes"], "soft": ["boutique hotel"]},
    )

    assert resumed.status == "running"
    assert resumed.waiting_on is None
    resumed = restarted.worker_once("wf_trip", worker_id="worker-2")
    assert resumed.status == "waiting"
    assert resumed.waiting_on == "step:draft_options:0"
    assert restarted.pending_commands("wf_trip")[-1] == {
        "type": "run_step",
        "key": "step:draft_options:0",
        "payload": {
            "step_name": "draft_options",
            "args": [{"hard": ["no red eyes"], "soft": ["boutique hotel"]}],
            "kwargs": {},
        },
    }


def test_completed_steps_are_memoized_and_manual_signal_wakes_workflow(tmp_path):
    db = tmp_path / "wf.sqlite"
    engine = WorkflowEngine(db)
    engine.start(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip")
    engine.worker_once("wf_trip", worker_id="worker-start")
    engine.complete_step("wf_trip", "step:collect_constraints:0", {"hard": ["no red eyes"]})
    engine.worker_once("wf_trip", worker_id="worker-after-constraints")
    waiting_for_approval = engine.complete_step(
        "wf_trip",
        "step:draft_options:0",
        {"summary": "NYC plan", "hotel": "The Ludlow"},
    )
    assert waiting_for_approval.status == "running"
    waiting_for_approval = engine.worker_once("wf_trip", worker_id="worker-after-options")

    assert waiting_for_approval.status == "waiting"
    assert waiting_for_approval.waiting_on == "signal:approval.granted:approve_trip_plan"
    assert [event["type"] for event in engine.events("wf_trip")].count("StepRequested") == 2

    restarted = WorkflowEngine(db)
    signalled = restarted.signal(
        "wf_trip",
        "approval.granted",
        key="approve_trip_plan",
        payload={"by": "skylar", "decision": "approved"},
        idempotency_key="discord-message-1",
    )

    assert signalled.status == "running"
    completed = restarted.worker_once("wf_trip", worker_id="worker-after-signal")
    assert completed.status == "completed"
    assert completed.result == {
        "options": {"summary": "NYC plan", "hotel": "The Ludlow"},
        "approved_by": "skylar",
    }

    commands = restarted.pending_commands("wf_trip")
    assert [command["key"] for command in commands if command["type"] == "run_step"] == [
        "step:collect_constraints:0",
        "step:draft_options:0",
    ]
    assert [event["type"] for event in restarted.events("wf_trip")].count("SignalReceived") == 1

    duplicate = restarted.signal(
        "wf_trip",
        "approval.granted",
        key="approve_trip_plan",
        payload={"by": "skylar", "decision": "approved"},
        idempotency_key="discord-message-1",
    )
    assert duplicate.status == "completed"
    assert [event["type"] for event in restarted.events("wf_trip")].count("SignalReceived") == 1


def test_read_only_engine_rejects_mutation_methods_before_sqlite_write(tmp_path):
    db = tmp_path / "wf.sqlite"
    writer = WorkflowEngine(db)
    writer.start(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip")
    writer.worker_once("wf_trip", worker_id="writer")

    reader = WorkflowEngine(db, read_only=True)
    assert reader.workflow_status("wf_trip")["waiting_on"] == "step:collect_constraints:0"

    with pytest.raises(RuntimeError, match="WorkflowEngine is read-only"):
        reader.start(trip_planning, {"destination": "LA"}, workflow_id="wf_other")
    with pytest.raises(RuntimeError, match="WorkflowEngine is read-only"):
        reader.complete_step("wf_trip", "step:collect_constraints:0", {"hard": ["no red eyes"]})
    with pytest.raises(RuntimeError, match="WorkflowEngine is read-only"):
        reader.signal("wf_trip", "approval.granted", key="approve_trip_plan", payload={"by": "skylar"})
    with pytest.raises(RuntimeError, match="WorkflowEngine is read-only"):
        reader.claim_command("wf_trip", worker_id="read-only-worker")

    assert [event["type"] for event in writer.events("wf_trip")] == ["WorkflowStarted", "CommandClaimed", "StepRequested"]

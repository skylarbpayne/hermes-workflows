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


def test_workflow_exits_after_enqueueing_first_step_and_survives_restart(tmp_path):
    db = tmp_path / "wf.sqlite"

    engine = WorkflowEngine(db)
    started = engine.start(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip")

    assert started.status == "waiting"
    assert started.waiting_on == "step:collect_constraints:0"
    assert engine.pending_commands("wf_trip") == [
        {
            "type": "run_step",
            "key": "step:collect_constraints:0",
            "payload": {
                "step_name": "collect_constraints",
                "args": [{"destination": "NYC"}],
                "kwargs": {},
            },
        }
    ]

    restarted = WorkflowEngine(db)
    resumed = restarted.complete_step(
        "wf_trip",
        "step:collect_constraints:0",
        {"hard": ["no red eyes"], "soft": ["boutique hotel"]},
    )

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


def test_completed_steps_are_memoized_and_manual_signal_resumes_workflow(tmp_path):
    db = tmp_path / "wf.sqlite"
    engine = WorkflowEngine(db)
    engine.start(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip")
    engine.complete_step("wf_trip", "step:collect_constraints:0", {"hard": ["no red eyes"]})
    waiting_for_approval = engine.complete_step(
        "wf_trip",
        "step:draft_options:0",
        {"summary": "NYC plan", "hotel": "The Ludlow"},
    )

    assert waiting_for_approval.status == "waiting"
    assert waiting_for_approval.waiting_on == "signal:approval.granted:approve_trip_plan"
    assert [event["type"] for event in engine.events("wf_trip")].count("StepRequested") == 2

    restarted = WorkflowEngine(db)
    completed = restarted.signal(
        "wf_trip",
        "approval.granted",
        key="approve_trip_plan",
        payload={"by": "skylar", "decision": "approved"},
        idempotency_key="discord-message-1",
    )

    assert completed.status == "completed"
    assert completed.result == {
        "options": {"summary": "NYC plan", "hotel": "The Ludlow"},
        "approved_by": "skylar",
    }

    commands = restarted.pending_commands("wf_trip")
    assert [command["key"] for command in commands] == [
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

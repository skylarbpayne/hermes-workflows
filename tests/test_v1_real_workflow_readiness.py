from hermes_workflows import WorkflowEngine, step, workflow


STEP_RUNS = []


@step
async def compose_trip_plan(ctx, inputs):
    STEP_RUNS.append({"step": "compose_trip_plan", "inputs": inputs, "workflow_id": ctx.workflow_id})
    return {
        "summary": f"Trip plan for {inputs['destination']}",
        "gesture": "Reserve one quiet dinner slot and pick up a small gift before travel day.",
    }


@step
async def package_trip_plan(ctx, plan, decision):
    STEP_RUNS.append({"step": "package_trip_plan", "plan": plan, "decision": decision})
    return {
        "package": plan,
        "approved_by": decision["by"],
        "ready": True,
    }


@workflow
async def approval_trip_workflow(ctx, inputs):
    plan = await compose_trip_plan(ctx, inputs)
    decision = await ctx.approval.request(
        "Approve trip plan before packaging?",
        key="approve_trip_plan",
        artifact=plan,
        approver="human:skylar",
        allowed=["approve", "reject", "edit", "rerun"],
    )
    if decision["action"] != "approve":
        return {"ready": False, "decision": decision}
    return await package_trip_plan(ctx, plan, decision)


def test_run_until_idle_executes_local_steps_once_then_waits_for_approval(tmp_path):
    STEP_RUNS.clear()
    db = tmp_path / "wf.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        approval_trip_workflow,
        {"destination": "NYC"},
        workflow_id="wf_real_trip",
    )

    assert result.status == "waiting"
    assert result.waiting_on == "signal:approval.decision:approve_trip_plan"
    assert STEP_RUNS == [
        {
            "step": "compose_trip_plan",
            "inputs": {"destination": "NYC"},
            "workflow_id": "wf_real_trip",
        }
    ]

    events = engine.events("wf_real_trip")
    assert [event["type"] for event in events] == [
        "WorkflowStarted",
        "StepRequested",
        "StepCompleted",
        "ApprovalRequested",
        "WaitRequested",
    ]
    approval = next(event for event in events if event["type"] == "ApprovalRequested")
    assert approval["payload"]["prompt"] == "Approve trip plan before packaging?"
    assert approval["payload"]["approver"] == "human:skylar"
    assert approval["payload"]["artifact"]["summary"] == "Trip plan for NYC"

    restarted = WorkflowEngine(db)
    again = restarted.drain("wf_real_trip")
    assert again.status == "waiting"
    assert STEP_RUNS == [
        {
            "step": "compose_trip_plan",
            "inputs": {"destination": "NYC"},
            "workflow_id": "wf_real_trip",
        }
    ]


def test_signal_approval_resumes_and_drains_downstream_steps(tmp_path):
    STEP_RUNS.clear()
    db = tmp_path / "wf.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(approval_trip_workflow, {"destination": "NYC"}, workflow_id="wf_real_trip")

    restarted = WorkflowEngine(db)
    completed = restarted.signal(
        "wf_real_trip",
        "approval.decision",
        key="approve_trip_plan",
        payload={"action": "approve", "by": "skylar"},
        idempotency_key="discord-approval-1",
    )

    assert completed.status == "completed"
    assert completed.result == {
        "package": {
            "summary": "Trip plan for NYC",
            "gesture": "Reserve one quiet dinner slot and pick up a small gift before travel day.",
        },
        "approved_by": "skylar",
        "ready": True,
    }
    assert [run["step"] for run in STEP_RUNS] == ["compose_trip_plan", "package_trip_plan"]

    duplicate = restarted.signal(
        "wf_real_trip",
        "approval.decision",
        key="approve_trip_plan",
        payload={"action": "approve", "by": "skylar"},
        idempotency_key="discord-approval-1",
    )
    assert duplicate.status == "completed"
    assert [run["step"] for run in STEP_RUNS] == ["compose_trip_plan", "package_trip_plan"]

    assert [event["type"] for event in restarted.events("wf_real_trip")] == [
        "WorkflowStarted",
        "StepRequested",
        "StepCompleted",
        "ApprovalRequested",
        "WaitRequested",
        "SignalReceived",
        "StepRequested",
        "StepCompleted",
        "WorkflowCompleted",
    ]

from hermes_workflows import ApprovalDecisionInput, WorkflowEngine, approve, approve_many, step, workflow, workflow_id


STEP_RUNS = []


@step
async def compose_trip_plan(inputs):
    STEP_RUNS.append({"step": "compose_trip_plan", "inputs": inputs, "workflow_id": workflow_id()})
    return {
        "summary": f"Trip plan for {inputs['destination']}",
        "gesture": "Reserve one quiet dinner slot and pick up a small gift before travel day.",
    }


@step
async def package_trip_plan(plan, decision):
    STEP_RUNS.append({"step": "package_trip_plan", "plan": plan, "decision": decision})
    return {
        "package": plan,
        "approved_by": decision["by"],
        "ready": True,
    }


@workflow
async def approval_trip_workflow(inputs):
    plan = await compose_trip_plan(inputs)
    decision = await approve(
        "Approve trip plan before packaging?",
        key="approve_trip_plan",
        artifact=plan,
        allowed=["approve", "reject", "edit", "rerun"],
    )
    if decision["action"] != "approve":
        return {"ready": False, "decision": decision}
    return await package_trip_plan(plan, decision)


@workflow
async def bulk_approval_workflow(inputs):
    decisions = await approve_many(
        [
            {
                "prompt": "Approve entity A?",
                "key": "entity_a",
                "artifact": {"kind": "entity", "name": "A"},
            },
            {
                "prompt": "Approve entity B?",
                "key": "entity_b",
                "artifact": {"kind": "entity", "name": "B"},
            },
        ],
    )
    return {"decisions": decisions}


def test_request_many_emits_every_atomic_approval_before_waiting(tmp_path):
    db = tmp_path / "wf.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(bulk_approval_workflow, {}, workflow_id="wf_bulk_approval")

    assert result.status == "waiting"
    events = engine.events("wf_bulk_approval")
    approvals = [event for event in events if event["type"] == "ApprovalRequested"]
    waits = [event for event in events if event["type"] == "WaitRequested"]
    assert [event["payload"]["key"] for event in approvals] == ["entity_a", "entity_b"]
    assert [event["payload"]["prompt"] for event in approvals] == ["Approve entity A?", "Approve entity B?"]
    assert {event["payload"]["key"] for event in waits} == {"entity_a", "entity_b"}

    active = engine.list_approvals(status="waiting")
    assert [item.key for item in active] == ["entity_a", "entity_b"]


def test_request_many_waits_until_every_atomic_approval_is_decided(tmp_path):
    db = tmp_path / "wf.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(bulk_approval_workflow, {}, workflow_id="wf_bulk_approval")

    recorded_first = engine.signal(
        "wf_bulk_approval",
        "approval.decision",
        key="entity_a",
        payload={"action": "approve", "by": "skylar"},
        source={"channel": "dashboard", "message_id": "approval-a"},
        idempotency_key="approval-a",
    )

    assert recorded_first.status == "running"
    after_first = engine.drain("wf_bulk_approval")
    assert after_first.status == "waiting"
    assert after_first.waiting_on == "signals:approval.decision:entity_b"
    assert [item.key for item in engine.list_approvals(status="waiting")] == ["entity_b"]
    assert engine.workflow_status("wf_bulk_approval")["result"] is None

    recorded_second = engine.signal(
        "wf_bulk_approval",
        "approval.decision",
        key="entity_b",
        payload={"action": "reject", "by": "skylar", "reason": "not needed"},
        source={"channel": "dashboard", "message_id": "approval-b"},
        idempotency_key="approval-b",
    )

    assert recorded_second.status == "running"
    after_second = engine.drain("wf_bulk_approval")
    assert after_second.status == "completed"
    assert after_second.result["decisions"][0] == {
        "key": "entity_a",
        "action": "approve",
        "by": "skylar",
        "source": {"channel": "dashboard", "message_id": "approval-a"},
    }
    assert after_second.result["decisions"][1] == {
        "key": "entity_b",
        "action": "reject",
        "by": "skylar",
        "reason": "not needed",
        "source": {"channel": "dashboard", "message_id": "approval-b"},
    }
    assert engine.list_approvals(status="waiting") == []


def test_submit_approval_decision_preserves_rejection_feedback(tmp_path):
    db = tmp_path / "wf.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(approval_trip_workflow, {"destination": "NYC"}, workflow_id="wf_feedback")

    engine.submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_feedback",
            key="approve_trip_plan",
            action="reject",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "dashboard", "message_id": "feedback-1"},
            reason="too generic; make it operational",
            note="use concrete receipts",
            idempotency_key="feedback-1",
        ),
        resume=False,
    )

    signal = next(event for event in engine.events("wf_feedback") if event["type"] == "SignalReceived")
    assert signal["payload"]["payload"] == {
        "action": "reject",
        "by": "skylar",
        "note": "use concrete receipts",
        "reason": "too generic; make it operational",
    }


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
        "CommandClaimed",
        "StepRequested",
        "CommandClaimed",
        "StepCompleted",
        "CommandClaimed",
        "StepRequested",
        "ApprovalRequested",
        "WaitRequested",
    ]
    approval = next(event for event in events if event["type"] == "ApprovalRequested")
    assert approval["payload"]["prompt"] == "Approve trip plan before packaging?"
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
    recorded = restarted.signal(
        "wf_real_trip",
        "approval.decision",
        key="approve_trip_plan",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/1/message/30"},
        idempotency_key="discord-approval-1",
    )

    assert recorded.status == "running"
    completed = restarted.drain("wf_real_trip")
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
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/1/message/30"},
        idempotency_key="discord-approval-1",
    )
    assert duplicate.status == "completed"
    assert [run["step"] for run in STEP_RUNS] == ["compose_trip_plan", "package_trip_plan"]

    assert [event["type"] for event in restarted.events("wf_real_trip")] == [
        "WorkflowStarted",
        "CommandClaimed",
        "StepRequested",
        "CommandClaimed",
        "StepCompleted",
        "CommandClaimed",
        "StepRequested",
        "ApprovalRequested",
        "WaitRequested",
        "SignalReceived",
        "StepCompleted",
        "CommandClaimed",
        "StepRequested",
        "CommandClaimed",
        "StepCompleted",
        "CommandClaimed",
        "WorkflowCompleted",
    ]

import pytest

from hermes_workflows import WorkflowEngine, approve, step, wait_for, workflow


@workflow
async def approval_workflow(inputs):
    decision = await approve(
        "Approve the test plan?",
        key="approve_test_plan",
        artifact={"plan": "test"},
        allowed=["approve", "reject"],
    )
    return {"decision": decision}


@step
async def prepare_approval_artifact(inputs):
    return {"plan": inputs.get("plan", "test")}


@workflow
async def approval_after_step_workflow(inputs):
    artifact = await prepare_approval_artifact(inputs)
    decision = await approve(
        "Approve the prepared test plan?",
        key="approve_test_plan",
        artifact=artifact,
    )
    return {"decision": decision}


@workflow
async def approval_then_wait_workflow(inputs):
    decision = await approve(
        "Approve before waiting for follow-up?",
        key="approve_test_plan",
        artifact={"plan": "test"},
    )
    follow_up = await wait_for("followup.ready", key="continue")
    return {"decision": decision, "follow_up": follow_up}


@workflow
async def immediate_workflow(inputs):
    return {"ok": True}


def human_source(message_id="456"):
    return {
        "kind": "human",
        "id": "skylar",
        "channel": "discord",
        "message_url": f"discord://thread/123/message/{message_id}",
    }


def approve_payload():
    return {"action": "approve", "by": "skylar"}


def reject_payload():
    return {"action": "reject", "by": "skylar"}


def test_approval_rejects_source_without_external_provenance(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")
    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_test_plan"

    with pytest.raises(ValueError, match="requires external decision provenance"):
        WorkflowEngine(tmp_path / "workflow.sqlite").signal(
            "wf_approval",
            "approval.decision",
            key="approve_test_plan",
            payload={"action": "approve", "by": "palmer"},
            source={"kind": "agent", "id": "palmer", "channel": "test"},
            idempotency_key="agent-approval",
        )

    status = WorkflowEngine(tmp_path / "workflow.sqlite").workflow_status("wf_approval", recent_events=10)
    assert status["status"] == "waiting"
    assert status["waiting_on"] == "signal:approval.decision:approve_test_plan"
    assert status["pending_commands"][0]["key"] == "approval:approve_test_plan"
    assert not [event for event in status["events"] if event["type"] == "SignalReceived"]


def test_human_approval_accepts_human_source_and_returns_provenance(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")
    assert first.status == "waiting"

    signaler = WorkflowEngine(tmp_path / "workflow.sqlite")
    recorded = signaler.signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload=approve_payload(),
        source=human_source(),
        idempotency_key="human-approval",
    )

    assert recorded.status == "running"
    result = signaler.drain("wf_approval")
    assert result.status == "completed"
    decision = result.result["decision"]
    assert decision["action"] == "approve"
    assert decision["by"] == "skylar"
    assert decision["source"] == {
        "channel": "discord",
        "message_url": "discord://thread/123/message/456",
    }
    assert decision.to_dict() == {
        "action": "approve",
        "by": "skylar",
        "source": {
            "channel": "discord",
            "message_url": "discord://thread/123/message/456",
        },
    }
    assert decision.response_provenance == {
        "schema_version": 1,
        "kind": "legacy_unverified",
        "principal": None,
        "display_label": "skylar",
        "event": {
            "channel": "discord",
            "message_id": None,
            "message_url": "discord://thread/123/message/456",
            "event_id": None,
        },
    }


def test_local_dashboard_decision_is_truthfully_unattributed():
    from hermes_workflows.approvals import ApprovalDecision

    decision = ApprovalDecision(
        action="approve",
        source={"channel": "local-dashboard", "event_id": "dashboard:click-1"},
    )

    assert decision.to_dict() == {
        "action": "approve",
        "source": {"channel": "local-dashboard", "event_id": "dashboard:click-1"},
    }
    assert decision.response_provenance == {
        "schema_version": 1,
        "kind": "unattributed_local_operator",
        "principal": None,
        "display_label": None,
        "event": {
            "channel": "local-dashboard",
            "message_id": None,
            "message_url": None,
            "event_id": "dashboard:click-1",
        },
    }


def test_human_approval_rejects_missing_source(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")

    with pytest.raises(ValueError, match="requires external decision provenance"):
        WorkflowEngine(tmp_path / "workflow.sqlite").signal(
            "wf_approval",
            "approval.decision",
            key="approve_test_plan",
            payload={"action": "approve", "by": "skylar-current-chat"},
            idempotency_key="missing-source-approval",
        )

    assert WorkflowEngine(tmp_path / "workflow.sqlite").workflow_status("wf_approval")["status"] == "waiting"


def test_human_approval_rejects_human_source_without_external_provenance(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")

    with pytest.raises(ValueError, match="requires external decision provenance"):
        WorkflowEngine(tmp_path / "workflow.sqlite").signal(
            "wf_approval",
            "approval.decision",
            key="approve_test_plan",
            payload={"action": "approve", "by": "skylar"},
            source={"kind": "human", "id": "skylar", "channel": "current-chat"},
            idempotency_key="thin-source-approval",
        )

    assert WorkflowEngine(tmp_path / "workflow.sqlite").workflow_status("wf_approval")["status"] == "waiting"


def test_approval_accepts_provenance_without_matching_actor_identity(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")

    recorded = WorkflowEngine(tmp_path / "workflow.sqlite").signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "not-skylar", "channel": "discord", "message_url": "discord://thread/123/message/456"},
        idempotency_key="wrong-human-approval",
    )

    assert recorded.status == "running"
    assert WorkflowEngine(tmp_path / "workflow.sqlite").drain("wf_approval").status == "completed"


def test_approval_decision_cannot_arrive_before_approval_request(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.start(approval_after_step_workflow, {"plan": "needs prep"}, workflow_id="wf_approval")
    assert first.status == "running"
    first = engine.worker_once("wf_approval", worker_id="worker-a")
    assert first.status == "waiting"
    assert first.waiting_on == "step:prepare_approval_artifact:0"

    with pytest.raises(ValueError, match="has no matching ApprovalRequested event"):
        engine.signal(
            "wf_approval",
            "approval.decision",
            key="approve_test_plan",
            payload={"action": "approve", "by": "skylar"},
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/123/message/456"},
            idempotency_key="early-human-approval",
        )

    after_step_recorded = engine.complete_step("wf_approval", "step:prepare_approval_artifact:0", {"plan": "needs prep"})
    assert after_step_recorded.status == "running"
    after_step = engine.drain("wf_approval")
    assert after_step.status == "waiting"
    assert after_step.waiting_on == "signal:approval.decision:approve_test_plan"
    status = engine.workflow_status("wf_approval", recent_events=10)
    assert [event["type"] for event in status["events"]].count("ApprovalRequested") == 1
    assert not [event for event in status["events"] if event["type"] == "SignalReceived"]

def test_completed_workflow_rejects_conflicting_late_approval_signal(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")
    recorded = engine.signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload=approve_payload(),
        source=human_source("456"),
        idempotency_key="human-approval",
    )
    assert recorded.status == "running"
    approved = engine.drain("wf_approval")
    assert approved.status == "completed"
    before = engine.workflow_status("wf_approval", recent_events=20)
    before_event_count = before["event_count"]

    with pytest.raises(ValueError, match="already has a recorded decision"):
        engine.signal(
            "wf_approval",
            "approval.decision",
            key="approve_test_plan",
            payload=reject_payload(),
            source=human_source("789"),
            idempotency_key="late-reject",
        )

    after = engine.workflow_status("wf_approval", recent_events=20)
    assert after["event_count"] == before_event_count
    assert after["result"]["decision"]["action"] == "approve"


def test_second_approval_decision_for_waiting_workflow_is_rejected(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.run_until_idle(approval_then_wait_workflow, {}, workflow_id="wf_approval")
    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_test_plan"

    recorded = engine.signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload=approve_payload(),
        source=human_source("456"),
        idempotency_key="human-approval",
    )
    assert recorded.status == "running"
    after_approval = engine.drain("wf_approval")
    assert after_approval.status == "waiting"
    assert after_approval.waiting_on == "signal:followup.ready:continue"

    duplicate = engine.signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload=approve_payload(),
        source=human_source("456"),
        idempotency_key="human-approval",
    )
    assert duplicate.status == "waiting"
    assert duplicate.waiting_on == "signal:followup.ready:continue"

    with pytest.raises(ValueError, match="already has a recorded decision"):
        engine.signal(
            "wf_approval",
            "approval.decision",
            key="approve_test_plan",
            payload=reject_payload(),
            source=human_source("789"),
            idempotency_key="second-decision",
        )

    status = engine.workflow_status("wf_approval", recent_events=20)
    decisions = [
        event
        for event in status["events"]
        if event["type"] == "SignalReceived" and event["key"] == "signal:approval.decision:approve_test_plan"
    ]
    assert len(decisions) == 1


def test_completed_workflow_ignores_late_step_completion(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    result = engine.run_until_idle(immediate_workflow, {}, workflow_id="wf_done")
    assert result.status == "completed"
    before = engine.workflow_status("wf_done", recent_events=20)

    late = engine.complete_step("wf_done", "step:never_requested:0", {"bad": True})

    assert late.status == "completed"
    assert late.result == {"ok": True}
    after = engine.workflow_status("wf_done", recent_events=20)
    assert after["event_count"] == before["event_count"]
    assert after["result"] == {"ok": True}

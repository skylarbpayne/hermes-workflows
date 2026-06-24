import pytest

from hermes_workflows import WorkflowEngine, approve, step, workflow
from hermes_workflows.approvals import ApprovalDecisionInput, ApprovalReceipt, ApprovalView
from hermes_workflows.engine import _WORKFLOW_REGISTRY


@step
async def adapter_followup_step(inputs):
    return {"followup": inputs.get("followup", "done")}


@workflow
async def adapter_approval_workflow(inputs):
    decision = await approve(
        "Approve adapter test?",
        key="approve_adapter_test",
        artifact={"plan": inputs.get("plan", "adapter"), "count": 2},
        allowed=["approve", "reject"],
        timeout="24h",
    )
    return {"decision": decision}


@workflow
async def adapter_approval_then_step_workflow(inputs):
    decision = await approve(
        "Approve before follow-up step?",
        key="approve_adapter_test",
        artifact={"plan": inputs.get("plan", "adapter")},
    )
    followup = await adapter_followup_step(inputs)
    return {"decision": decision, "followup": followup}


def human_source(message_id="msg-1"):
    return {
        "kind": "human",
        "id": "skylar",
        "channel": "discord",
        "message_id": message_id,
    }


def decision_input(*, action="approve", message_id="msg-1", idempotency_key="approval-1"):
    return ApprovalDecisionInput(
        workflow_id="wf_adapter",
        key="approve_adapter_test",
        action=action,
        by="skylar",
        source=human_source(message_id),
        note="looks safe",
        idempotency_key=idempotency_key,
    )


def test_list_pending_approvals_returns_allowed_artifact_and_workflow_ref(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    result = engine.run_until_idle(
        adapter_approval_workflow,
        {"plan": "adapter api"},
        workflow_id="wf_adapter",
        workflow_ref="tests.test_approval_adapter_api:adapter_approval_workflow",
    )
    assert result.status == "waiting"

    approvals = WorkflowEngine(db, read_only=True).list_approvals(status="waiting")

    assert len(approvals) == 1
    approval = approvals[0]
    assert isinstance(approval, ApprovalView)
    assert approval.db_path == str(db)
    assert approval.workflow_id == "wf_adapter"
    assert approval.workflow_name == "adapter_approval_workflow"
    assert approval.workflow_ref == "tests.test_approval_adapter_api:adapter_approval_workflow"
    assert approval.key == "approve_adapter_test"
    assert approval.status == "waiting"
    assert approval.prompt == "Approve adapter test?"
    assert approval.artifact == {"plan": "adapter api", "count": 2}
    assert approval.allowed == ["approve", "reject"]
    assert approval.timeout == "24h"
    assert approval.waiting_on == "signal:approval.decision:approve_adapter_test"
    assert approval.requested_seq is not None
    assert approval.decision is None
    assert approval.source is None
    assert approval.diagnostics


def test_get_approval_returns_one_view_without_dashboard_renderer(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(adapter_approval_workflow, {}, workflow_id="wf_adapter")

    approval = WorkflowEngine(db, read_only=True).get_approval("wf_adapter", "approve_adapter_test")

    assert isinstance(approval, ApprovalView)
    assert approval.key == "approve_adapter_test"
    assert approval.status == "waiting"


def test_submit_approval_decision_validates_external_provenance(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(adapter_approval_workflow, {}, workflow_id="wf_adapter")

    with pytest.raises(ValueError, match="requires external decision provenance"):
        engine.submit_approval_decision(
            ApprovalDecisionInput(
                workflow_id="wf_adapter",
                key="approve_adapter_test",
                action="approve",
                by="skylar",
                source={"channel": "test"},
            )
        )

    assert engine.workflow_status("wf_adapter")["status"] == "waiting"


def test_submit_approval_decision_resume_true_returns_receipt_and_completes_workflow(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(adapter_approval_workflow, {}, workflow_id="wf_adapter")

    receipt = engine.submit_approval_decision(decision_input(), resume=True)

    assert isinstance(receipt, ApprovalReceipt)
    assert receipt.workflow_id == "wf_adapter"
    assert receipt.key == "approve_adapter_test"
    assert receipt.action == "approve"
    assert receipt.by == "skylar"
    assert receipt.source == {"channel": "discord", "message_id": "msg-1"}
    assert receipt.status == "running"
    assert receipt.waiting_on == "signal:approval.decision:approve_adapter_test"
    assert receipt.result_summary is None

    completed = engine.drain("wf_adapter")
    assert completed.status == "completed"
    decision = completed.result["decision"]
    assert decision.action == "approve"
    assert decision.by == "skylar"
    assert decision.note == "looks safe"
    assert decision.source == {"channel": "discord", "message_id": "msg-1"}


def test_submit_approval_decision_resume_false_records_without_running_next_step(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(adapter_approval_then_step_workflow, {}, workflow_id="wf_adapter")

    receipt = engine.submit_approval_decision(decision_input(), resume=False)

    assert receipt.status == "decision_recorded"
    status = engine.workflow_status("wf_adapter", recent_events=20)
    assert status["status"] == "running"
    assert status["waiting_on"] == "signal:approval.decision:approve_adapter_test"
    assert [event for event in status["events"] if event["type"] == "SignalReceived"]
    assert not [command for command in status["pending_commands"] if command["type"] == "run_step"]

    resumed = engine.drain("wf_adapter")
    assert resumed.status == "completed"
    assert resumed.result["followup"] == {"followup": "done"}


def test_submit_approval_decision_resume_false_is_idempotent_for_same_event(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(adapter_approval_then_step_workflow, {}, workflow_id="wf_adapter")

    first = engine.submit_approval_decision(decision_input(), resume=False)
    second = engine.submit_approval_decision(decision_input(), resume=False)

    assert first.status == "decision_recorded"
    assert second.status == "decision_recorded"
    events = engine.events("wf_adapter")
    approval_signals = [event for event in events if event["type"] == "SignalReceived" and event["key"] == "signal:approval.decision:approve_adapter_test"]
    assert len(approval_signals) == 1


def test_workflow_ref_is_exposed_in_status_and_listed_approvals(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(
        adapter_approval_workflow,
        {},
        workflow_id="wf_adapter",
        workflow_ref="tests.test_approval_adapter_api:adapter_approval_workflow",
    )

    status = engine.workflow_status("wf_adapter")
    approval = engine.get_approval("wf_adapter", "approve_adapter_test")

    assert status["workflow_ref"] == "tests.test_approval_adapter_api:adapter_approval_workflow"
    assert approval.workflow_ref == status["workflow_ref"]


def test_submit_approval_decision_can_resume_by_stored_workflow_ref(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(
        adapter_approval_workflow,
        {},
        workflow_id="wf_adapter",
        workflow_ref="tests.test_approval_adapter_api:adapter_approval_workflow",
    )
    removed = _WORKFLOW_REGISTRY.pop("adapter_approval_workflow", None)
    try:
        receipt = WorkflowEngine(db).submit_approval_decision(decision_input(), resume=True)
        completed = WorkflowEngine(db).drain("wf_adapter")
    finally:
        if removed is not None:
            _WORKFLOW_REGISTRY["adapter_approval_workflow"] = removed

    assert receipt.status == "running"
    assert completed.status == "completed"
    assert completed.result is not None
    assert completed.result["decision"]["source"] == {"channel": "discord", "message_id": "msg-1"}

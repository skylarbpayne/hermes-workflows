import pytest

from hermes_workflows import WorkflowEngine, step, workflow
from hermes_workflows.approvals import ApprovalDecisionInput, ApprovalReceipt, ApprovalView
from hermes_workflows.engine import _WORKFLOW_REGISTRY


@step
async def adapter_followup_step(ctx, inputs):
    return {"followup": inputs.get("followup", "done")}


@workflow
async def adapter_approval_workflow(ctx, inputs):
    decision = await ctx.approval.request(
        "Approve adapter test?",
        key="approve_adapter_test",
        artifact={"plan": inputs.get("plan", "adapter"), "count": 2},
        approver="human:skylar",
        allowed=["approve", "reject"],
        authority=["adapter:test"],
        timeout="24h",
    )
    return {"decision": decision}


@workflow
async def adapter_approval_then_step_workflow(ctx, inputs):
    decision = await ctx.approval.request(
        "Approve before follow-up step?",
        key="approve_adapter_test",
        artifact={"plan": inputs.get("plan", "adapter")},
        approver="human:skylar",
    )
    followup = await adapter_followup_step(ctx, inputs)
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


def test_list_pending_approvals_returns_allowed_authority_artifact_and_workflow_ref(tmp_path):
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
    assert approval.approver == "human:skylar"
    assert approval.allowed == ["approve", "reject"]
    assert approval.authority == ["adapter:test"]
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


def test_submit_approval_decision_validates_human_source(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(adapter_approval_workflow, {}, workflow_id="wf_adapter")

    with pytest.raises(ValueError, match="requires human approval source"):
        engine.submit_approval_decision(
            ApprovalDecisionInput(
                workflow_id="wf_adapter",
                key="approve_adapter_test",
                action="approve",
                by="skylar",
                source={"kind": "agent", "id": "palmer", "channel": "test", "message_id": "m"},
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
    assert receipt.source == human_source()
    assert receipt.status == "completed"
    assert receipt.waiting_on is None
    assert receipt.result_summary == {"decision": {"action": "approve", "by": "skylar", "note": "[REDACTED]", "source": human_source()}}


def test_submit_approval_decision_resume_false_records_without_running_next_step(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(adapter_approval_then_step_workflow, {}, workflow_id="wf_adapter")

    receipt = engine.submit_approval_decision(decision_input(), resume=False)

    assert receipt.status == "decision_recorded"
    status = engine.workflow_status("wf_adapter", recent_events=20)
    assert status["status"] == "waiting"
    assert status["waiting_on"] == "signal:approval.decision:approve_adapter_test"
    assert [event for event in status["events"] if event["type"] == "SignalReceived"]
    assert not [command for command in status["pending_commands"] if command["type"] == "run_step"]

    resumed = engine.resume(adapter_approval_then_step_workflow, "wf_adapter")
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
    finally:
        if removed is not None:
            _WORKFLOW_REGISTRY["adapter_approval_workflow"] = removed

    assert receipt.status == "completed"
    assert receipt.result_summary is not None
    assert receipt.result_summary["decision"]["source"] == human_source()

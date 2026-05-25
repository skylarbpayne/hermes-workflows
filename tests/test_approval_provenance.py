from hermes_workflows import WorkflowEngine, workflow


@workflow
async def approval_workflow(ctx, inputs):
    decision = await ctx.approval.request(
        "Approve the test plan?",
        key="approve_test_plan",
        artifact={"plan": "test"},
        approver="human:skylar",
        allowed=["approve", "reject"],
        authority=["approve_plan"],
    )
    return {"decision": decision}


def test_human_approval_rejects_agent_originated_signal(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")
    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_test_plan"

    result = WorkflowEngine(tmp_path / "workflow.sqlite").signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload={"action": "approve", "by": "palmer"},
        source={"kind": "agent", "id": "palmer", "channel": "test"},
        idempotency_key="agent-approval",
    )

    assert result.status == "failed"
    assert "requires human approval source" in result.error


def test_human_approval_accepts_human_source_and_returns_provenance(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")
    assert first.status == "waiting"

    result = WorkflowEngine(tmp_path / "workflow.sqlite").signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload={"action": "approve", "by": "skylar"},
        source={
            "kind": "human",
            "id": "skylar",
            "channel": "discord",
            "message_url": "discord://thread/123/message/456",
        },
        idempotency_key="human-approval",
    )

    assert result.status == "completed"
    decision = result.result["decision"]
    assert decision["action"] == "approve"
    assert decision["by"] == "skylar"
    assert decision["source"] == {
        "kind": "human",
        "id": "skylar",
        "channel": "discord",
        "message_url": "discord://thread/123/message/456",
    }


def test_human_approval_rejects_missing_source(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")

    result = WorkflowEngine(tmp_path / "workflow.sqlite").signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload={"action": "approve", "by": "skylar-current-chat"},
        idempotency_key="missing-source-approval",
    )

    assert result.status == "failed"
    assert "requires human approval source" in result.error


def test_human_approval_rejects_human_source_without_external_provenance(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")

    result = WorkflowEngine(tmp_path / "workflow.sqlite").signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "current-chat"},
        idempotency_key="thin-source-approval",
    )

    assert result.status == "failed"
    assert "requires external approval provenance" in result.error


def test_human_approval_rejects_wrong_human_source(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(approval_workflow, {}, workflow_id="wf_approval")

    result = WorkflowEngine(tmp_path / "workflow.sqlite").signal(
        "wf_approval",
        "approval.decision",
        key="approve_test_plan",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "not-skylar", "channel": "discord", "message_url": "discord://thread/123/message/456"},
        idempotency_key="wrong-human-approval",
    )

    assert result.status == "failed"
    assert "requires approval from human:skylar" in result.error

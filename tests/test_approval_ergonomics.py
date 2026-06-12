import pytest

from hermes_workflows import ApprovalDecision, WorkflowEngine, step, workflow
from hermes_workflows.prompts import build_agent_step_payload


def human_source(message_id="msg-1"):
    return {
        "kind": "human",
        "id": "skylar",
        "channel": "discord",
        "message_id": message_id,
    }


@workflow
async def typed_auto_key_workflow(ctx, inputs):
    decision = await ctx.approve(
        "Approve plan artifact?",
        artifact={"plan": inputs.get("plan", "ship it")},
        approver="human:skylar",
    )
    return {
        "typed": isinstance(decision, ApprovalDecision),
        "approved": decision.approved,
        "by": decision.by,
        "legacy_action": decision["action"],
    }


@workflow
async def handoff_workflow(ctx, inputs):
    approved = await ctx.approve(
        "Approve implementation handoff?",
        key="approve_handoff_plan",
        artifact={"goal": inputs.get("goal", "demo")},
        approver="human:skylar",
    )
    if not approved.approved:
        return {"status": "not-approved", "feedback": approved.feedback}
    handoff = await ctx.handoff(
        "Implement approved plan",
        key="implementation_ready",
        artifact={"goal": inputs.get("goal", "demo")},
        assignee="agent:implementer",
        instructions="Make the source change, then signal handoff.completed.",
    )
    return {"status": "done", "handoff": handoff}


@step
async def revise_packet(ctx, feedback):
    return {"revision": feedback}


@workflow
async def feedback_loop_workflow(ctx, inputs):
    packet = {"draft": inputs.get("draft", "v1")}
    decisions = []
    for _ in range(3):
        decision = await ctx.approve(
            "Approve packet?",
            key="approve_packet",
            artifact=packet,
            approver="human:skylar",
            allowed=["approve", "reject", "edit"],
            feedback_loop=True,
        )
        decisions.append({"action": decision.action, "feedback": decision.feedback})
        if decision.approved:
            return {"status": "approved", "decisions": decisions, "packet": packet}
        if not decision.needs_revision:
            return {"status": "stopped", "decisions": decisions}
        packet = await revise_packet(ctx, decision.feedback or "revise")
    return {"status": "too-many-revisions", "decisions": decisions}


@workflow
async def human_only_guard_workflow(ctx, inputs):
    decision = await ctx.approve(
        "Approve guarded action?",
        key="approve_guarded_action",
        approver="human:skylar",
    )
    return {"approved": decision.approved}


def test_ctx_approve_returns_typed_decision_and_derives_key(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(typed_auto_key_workflow, {"plan": "typed"}, workflow_id="wf_typed")

    assert first.status == "waiting"
    assert first.waiting_on.startswith("signal:approval.decision:approve_plan_artifact")
    key = first.waiting_on.rsplit(":", 1)[1]
    first_status = engine.workflow_status("wf_typed")
    assert first_status["steps"] == [
        {
            "id": key,
            "key": key,
            "status": "waiting",
            "first_seq": first_status["steps"][0]["first_seq"],
            "last_seq": first_status["steps"][0]["last_seq"],
            "label": "Approve plan artifact?",
            "completion_mode": "approval",
            "step_type": "approval",
            "requested_seq": first_status["steps"][0]["requested_seq"],
        }
    ]
    approval = engine.get_approval("wf_typed", key)
    assert approval.key == key
    assert approval.artifact == {"plan": "typed"}

    result = engine.signal(
        "wf_typed",
        "approval.decision",
        key=key,
        payload={"action": "approve", "by": "skylar"},
        source=human_source(),
        idempotency_key="approve-auto-key",
    )

    assert result.status == "completed"
    assert result.result == {"typed": True, "approved": True, "by": "skylar", "legacy_action": "approve"}
    completed_step = engine.workflow_status("wf_typed")["steps"][0]
    assert completed_step["id"] == key
    assert completed_step["status"] == "completed"
    assert completed_step["completion_mode"] == "approval"
    assert completed_step["output"] == {"action": "approve", "by": "skylar"}
    assert completed_step["source"]["kind"] == "human"


def test_ctx_handoff_records_external_work_and_resumes_on_completion_signal(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(handoff_workflow, {"goal": "ergonomics"}, workflow_id="wf_handoff")
    assert first.waiting_on == "signal:approval.decision:approve_handoff_plan"

    after_approval = engine.signal(
        "wf_handoff",
        "approval.decision",
        key="approve_handoff_plan",
        payload={"action": "approve", "by": "skylar"},
        source=human_source(),
        idempotency_key="approve-handoff",
    )
    assert after_approval.status == "waiting"
    assert after_approval.waiting_on == "signal:handoff.completed:implementation_ready"
    after_approval_steps = {step["id"]: step for step in engine.workflow_status("wf_handoff")["steps"]}
    assert after_approval_steps["approve_handoff_plan"]["status"] == "completed"
    assert after_approval_steps["approve_handoff_plan"]["completion_mode"] == "approval"
    assert after_approval_steps["implementation_ready"]["status"] == "waiting"
    assert after_approval_steps["implementation_ready"]["completion_mode"] == "worker"
    pending = engine.pending_commands("wf_handoff")
    handoff_commands = [command for command in pending if command["type"] == "external_handoff"]
    assert len(handoff_commands) == 1
    assert handoff_commands[0]["key"] == "handoff:implementation_ready"
    assert handoff_commands[0]["payload"]["assignee"] == "agent:implementer"

    done = engine.signal(
        "wf_handoff",
        "handoff.completed",
        key="implementation_ready",
        payload={"summary": "source changed", "artifacts": ["diff.patch"]},
        idempotency_key="handoff-ready",
    )
    assert done.status == "completed"
    assert done.result["handoff"]["summary"] == "source changed"
    done_steps = {step["id"]: step for step in engine.workflow_status("wf_handoff")["steps"]}
    assert done_steps["implementation_ready"]["status"] == "completed"
    assert done_steps["implementation_ready"]["completion_mode"] == "worker"
    assert done_steps["implementation_ready"]["output"] == {"summary": "source changed", "artifacts": ["diff.patch"]}
    history = engine.workflow_status("wf_handoff", command_history="all")["command_history"]
    statuses = {command["key"]: command["status"] for command in history}
    assert statuses["handoff:implementation_ready"] == "completed"


def test_feedback_loop_uses_new_attempt_keys_after_revision_feedback(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(feedback_loop_workflow, {"draft": "v1"}, workflow_id="wf_loop")
    assert first.waiting_on == "signal:approval.decision:approve_packet"

    after_edit = engine.signal(
        "wf_loop",
        "approval.decision",
        key="approve_packet",
        payload={"action": "edit", "by": "skylar", "reason": "tighten scope"},
        source=human_source("edit-1"),
        idempotency_key="edit-1",
    )
    assert after_edit.status == "completed" or after_edit.status == "waiting"
    if after_edit.status == "completed":
        # local drain ran the revision step and decider may already be waiting on retry in fast environments
        after_edit = engine.resume(feedback_loop_workflow, "wf_loop")
    assert after_edit.status == "waiting"
    assert after_edit.waiting_on == "signal:approval.decision:approve_packet_retry_1"

    approved = engine.signal(
        "wf_loop",
        "approval.decision",
        key="approve_packet_retry_1",
        payload={"action": "approve", "by": "skylar"},
        source=human_source("approve-2"),
        idempotency_key="approve-2",
    )
    assert approved.status == "completed"
    assert approved.result["status"] == "approved"
    assert approved.result["decisions"] == [
        {"action": "edit", "feedback": "[REDACTED]"},
        {"action": "approve", "feedback": None},
    ]


def test_agent_step_variables_accept_typed_approval_decisions():
    payload = build_agent_step_payload(
        "draft_packet_agent",
        "prepare packet",
        dict,
        variables={
            "human_approval": ApprovalDecision(
                action="approve",
                by="skylar",
                source=human_source(),
                note="ship the packet",
            )
        },
        mock_output={"ok": True},
    )

    request = payload["args"][0]
    assert request["variables"]["human_approval"] == {
        "action": "approve",
        "by": "skylar",
        "note": "ship the packet",
        "source": human_source(),
    }
    assert request["variables_sha256"]


def test_human_gate_rejects_agent_authored_named_gate_approval(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(human_only_guard_workflow, {}, workflow_id="wf_guard")

    with pytest.raises(ValueError, match="requires human approval source"):
        engine.signal(
            "wf_guard",
            "approval.decision",
            key="approve_guarded_action",
            payload={"action": "approve", "by": "palmer"},
            source={"kind": "agent", "id": "palmer", "channel": "discord", "message_id": "broad-chat"},
            idempotency_key="agent-broad-chat",
        )

    status = engine.workflow_status("wf_guard")
    assert status["status"] == "waiting"
    assert status["waiting_on"] == "signal:approval.decision:approve_guarded_action"

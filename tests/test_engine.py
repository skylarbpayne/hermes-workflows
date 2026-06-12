from hermes_workflows import WorkflowEngine, workflow


def human_source(message_id="msg-1"):
    return {
        "kind": "human",
        "id": "skylar",
        "channel": "test",
        "message_id": message_id,
    }


@workflow
async def approval_then_worker_step_workflow(ctx, inputs):
    decision = await ctx.approve(
        "Approve plan?",
        key="approve_plan",
        artifact={"goal": inputs.get("goal", "demo")},
        approver="human:skylar",
    )
    if not decision.approved:
        return {"ready": False, "stage": "plan_rejected"}
    worker_output = await ctx.handoff(
        "Run worker step",
        key="run_worker_step",
        assignee="agent:worker",
        instructions="Complete this worker step and return output.",
    )
    return {"ready": True, "worker_output": worker_output}


def test_approval_and_worker_completion_are_operator_facing_steps(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.run_until_idle(
        approval_then_worker_step_workflow,
        {"goal": "step model"},
        workflow_id="wf_step_model",
    )

    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_plan"
    first_steps = {step["id"]: step for step in engine.workflow_status("wf_step_model")["steps"]}
    assert set(first_steps) == {"approve_plan"}
    assert first_steps["approve_plan"]["completion_mode"] == "approval"
    assert first_steps["approve_plan"]["status"] == "waiting"

    after_approval = engine.signal(
        "wf_step_model",
        "approval.decision",
        key="approve_plan",
        payload={"action": "approve", "by": "skylar"},
        source=human_source("approve-plan"),
        idempotency_key="approve-plan",
    )

    assert after_approval.status == "waiting"
    assert after_approval.waiting_on == "signal:handoff.completed:run_worker_step"
    after_approval_steps = {step["id"]: step for step in engine.workflow_status("wf_step_model")["steps"]}
    assert set(after_approval_steps) == {"approve_plan", "run_worker_step"}
    assert after_approval_steps["approve_plan"]["status"] == "completed"
    assert after_approval_steps["approve_plan"]["output"] == {"action": "approve", "by": "skylar"}
    assert after_approval_steps["approve_plan"]["source"]["kind"] == "human"
    assert after_approval_steps["run_worker_step"]["completion_mode"] == "worker"
    assert after_approval_steps["run_worker_step"]["status"] == "waiting"

    done = engine.signal(
        "wf_step_model",
        "handoff.completed",
        key="run_worker_step",
        payload={"summary": "worker completed the step", "artifact": "diff.patch"},
        source={"kind": "worker", "id": "worker-1"},
        idempotency_key="worker-step-complete",
    )

    assert done.status == "completed"
    completed_steps = {step["id"]: step for step in engine.workflow_status("wf_step_model")["steps"]}
    assert completed_steps["run_worker_step"]["status"] == "completed"
    assert completed_steps["run_worker_step"]["completion_mode"] == "worker"
    assert completed_steps["run_worker_step"]["output"] == {"summary": "worker completed the step", "artifact": "diff.patch"}
    assert not any(step_id.startswith(("approval:", "signal:", "handoff:", "wait:")) for step_id in completed_steps)


def test_internal_signal_and_wait_records_remain_events_not_public_steps(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(
        approval_then_worker_step_workflow,
        {"goal": "private plumbing"},
        workflow_id="wf_private_plumbing",
    )
    engine.signal(
        "wf_private_plumbing",
        "approval.decision",
        key="approve_plan",
        payload={"action": "approve", "by": "skylar"},
        source=human_source("approve-private"),
        idempotency_key="approve-private",
    )

    event_keys = {event["key"] for event in engine.events("wf_private_plumbing")}
    assert "wait:approval.decision:approve_plan" in event_keys
    assert "signal:approval.decision:approve_plan" in event_keys
    assert "handoff:run_worker_step" in event_keys

    step_ids = {step["id"] for step in engine.workflow_status("wf_private_plumbing")["steps"]}
    assert step_ids == {"approve_plan", "run_worker_step"}

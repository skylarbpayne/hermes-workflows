import sqlite3

from hermes_workflows import WorkflowEngine, agent, approve, workflow


def human_source(message_id="msg-1"):
    return {
        "kind": "human",
        "id": "skylar",
        "channel": "test",
        "message_id": message_id,
    }


@workflow
async def approval_then_agent_workflow(inputs):
    decision = await approve(
        "Approve plan?",
        key="approve_plan",
        artifact={"goal": inputs.get("goal", "demo")},
    )
    if not decision.approved:
        return {"ready": False, "stage": "plan_rejected"}
    agent_output = await agent(
        "run_agent_request",
        prompt="Run agent request",
        key="run_agent_request",
    )
    return {"ready": True, "agent_output": agent_output}


def test_approval_and_agent_completion_are_operator_facing_steps(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.run_until_idle(
        approval_then_agent_workflow,
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
    after_approval = engine.drain("wf_step_model", initial=after_approval)

    assert after_approval.status == "waiting"
    assert after_approval.waiting_on == "signal:agent.completed:run_agent_request"
    after_approval_steps = {step["id"]: step for step in engine.workflow_status("wf_step_model")["steps"]}
    assert set(after_approval_steps) == {"approve_plan", "run_agent_request"}
    assert after_approval_steps["approve_plan"]["status"] == "completed"
    assert after_approval_steps["approve_plan"]["output"] == {"action": "approve", "by": "skylar"}
    assert after_approval_steps["run_agent_request"]["completion_mode"] == "agent"
    assert after_approval_steps["run_agent_request"]["status"] == "waiting"

    done = engine.signal(
        "wf_step_model",
        "agent.completed",
        key="run_agent_request",
        payload={"summary": "agent completed the request", "artifact": "diff.patch"},
        source={"kind": "agent", "id": "agent-1"},
        idempotency_key="agent-request-complete",
    )
    done = engine.drain("wf_step_model", initial=done)

    assert done.status == "completed"
    completed_steps = {step["id"]: step for step in engine.workflow_status("wf_step_model")["steps"]}
    assert completed_steps["run_agent_request"]["status"] == "completed"
    assert completed_steps["run_agent_request"]["completion_mode"] == "agent"
    assert completed_steps["run_agent_request"]["output"] == {"summary": "agent completed the request", "artifact": "diff.patch"}
    assert not any(step_id.startswith(("approval:", "signal:", "agent:", "wait:")) for step_id in completed_steps)


def test_internal_signal_and_wait_records_remain_events_not_public_steps(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(
        approval_then_agent_workflow,
        {"goal": "private plumbing"},
        workflow_id="wf_private_plumbing",
    )
    signal_result = engine.signal(
        "wf_private_plumbing",
        "approval.decision",
        key="approve_plan",
        payload={"action": "approve", "by": "skylar"},
        source=human_source("approve-private"),
        idempotency_key="approve-private",
    )
    engine.drain("wf_private_plumbing", initial=signal_result)

    event_keys = {event["key"] for event in engine.events("wf_private_plumbing")}
    assert "wait:approval.decision:approve_plan" in event_keys
    assert "signal:approval.decision:approve_plan" in event_keys
    assert "agent:run_agent_request" in event_keys

    step_ids = {step["id"] for step in engine.workflow_status("wf_private_plumbing")["steps"]}
    assert step_ids == {"approve_plan", "run_agent_request"}


@workflow
async def duplicate_public_step_key_workflow(inputs):
    decision = await approve(
        "Approve overloaded key?",
        key="shared_step",
    )
    if not decision.approved:
        return {"ready": False}
    await agent(
        "shared_step_agent",
        prompt="Agent request reuses approval key",
        key="shared_step",
    )
    return {"ready": True}


def test_conflicting_public_step_keys_fail_before_topology_merge(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    first = engine.run_until_idle(duplicate_public_step_key_workflow, {}, workflow_id="wf_duplicate_step_key")
    assert first.status == "waiting"

    result = engine.signal(
        "wf_duplicate_step_key",
        "approval.decision",
        key="shared_step",
        payload={"action": "approve", "by": "skylar"},
        source=human_source("approve-duplicate-key"),
        idempotency_key="approve-duplicate-key",
    )
    result = engine.drain("wf_duplicate_step_key", initial=result)

    assert result.status == "failed"
    assert "public step key conflict" in str(result.error)
    steps = {step["id"]: step for step in engine.workflow_status("wf_duplicate_step_key")["steps"]}
    assert set(steps) == {"shared_step"}
    assert steps["shared_step"]["completion_mode"] == "approval"
    assert not any(step_id.startswith(("approval:", "signal:", "agent:", "wait:")) for step_id in steps)


def test_workflow_engine_connect_context_closes_sqlite_fd(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    with engine._connect() as con:
        con.execute("SELECT 1").fetchone()

    try:
        con.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        assert "closed" in str(exc)
    else:
        raise AssertionError("WorkflowEngine._connect() context manager left SQLite connection usable/open")

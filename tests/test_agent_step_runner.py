from __future__ import annotations

import sys

from hermes_workflows import AgentStep, Workflow, WorkflowEngine, workflow


GENERATED_SOURCE = '''
from hermes_workflows import step, workflow

@step
async def label_item(ctx, item):
    return {"id": item["id"], "label": item["label"].upper()}

@workflow
async def process_item(ctx, item):
    return {"processed": await label_item(ctx, item)}
'''


@workflow
async def live_json_agent_pipeline(ctx, inputs):
    return await AgentStep(
        "summarize_item",
        prompt="Summarize {{item}}",
        variables={"item": inputs["item"]},
    )(ctx)


@workflow
async def live_generated_workflow_pipeline(ctx, inputs):
    processor = await AgentStep(
        "build_processor",
        prompt="Write a Python workflow for {{kind}} items.",
        variables={"kind": inputs["kind"]},
        returns=Workflow,
    )(ctx)
    return await processor(ctx, inputs["item"], key=inputs["item"]["id"])


def test_agent_step_dispatches_to_live_runner_and_replays_stored_result(tmp_path):
    db = tmp_path / "workflow.sqlite"
    calls = []

    def runner(request):
        calls.append(request)
        return {
            "output": {"summary": request["variables"]["item"].upper()},
            "provenance": {"runner": "fake", "run_id": "run-1"},
        }

    engine = WorkflowEngine(db, agent_runner=runner)
    result = engine.run_until_idle(live_json_agent_pipeline, {"item": "alpha"}, workflow_id="wf_live_json")

    assert result.status == "completed"
    assert result.result == {"summary": "ALPHA"}
    assert len(calls) == 1
    assert calls[0]["kind"] == "agent_step.runner_request.v1"
    assert calls[0]["name"] == "summarize_item"
    assert calls[0]["prompt"] == "Summarize {{item}}"
    assert calls[0]["variables"] == {"item": "alpha"}
    assert calls[0]["returns"] == "json"
    assert calls[0]["workflow_id"] == "wf_live_json"
    assert calls[0]["step_key"] == "step:agent_step:0"

    completed = [event for event in engine.events("wf_live_json") if event["type"] == "StepCompleted"][0]
    assert completed["payload"]["output"] == {"summary": "ALPHA"}
    assert completed["payload"]["metadata"]["kind"] == "agent_step.live_result.v1"
    assert completed["payload"]["metadata"]["provenance"] == {"runner": "fake", "run_id": "run-1"}
    assert completed["payload"]["metadata"]["request"]["step_key"] == "step:agent_step:0"

    replay_calls = []
    replay_engine = WorkflowEngine(db, agent_runner=lambda request: replay_calls.append(request) or {"output": "wrong"})
    replayed = replay_engine.run_until_idle(live_json_agent_pipeline, {"item": "alpha"}, workflow_id="wf_live_json")

    assert replayed.status == "completed"
    assert replayed.result == result.result
    assert replay_calls == []


def test_live_generated_workflow_requires_human_approval_before_child_runs(tmp_path):
    db = tmp_path / "workflow.sqlite"
    calls = []

    def runner(request):
        calls.append(request)
        return {
            "output": {"source": GENERATED_SOURCE, "symbol": "process_item"},
            "provenance": {"runner": "fake", "run_id": "run-workflow-1"},
        }

    engine = WorkflowEngine(db, agent_runner=runner)
    first = engine.run_until_idle(
        live_generated_workflow_pipeline,
        {"kind": "catalog", "item": {"id": "a", "label": "alpha"}},
        workflow_id="wf_live_workflow",
    )

    assert first.status == "waiting"
    approvals = engine.workflow_status("wf_live_workflow")["approvals"]
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["status"] == "waiting"
    assert approval["key"].startswith("generated-workflow:")
    assert approval["artifact"]["symbol"] == "process_item"
    assert approval["artifact"]["runner_provenance"] == {"runner": "fake", "run_id": "run-workflow-1"}
    assert first.waiting_on == f"signal:approval.decision:{approval['key']}"
    assert [event for event in engine.events("wf_live_workflow") if event["type"] == "ChildWorkflowRequested"] == []
    assert f"hermes_generated_workflows.{approval['artifact']['source_sha256']}" not in sys.modules
    workflow_value = [event for event in engine.events("wf_live_workflow") if event["type"] == "StepCompleted"][0][
        "payload"
    ]["output"]
    try:
        workflow_value.with_base_dir(db.parent).load()
    except ValueError as exc:
        assert "requires human approval" in str(exc)
    else:
        raise AssertionError("approval-required Workflow.load() should fail before approval")

    approved = engine.signal(
        "wf_live_workflow",
        "approval.decision",
        key=approval["key"],
        payload={"action": "approve", "by": "skylar", "message": "approved in test"},
        source={"kind": "human", "id": "skylar", "channel": "test", "event_id": "evt-1"},
    )

    assert approved.status == "completed"
    assert approved.result == {"processed": {"id": "a", "label": "ALPHA"}}
    assert len(calls) == 1
    child_requests = [event for event in engine.events("wf_live_workflow") if event["type"] == "ChildWorkflowRequested"]
    assert len(child_requests) == 1
    assert child_requests[0]["payload"]["source_sha256"] == approval["artifact"]["source_sha256"]

    status_after = engine.workflow_status("wf_live_workflow")
    assert status_after["approvals"][0]["status"] == "approve"
    assert status_after["approvals"][0]["source"]["event_id"] == "evt-1"


def test_live_runner_non_json_response_fails_without_orphaning_step(tmp_path):
    db = tmp_path / "workflow.sqlite"

    def runner(request):
        return {"output": {"ok": True}, "bad_vendor_object": object()}

    engine = WorkflowEngine(db, agent_runner=runner)
    result = engine.run_until_idle(live_json_agent_pipeline, {"item": "alpha"}, workflow_id="wf_bad_response")

    assert result.status == "failed"
    assert "TypeError" in (result.error or "")
    events = engine.events("wf_bad_response")
    assert [event for event in events if event["type"] == "StepCompleted"] == []
    failures = [event for event in events if event["type"] == "StepFailed"]
    assert len(failures) == 1
    assert failures[0]["payload"]["error"]["type"] == "TypeError"
    assert engine.workflow_status("wf_bad_response")["pending_commands"] == []


def test_live_agent_step_supports_async_runner(tmp_path):
    db = tmp_path / "workflow.sqlite"

    async def runner(request):
        return {
            "output": {"summary": request["variables"]["item"] + "!"},
            "provenance": {"runner": "async-fake"},
        }

    engine = WorkflowEngine(db, agent_runner=runner)
    result = engine.run_until_idle(live_json_agent_pipeline, {"item": "alpha"}, workflow_id="wf_async_runner")

    assert result.status == "completed"
    assert result.result == {"summary": "alpha!"}
    completed = [event for event in engine.events("wf_async_runner") if event["type"] == "StepCompleted"][0]
    assert completed["payload"]["metadata"]["provenance"] == {"runner": "async-fake"}

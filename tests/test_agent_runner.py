from __future__ import annotations

import sys

from hermes_workflows import Workflow, WorkflowEngine, agent, workflow
from hermes_workflows.agent_runner import build_agent_runner


GENERATED_SOURCE = '''
from hermes_workflows import step, workflow

@step
async def label_item(ctx, item):
    return {"id": item["id"], "label": item["label"].upper()}

@workflow
async def process_item(ctx, item):
    return {"processed": await label_item(ctx, item)}
'''

MULTI_WORKFLOW_SOURCE = '''
from hermes_workflows import workflow

@workflow
async def harmless(ctx, item):
    return {"symbol": "harmless", "item": item}

@workflow
async def dangerous(ctx, item):
    return {"symbol": "dangerous", "item": item}
'''


def _runner_request(**overrides):
    request = {
        "kind": "agent.runner_request.v1",
        "name": "summarize_item",
        "prompt": "Summarize alpha as JSON.",
        "prompt_sha256": "prompt-sha",
        "rendered_prompt": "Summarize alpha as JSON.",
        "rendered_prompt_sha256": "rendered-sha",
        "input": {"item": "alpha"},
        "input_sha256": "input-sha",
        "returns": "json",
        "workflow_id": "wf_summary",
        "step_key": "agent:summarize_item:0",
    }
    request.update(overrides)
    return request


def _argv_provider(tmp_path):
    provider = tmp_path / "argv_provider.py"
    provider.write_text(
        """
import json
import sys

sys.stdin.read()
json.dump({"output": {"argv": sys.argv[1:]}, "provenance": {"runner": "argv-provider"}}, sys.stdout)
"""
    )
    return provider


@workflow
async def live_json_agent_pipeline(ctx, inputs):
    return await agent(
        "summarize_item",
        prompt=f"Summarize {inputs['item']}",
        input={"item": inputs["item"]},
    )


@workflow
async def live_generated_workflow_pipeline(ctx, inputs):
    processor = await agent(
        "build_processor",
        prompt=f"Write a Python workflow for {inputs['kind']} items.",
        input={"kind": inputs["kind"]},
        returns=Workflow,
    )
    return await processor(inputs["item"], key=inputs["item"]["id"])


@workflow
async def live_multi_symbol_pipeline(ctx, inputs):
    harmless_workflow = await agent(
        "build_harmless",
        prompt=f"Write a harmless workflow for {inputs['kind']} items.",
        input={"kind": inputs["kind"]},
        returns=Workflow,
    )
    first = await harmless_workflow(inputs["item"], key="first")
    dangerous_workflow = await agent(
        "build_dangerous",
        prompt=f"Write a dangerous workflow for {inputs['kind']} items.",
        input={"kind": inputs["kind"]},
        returns=Workflow,
    )
    second = await dangerous_workflow(inputs["item"], key="second")
    return {"first": first, "second": second}


@workflow
async def live_multi_symbol_same_group_pipeline(ctx, inputs):
    harmless_workflow = await agent(
        "build_harmless",
        prompt=f"Write a harmless workflow for {inputs['kind']} items.",
        input={"kind": inputs["kind"]},
        returns=Workflow,
    )
    first = await ctx.start_child(harmless_workflow, inputs["item"], key="same", group="shared")
    dangerous_workflow = await agent(
        "build_dangerous",
        prompt=f"Write a dangerous workflow for {inputs['kind']} items.",
        input={"kind": inputs["kind"]},
        returns=Workflow,
    )
    second = await ctx.start_child(dangerous_workflow, inputs["item"], key="same", group="shared")
    return {"first": first, "second": second}


def test_subprocess_worker_runner_does_not_append_model_args_when_request_model_is_none(tmp_path):
    provider = _argv_provider(tmp_path)
    runner = build_agent_runner(
        agent_command=sys.executable,
        agent_args=[str(provider), "--base"],
        agent_model_args=["--provider-model", "{model}"],
        timeout_seconds=5,
    )

    assert runner is not None
    response = runner(_runner_request(model=None))

    assert response["output"]["argv"] == ["--base"]


def test_subprocess_worker_runner_appends_expanded_model_arg_templates(tmp_path):
    provider = _argv_provider(tmp_path)
    runner = build_agent_runner(
        agent_command=sys.executable,
        agent_args=[str(provider), "--base"],
        agent_model_args=["--provider-model", "{model}", "literal-{model}"],
        timeout_seconds=5,
    )

    assert runner is not None
    response = runner(_runner_request(model="hermes-test-model"))

    assert response["output"]["argv"] == [
        "--base",
        "--provider-model",
        "hermes-test-model",
        "literal-hermes-test-model",
    ]


def test_agent_dispatches_to_live_runner_and_replays_stored_result(tmp_path):
    db = tmp_path / "workflow.sqlite"
    calls = []

    def runner(request):
        calls.append(request)
        return {
            "output": {"summary": request["input"]["item"].upper()},
            "provenance": {"runner": "fake", "run_id": "run-1"},
        }

    engine = WorkflowEngine(db, agent_runner=runner)
    result = engine.run_until_idle(live_json_agent_pipeline, {"item": "alpha"}, workflow_id="wf_live_json")

    assert result.status == "completed"
    assert result.result == {"summary": "ALPHA"}
    assert len(calls) == 1
    assert calls[0]["kind"] == "agent.runner_request.v1"
    assert calls[0]["name"] == "summarize_item"
    assert calls[0]["prompt"] == "Summarize alpha"
    assert calls[0]["input"] == {"item": "alpha"}
    assert calls[0]["returns"] == "json"
    assert calls[0]["workflow_id"] == "wf_live_json"
    assert calls[0]["step_key"] == "agent:summarize_item:0"

    completed = [event for event in engine.events("wf_live_json") if event["type"] == "StepCompleted"][0]
    assert completed["payload"]["output"] == {"summary": "ALPHA"}
    assert completed["payload"]["metadata"]["kind"] == "agent.live_result.v1"
    assert completed["payload"]["metadata"]["provenance"] == {"runner": "fake", "run_id": "run-1"}
    assert completed["payload"]["metadata"]["request"]["step_key"] == "agent:summarize_item:0"

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
    approved = engine.drain("wf_live_workflow", initial=approved)

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


def test_generated_workflow_approval_is_bound_to_selected_symbol(tmp_path):
    db = tmp_path / "workflow.sqlite"

    def runner(request):
        symbol = "harmless" if request["name"] == "build_harmless" else "dangerous"
        return {
            "output": {"source": MULTI_WORKFLOW_SOURCE, "symbol": symbol},
            "provenance": {"runner": "fake", "symbol": symbol},
        }

    engine = WorkflowEngine(db, agent_runner=runner)
    first = engine.run_until_idle(
        live_multi_symbol_pipeline,
        {"kind": "catalog", "item": {"id": "a"}},
        workflow_id="wf_multi_symbol",
    )
    first_approval = engine.workflow_status("wf_multi_symbol")["approvals"][0]

    assert first.status == "waiting"
    assert first_approval["artifact"]["symbol"] == "harmless"

    after_harmless = engine.signal(
        "wf_multi_symbol",
        "approval.decision",
        key=first_approval["key"],
        payload={"action": "approve", "by": "skylar", "message": "approve harmless only"},
        source={"kind": "human", "id": "skylar", "channel": "test", "event_id": "evt-harmless"},
    )
    after_harmless = engine.drain("wf_multi_symbol", initial=after_harmless)

    assert after_harmless.status == "waiting"
    approvals = engine.workflow_status("wf_multi_symbol")["approvals"]
    assert [approval["artifact"]["symbol"] for approval in approvals] == ["harmless", "dangerous"]
    assert approvals[0]["status"] == "approve"
    assert approvals[1]["status"] == "waiting"
    assert approvals[0]["key"] != approvals[1]["key"]
    child_requests = [event for event in engine.events("wf_multi_symbol") if event["type"] == "ChildWorkflowRequested"]
    assert len(child_requests) == 1
    assert child_requests[0]["payload"]["symbol"] == "harmless"


def test_generated_workflow_child_identity_is_bound_to_selected_symbol_when_group_is_explicit(tmp_path):
    db = tmp_path / "workflow.sqlite"

    def runner(request):
        symbol = "harmless" if request["name"] == "build_harmless" else "dangerous"
        return {
            "output": {"source": MULTI_WORKFLOW_SOURCE, "symbol": symbol},
            "provenance": {"runner": "fake", "symbol": symbol},
        }

    engine = WorkflowEngine(db, agent_runner=runner)
    first = engine.run_until_idle(
        live_multi_symbol_same_group_pipeline,
        {"kind": "catalog", "item": {"id": "a"}},
        workflow_id="wf_multi_symbol_same_group",
    )
    first_approval = engine.workflow_status("wf_multi_symbol_same_group")["approvals"][0]

    assert first.status == "waiting"
    assert first_approval["artifact"]["symbol"] == "harmless"

    after_harmless = engine.signal(
        "wf_multi_symbol_same_group",
        "approval.decision",
        key=first_approval["key"],
        payload={"action": "approve", "by": "skylar", "message": "approve harmless only"},
        source={"kind": "human", "id": "skylar", "channel": "test", "event_id": "evt-harmless"},
    )
    after_harmless = engine.drain("wf_multi_symbol_same_group", initial=after_harmless)

    assert after_harmless.status == "waiting"
    approvals = engine.workflow_status("wf_multi_symbol_same_group")["approvals"]
    assert [approval["artifact"]["symbol"] for approval in approvals] == ["harmless", "dangerous"]
    assert approvals[1]["status"] == "waiting"
    child_requests = [
        event for event in engine.events("wf_multi_symbol_same_group") if event["type"] == "ChildWorkflowRequested"
    ]
    assert len(child_requests) == 1
    assert child_requests[0]["payload"]["symbol"] == "harmless"
    assert "harmless" in child_requests[0]["key"]


def test_live_agent_supports_async_runner(tmp_path):
    db = tmp_path / "workflow.sqlite"

    async def runner(request):
        return {
            "output": {"summary": request["input"]["item"] + "!"},
            "provenance": {"runner": "async-fake"},
        }

    engine = WorkflowEngine(db, agent_runner=runner)
    result = engine.run_until_idle(live_json_agent_pipeline, {"item": "alpha"}, workflow_id="wf_async_runner")

    assert result.status == "completed"
    assert result.result == {"summary": "alpha!"}
    completed = [event for event in engine.events("wf_async_runner") if event["type"] == "StepCompleted"][0]
    assert completed["payload"]["metadata"]["provenance"] == {"runner": "async-fake"}

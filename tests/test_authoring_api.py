from __future__ import annotations

from dataclasses import dataclass

import pytest

from hermes_workflows import WorkflowEngine, agent, approve_until, parallel, pipeline, workflow


@dataclass
class ResearchPacket:
    summary: str
    sources: list[str]


@dataclass
class DraftPacket:
    text: str


@dataclass
class ItemPacket:
    text: str


PROMPT_VERSION = "v1"
CONTEXT_VERSION = "ctx-v1"


@workflow
async def prompted_agent_workflow(inputs):
    research = await agent(
        "research",
        prompt=f"Research {inputs['topic']}",
        input={"topic": inputs["topic"]},
        context=[{"label": "brief", "content": "api rehaul"}],
        returns=ResearchPacket,
    )
    assert isinstance(research, ResearchPacket)
    return {"summary": research.summary, "sources": research.sources}


@workflow
async def memoized_context_workflow(inputs):
    research = await agent(
        "research",
        prompt=f"Research {inputs['topic']} with {PROMPT_VERSION}",
        input={"topic": inputs["topic"]},
        context=[{"label": "brief", "content": CONTEXT_VERSION}],
        returns=ResearchPacket,
    )
    await approve_until("approve_research", research, prompt="Approve research")
    return research.summary


@workflow
async def parallel_agent_workflow(inputs):
    drafts = await parallel(
        [
            agent(
                "draft_section",
                prompt=f"Draft section {item}",
                input={"item": item},
                key_by=item,
                returns=DraftPacket,
            )
            for item in inputs["items"]
        ],
        limit=2,
    )
    assert all(isinstance(draft, DraftPacket) for draft in drafts)
    return [draft.text for draft in drafts]


@workflow
async def pipeline_agent_workflow(inputs):
    sections = await pipeline(
        inputs["items"],
        lambda item: agent("upper", prompt=f"Uppercase {item}", input={"item": item}, key_by=item, returns=ItemPacket),
        lambda item: agent(
            "tag",
            prompt=f"Tag {item.text}",
            input={"text": item.text},
            key_by=item.text,
            returns=ItemPacket,
        ),
        limit=2,
    )
    assert all(isinstance(section, ItemPacket) for section in sections)
    return [section.text for section in sections]


def test_agent_requires_prompt():
    with pytest.raises(TypeError, match="prompt"):
        agent("research")


def test_agent_prompt_input_context_are_sent_to_runner_and_typed_output_replays(tmp_path):
    calls = []

    def runner(request):
        calls.append(request)
        return {"output": {"summary": request["input"]["topic"].upper(), "sources": ["docs"]}}

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db, agent_runner=runner)

    result = engine.run_until_idle(prompted_agent_workflow, {"topic": "typed workflows"}, workflow_id="wf_prompted_agent")

    assert result.status == "completed"
    assert result.result == {"summary": "TYPED WORKFLOWS", "sources": ["docs"]}
    assert len(calls) == 1
    assert calls[0]["name"] == "research"
    assert calls[0]["prompt"] == "Research typed workflows"
    assert calls[0]["input"] == {"topic": "typed workflows"}
    assert calls[0]["context"][0]["label"] == "brief"
    assert calls[0]["context_sha256"]
    assert calls[0]["fingerprint"]
    assert calls[0]["returns"].endswith(":ResearchPacket")

    replay_calls = []
    replay = WorkflowEngine(db, agent_runner=lambda request: replay_calls.append(request) or {"output": "wrong"})
    replayed = replay.run_until_idle(prompted_agent_workflow, {"topic": "typed workflows"}, workflow_id="wf_prompted_agent")

    assert replayed.status == "completed"
    assert replayed.result == result.result
    assert replay_calls == []


def test_agent_replay_fails_loudly_when_prompt_or_context_fingerprint_changes(tmp_path):
    global PROMPT_VERSION, CONTEXT_VERSION
    PROMPT_VERSION = "v1"
    CONTEXT_VERSION = "ctx-v1"
    db = tmp_path / "workflow.sqlite"
    calls = []

    def runner(request):
        calls.append(request)
        return {"output": {"summary": "memoized", "sources": [request["context_sha256"]]}}

    engine = WorkflowEngine(db, agent_runner=runner)
    first = engine.run_until_idle(memoized_context_workflow, {"topic": "memoization"}, workflow_id="wf_memoized")

    assert first.status == "waiting"
    assert len(calls) == 1
    approval = engine.workflow_status("wf_memoized")["approvals"][0]

    PROMPT_VERSION = "v2"
    CONTEXT_VERSION = "ctx-v2"
    approved = engine.signal(
        "wf_memoized",
        "approval.decision",
        key=approval["key"],
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "test", "event_id": "evt-approve"},
    )
    result = engine.drain("wf_memoized", initial=approved)

    assert result.status == "failed"
    assert "fingerprint changed" in (result.error or "")
    assert len(calls) == 1


def test_parallel_enqueues_all_agent_calls_before_waiting_and_replays_typed_results(tmp_path):
    db = tmp_path / "workflow.sqlite"
    calls = []

    def runner(request):
        calls.append(request)
        return {"output": {"text": f"draft:{request['input']['item']}"}}

    engine = WorkflowEngine(db, agent_runner=runner)
    engine.start(parallel_agent_workflow, {"items": ["a", "b"]}, workflow_id="wf_parallel")
    first = engine.worker_once("wf_parallel", worker_id="worker-start")

    assert first.status == "waiting"
    assert first.waiting_on == "parallel:0"
    assert calls == []
    assert [command["key"] for command in engine.pending_commands("wf_parallel") if command["type"] == "run_step"] == [
        "agent:draft_section:a",
        "agent:draft_section:b",
    ]

    result = engine.drain("wf_parallel", initial=first)

    assert result.status == "completed"
    assert result.result == ["draft:a", "draft:b"]
    assert [call["input"]["item"] for call in calls] == ["a", "b"]


def test_pipeline_runs_stages_over_items_with_typed_stage_outputs(tmp_path):
    db = tmp_path / "workflow.sqlite"

    def runner(request):
        if request["name"] == "upper":
            return {"output": {"text": request["input"]["item"].upper()}}
        if request["name"] == "tag":
            return {"output": {"text": f"tagged:{request['input']['text']}"}}
        raise AssertionError(request)

    engine = WorkflowEngine(db, agent_runner=runner)
    result = engine.run_until_idle(pipeline_agent_workflow, {"items": ["alpha", "beta"]}, workflow_id="wf_pipeline")

    assert result.status == "completed"
    assert result.result == ["tagged:ALPHA", "tagged:BETA"]
    status = engine.workflow_status("wf_pipeline")
    step_ids = [step["id"] for step in status["steps"]]
    assert "agent:upper:alpha" in step_ids
    assert "agent:tag:ALPHA" in step_ids

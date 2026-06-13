from __future__ import annotations

from dataclasses import dataclass

import pytest

from hermes_workflows import WorkflowEngine, agent, ask, parallel, pipeline, workflow


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


@dataclass
class ReviewDecision:
    action: str
    feedback: str | None = None


@dataclass
class AngleChoice:
    angle_id: str
    rationale: str


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
    await ask(
        "Approve research",
        key="approve_research",
        artifact=research,
        output=ReviewDecision,
    )
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


@workflow
async def ask_angle_workflow(inputs):
    choice = await ask(
        prompt="Which angle should we pursue?",
        key="choose_angle",
        artifact={"options": inputs["angles"]},
        output=AngleChoice,
    )
    assert isinstance(choice, AngleChoice)
    return {"angle_id": choice.angle_id, "rationale": choice.rationale}


@workflow
async def parallel_ask_workflow(inputs):
    reviews = await parallel(
        [
            ask(
                prompt=f"Review section {item}",
                key=f"review_{item}",
                artifact={"section": item},
                output=ReviewDecision,
            )
            for item in inputs["items"]
        ]
    )
    assert all(isinstance(review, ReviewDecision) for review in reviews)
    return [review.action for review in reviews]


@workflow
async def pipeline_with_ask_workflow(inputs):
    reviews = await pipeline(
        inputs["items"],
        lambda item: ask(
            prompt=f"Review section {item}",
            key=f"review_{item}",
            artifact={"section": item},
            output=ReviewDecision,
        ),
        limit=2,
    )
    assert all(isinstance(review, ReviewDecision) for review in reviews)
    return [review.feedback for review in reviews]


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
    operator_step = engine.workflow_status("wf_memoized")["operator_steps"][0]

    PROMPT_VERSION = "v2"
    CONTEXT_VERSION = "ctx-v2"
    responded = engine.signal(
        "wf_memoized",
        "operator.response",
        key=operator_step["key"],
        payload={"action": "approve", "feedback": "looks good"},
        source={"kind": "human", "id": "skylar", "channel": "test", "event_id": "evt-respond"},
    )
    result = engine.drain("wf_memoized", initial=responded)

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



def test_ask_collects_typed_human_input_without_requiring_approval_action(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        ask_angle_workflow,
        {"angles": ["inspectable", "resumable"]},
        workflow_id="wf_ask_angle",
    )

    assert first.status == "waiting"
    assert first.waiting_on == "signal:operator.response:choose_angle"
    status = engine.workflow_status("wf_ask_angle")
    assert status["approvals"] == []
    assert [step["key"] for step in status["operator_steps"]] == ["choose_angle"]
    step = status["steps"][0]
    assert step["key"] == "choose_angle"
    assert step["label"] == "Which angle should we pursue?"
    assert step["completion_mode"] == "operator"
    assert step["step_type"] == "operator"
    assert step["request"]["artifact"] == {"options": ["inspectable", "resumable"]}
    assert step["request"]["schema"].endswith(":AngleChoice")

    receipt = engine.submit_operator_response(
        workflow_id="wf_ask_angle",
        key="choose_angle",
        payload={"angle_id": "inspectable", "rationale": "clearest product claim"},
        source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "m-angle"},
    )
    assert receipt.status == "running"
    result = engine.drain("wf_ask_angle")

    assert result.status == "completed"
    assert result.result == {"angle_id": "inspectable", "rationale": "clearest product claim"}


def test_operator_response_can_be_recorded_without_inline_resume(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(
        ask_angle_workflow,
        {"angles": ["inspectable", "resumable"]},
        workflow_id="wf_ask_angle_no_resume",
    )

    assert first.status == "waiting"
    receipt = engine.submit_operator_response(
        workflow_id="wf_ask_angle_no_resume",
        key="choose_angle",
        payload={"angle_id": "resumable", "rationale": "proves decoupled response recording"},
        source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "m-angle-no-resume"},
        resume=False,
    )

    assert receipt.status == "response_recorded"
    recorded = engine.workflow_status("wf_ask_angle_no_resume")
    assert recorded["status"] == "running"
    assert recorded["operator_steps"][0]["status"] == "completed"
    assert recorded["operator_steps"][0]["output"]["angle_id"] == "resumable"

    result = engine.drain("wf_ask_angle_no_resume")
    assert result.status == "completed"
    assert result.result == {"angle_id": "resumable", "rationale": "proves decoupled response recording"}


def test_parallel_ask_emits_all_human_prompts_before_waiting(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(parallel_ask_workflow, {"items": ["one", "two"]}, workflow_id="wf_parallel_ask")

    assert first.status == "waiting"
    assert first.waiting_on == "parallel:0"
    operator_steps = [step for step in engine.workflow_status("wf_parallel_ask")["steps"] if step.get("step_type") == "operator"]
    assert [step["key"] for step in operator_steps] == ["review_one", "review_two"]
    assert [step["request"]["schema"] for step in operator_steps] == [
        f"{ReviewDecision.__module__}:ReviewDecision",
        f"{ReviewDecision.__module__}:ReviewDecision",
    ]

    one = engine.signal(
        "wf_parallel_ask",
        "operator.response",
        key="review_one",
        payload={"action": "approve", "feedback": "good"},
        source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "m-one"},
    )
    after_one = engine.drain("wf_parallel_ask", initial=one)
    assert after_one.status == "waiting"

    two = engine.signal(
        "wf_parallel_ask",
        "operator.response",
        key="review_two",
        payload={"action": "revise", "feedback": "tighten"},
        source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "m-two"},
    )
    result = engine.drain("wf_parallel_ask", initial=two)

    assert result.status == "completed"
    assert result.result == ["approve", "revise"]


def test_pipeline_ask_stage_fans_out_human_prompts(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(pipeline_with_ask_workflow, {"items": ["a", "b"]}, workflow_id="wf_pipeline_ask")

    assert first.status == "waiting"
    assert first.waiting_on == "parallel:0"
    operator_steps = [step for step in engine.workflow_status("wf_pipeline_ask")["steps"] if step.get("step_type") == "operator"]
    assert [step["key"] for step in operator_steps] == ["review_a", "review_b"]

    engine.signal(
        "wf_pipeline_ask",
        "operator.response",
        key="review_a",
        payload={"action": "approve", "feedback": "ship a"},
        source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "m-a"},
    )
    result = engine.drain(
        "wf_pipeline_ask",
        initial=engine.signal(
            "wf_pipeline_ask",
            "operator.response",
            key="review_b",
            payload={"action": "approve", "feedback": "ship b"},
            source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "m-b"},
        ),
    )

    assert result.status == "completed"
    assert result.result == ["ship a", "ship b"]

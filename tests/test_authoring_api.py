from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import pytest

from hermes_workflows import WorkflowEngine, agent, ask, current_step_context, goal, parallel, pipeline, prompt_file, step, workflow, workflow_id


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
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@dataclass
class PublishChoice:
    action: Literal["ship", "revise"]
    feedback: str | None = None


@dataclass
class DescribedPublishChoice:
    action: Annotated[Literal["ship", "revise"], "Publish decision to record"]
    expected_attendees: int = field(metadata={"description": "Expected attendee count used for venue planning"})
    feedback: Annotated[str | None, "Optional reviewer feedback"] = None


@dataclass
class AngleChoice:
    angle_id: str
    rationale: str


@dataclass
class TypedWorkflowInput:
    topic: str
    count: int = 1
    tags: list[str] = field(default_factory=list)


@dataclass
class TypedUnionWorkflowInput:
    count: int | None


@dataclass
class TypedContextWorkflowInput:
    topic: str
    enabled: bool = False


PROMPT_VERSION = "v1"


@workflow
async def typed_input_workflow(inputs: TypedWorkflowInput):
    assert isinstance(inputs, TypedWorkflowInput)
    return {"topic": inputs.topic, "count": inputs.count, "tags": inputs.tags}


@workflow
async def typed_union_input_workflow(inputs: TypedUnionWorkflowInput):
    assert isinstance(inputs, TypedUnionWorkflowInput)
    return {"count": inputs.count, "type": type(inputs.count).__name__}


@workflow
async def typed_context_input_workflow(inputs: TypedContextWorkflowInput):
    assert workflow_id()
    assert isinstance(inputs, TypedContextWorkflowInput)
    return {"topic": inputs.topic, "enabled": inputs.enabled}


@workflow
async def prompted_agent_workflow(inputs):
    research = await agent(
        "research",
        prompt=f"Research {inputs['topic']}",
        input={"topic": inputs["topic"]},
        returns=ResearchPacket,
    )
    assert isinstance(research, ResearchPacket)
    return {"summary": research.summary, "sources": research.sources}


@workflow
async def prompt_file_agent_workflow(inputs):
    rendered = prompt_file(inputs["prompt_path"]).render(topic=inputs["topic"])
    research = await agent(
        "research",
        prompt=rendered,
        input={"topic": inputs["topic"]},
        returns=ResearchPacket,
    )
    assert isinstance(research, ResearchPacket)
    return {"summary": research.summary, "sources": research.sources}


@workflow
async def workspace_agent_workflow(inputs):
    packet = await agent(
        "implement",
        prompt="Implement the approved plan.",
        input={"plan": inputs["plan"]},
        workspace_dir=inputs["workspace_dir"],
        isolation="worktree",
    )
    return packet


@step
async def capture_step_context_type():
    return type(current_step_context()).__name__


@workflow
async def current_step_context_workflow(inputs):
    return await capture_step_context_type()


@workflow
async def memoized_agent_workflow(inputs):
    research = await agent(
        "research",
        prompt=f"Research {inputs['topic']} with {PROMPT_VERSION}",
        input={"topic": inputs["topic"]},
        returns=ResearchPacket,
    )
    await ask(
        "Approve research",
        key="approve_research",
        input=research,
        returns=ReviewDecision,
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
        input={"options": inputs["angles"]},
        returns=AngleChoice,
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
                input={"section": item},
                returns=ReviewDecision,
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
            input={"section": item},
            returns=ReviewDecision,
        ),
        limit=2,
    )
    assert all(isinstance(review, ReviewDecision) for review in reviews)
    return [review.feedback for review in reviews]


def test_agent_requires_prompt():
    with pytest.raises(TypeError, match="prompt"):
        agent("research")


def test_ask_does_not_accept_legacy_artifact_or_output_names():
    with pytest.raises(TypeError, match="artifact"):
        ask("Review", key="review", artifact={"old": True})
    with pytest.raises(TypeError, match="output"):
        ask("Review", key="review", input={"new": True}, output=ReviewDecision)


def test_agent_and_ask_do_not_accept_context_keyword():
    legacy_kwargs: dict[str, Any] = {"context": {"old": True}}
    with pytest.raises(TypeError, match="context"):
        agent("research", prompt="Research", input={"topic": "x"}, **legacy_kwargs)
    with pytest.raises(TypeError, match="context"):
        ask("Review", key="review", input={"draft": "x"}, **legacy_kwargs)


def test_workflow_coerces_raw_json_to_typed_dataclass_input(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        typed_input_workflow,
        {"topic": "typed workflows", "count": "3", "tags": ["agent", "runtime"]},
        workflow_id="wf_typed_input",
    )

    assert result.status == "completed"
    assert result.result == {"topic": "typed workflows", "count": 3, "tags": ["agent", "runtime"]}


def test_workflow_coerces_pep604_union_fields(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        typed_union_input_workflow,
        {"count": "3"},
        workflow_id="wf_typed_union_input",
    )

    assert result.status == "completed"
    assert result.result == {"count": 3, "type": "int"}


def test_workflow_coerces_typed_input_for_legacy_ctx_signature(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        typed_context_input_workflow,
        {"topic": "context typed", "enabled": 1},
        workflow_id="wf_typed_context_input",
    )

    assert result.status == "completed"
    assert result.result == {"topic": "context typed", "enabled": True}


def test_workflow_input_parser_rejects_missing_required_fields(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(typed_input_workflow, {"count": 2}, workflow_id="wf_missing_typed_input")

    assert result.status == "failed"
    assert "missing required workflow input field: topic" in (result.error or "")
    status = engine.workflow_status("wf_missing_typed_input")
    assert status["status"] == "failed"
    assert "missing required workflow input field: topic" in (status["error"] or "")


def test_agent_prompt_input_are_sent_to_runner_and_typed_output_replays(tmp_path):
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
    assert "context" not in calls[0]
    assert "context_sha256" not in calls[0]
    assert calls[0]["fingerprint"]
    assert calls[0]["returns"].endswith(":ResearchPacket")

    replay_calls = []
    replay = WorkflowEngine(db, agent_runner=lambda request: replay_calls.append(request) or {"output": "wrong"})
    replayed = replay.run_until_idle(prompted_agent_workflow, {"topic": "typed workflows"}, workflow_id="wf_prompted_agent")

    assert replayed.status == "completed"
    assert replayed.result == result.result
    assert replay_calls == []


def test_agent_accepts_rendered_prompt_file_metadata(tmp_path):
    prompt_path = tmp_path / "research.md"
    prompt_path.write_text("Research {{ topic }} from durable context")
    calls = []

    def runner(request):
        calls.append(request)
        return {"output": {"summary": request["rendered_prompt"], "sources": [request["prompt_path"]]}}

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db, agent_runner=runner)

    result = engine.run_until_idle(
        prompt_file_agent_workflow,
        {"topic": "prompt files", "prompt_path": str(prompt_path)},
        workflow_id="wf_prompt_file_agent",
    )

    assert result.status == "completed"
    assert result.result == {"summary": "Research prompt files from durable context", "sources": [str(prompt_path.resolve())]}
    assert len(calls) == 1
    assert calls[0]["prompt"] == "Research {{ topic }} from durable context"
    assert calls[0]["rendered_prompt"] == "Research prompt files from durable context"
    assert calls[0]["prompt_path"] == str(prompt_path.resolve())
    assert calls[0]["template_path"] == str(prompt_path.resolve())
    assert calls[0]["template_sha256"] == calls[0]["prompt_sha256"]
    assert len(calls[0]["variables_sha256"]) == 64
    assert calls[0]["fingerprint"]


def test_agent_request_carries_workspace_dir_and_worktree_isolation(tmp_path):
    calls = []

    def runner(request):
        calls.append(request)
        return {"output": {"workspace_dir": request["workspace_dir"], "isolation": request["isolation"]}}

    workspace = tmp_path / "repo-worktree"
    workspace.mkdir()
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db, agent_runner=runner)

    result = engine.run_until_idle(
        workspace_agent_workflow,
        {"plan": "ship the slice", "workspace_dir": str(workspace)},
        workflow_id="wf_workspace_agent",
    )

    assert result.status == "completed"
    assert result.result == {"workspace_dir": str(workspace.resolve()), "isolation": "worktree"}
    assert len(calls) == 1
    assert calls[0]["workspace_dir"] == str(workspace.resolve())
    assert calls[0]["isolation"] == "worktree"
    assert calls[0]["fingerprint"]


def test_agent_fingerprint_preserves_legacy_shape_when_workspace_dir_is_absent():
    call = agent("research", prompt="Research typed workflows", input={"topic": "typed workflows"}, returns=ResearchPacket)
    request = call._payload("agent:research:0")["args"][0]

    legacy_fingerprint_payload = {
        "prompt": "Research typed workflows",
        "input": {"topic": "typed workflows"},
        "returns": f"{ResearchPacket.__module__}:{ResearchPacket.__qualname__}",
        "tools": [],
        "skills": [],
        "files": [],
        "model": None,
        "variant": None,
        "isolation": "workspace",
    }
    expected = hashlib.sha256(
        json.dumps(legacy_fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert request["workspace_dir"] is None
    assert request["fingerprint"] == expected


def test_current_step_context_remains_available_as_advanced_escape_hatch(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    result = engine.run_until_idle(current_step_context_workflow, {}, workflow_id="wf_current_step_context")

    assert result.status == "completed"
    assert result.result == "StepExecutionContext"


def test_agent_workspace_dir_rejects_empty_path():
    call = agent("workspace", prompt="Report workspace", workspace_dir=" ")

    with pytest.raises(TypeError, match="workspace_dir"):
        call._payload("agent:workspace:0")


def test_agent_replay_fails_loudly_when_prompt_or_input_fingerprint_changes(tmp_path):
    global PROMPT_VERSION
    PROMPT_VERSION = "v1"
    db = tmp_path / "workflow.sqlite"
    calls = []

    def runner(request):
        calls.append(request)
        return {"output": {"summary": "memoized", "sources": [request["input_sha256"]]}}

    engine = WorkflowEngine(db, agent_runner=runner)
    first = engine.run_until_idle(memoized_agent_workflow, {"topic": "memoization"}, workflow_id="wf_memoized")

    assert first.status == "waiting"
    assert len(calls) == 1
    operator_step = engine.workflow_status("wf_memoized")["operator_steps"][0]

    PROMPT_VERSION = "v2"
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
    review_request = status["review_requests"][0]
    assert review_request["kind"] == "human_input"
    assert review_request["key"] == "choose_angle"
    assert review_request["request_schema"]["id"].endswith(":AngleChoice")
    assert review_request["request_schema"]["fields"][0]["name"] == "angle_id"
    assert review_request["input_surface"]["kind"] == "structured_form"
    assert review_request["source"] is None

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
    review_requests = engine.workflow_status("wf_parallel_ask")["review_requests"]
    assert [request["key"] for request in review_requests] == ["review_one", "review_two"]
    assert [request["input_surface"]["kind"] for request in review_requests] == ["review_decision", "review_decision"]
    assert review_requests[0]["request_schema"]["fields"][0] == {
        "name": "action",
        "kind": "choice",
        "options": ["approve", "request_changes"],
        "required": True,
    }
    assert review_requests[0]["input_surface"]["actions"] == [
        {"value": "approve", "label": "Approve"},
        {"value": "request_changes", "label": "Request changes", "requires_feedback": True},
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
        payload={"action": "request_changes", "feedback": "tighten"},
        source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "m-two"},
    )
    result = engine.drain("wf_parallel_ask", initial=two)

    assert result.status == "completed"
    assert result.result == ["approve", "request_changes"]


def test_dataclass_action_literal_automatically_drives_review_actions(tmp_path):
    @workflow
    async def publish_choice_workflow(inputs):
        decision = await ask(
            prompt="Review publish choice",
            key="review_publish_choice",
            input={"draft": inputs["draft"]},
            returns=PublishChoice,
        )
        assert isinstance(decision, PublishChoice)
        return {"action": decision.action, "feedback": decision.feedback}

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(publish_choice_workflow, {"draft": "hello"}, workflow_id="wf_publish_choice")

    assert first.status == "waiting"
    request = engine.workflow_status("wf_publish_choice")["review_requests"][0]
    assert request["request_schema"]["name"] == "PublishChoice"
    assert request["request_schema"]["fields"][0] == {
        "name": "action",
        "kind": "choice",
        "options": ["ship", "revise"],
        "required": True,
    }
    assert request["input_surface"] == {
        "kind": "review_decision",
        "actions": [
            {"value": "ship", "label": "Ship"},
            {"value": "revise", "label": "Revise", "requires_feedback": True},
        ],
        "feedback": {"kind": "text", "optional": True, "placeholder": "What should change?"},
    }

    engine.submit_operator_response(
        workflow_id="wf_publish_choice",
        key="review_publish_choice",
        payload={"action": "revise", "feedback": "needs a sharper opener"},
        source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "m-publish"},
    )
    result = engine.drain("wf_publish_choice")

    assert result.status == "completed"
    assert result.result == {"action": "revise", "feedback": "needs a sharper opener"}


def test_dataclass_schema_includes_annotated_and_metadata_descriptions(tmp_path):
    @workflow
    async def described_publish_choice_workflow(inputs):
        decision = await ask(
            prompt="Review described publish choice",
            key="review_described_publish_choice",
            input={"draft": inputs["draft"]},
            returns=DescribedPublishChoice,
        )
        return {"action": decision.action, "feedback": decision.feedback}

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(
        described_publish_choice_workflow,
        {"draft": "hello"},
        workflow_id="wf_described_publish_choice",
    )

    assert first.status == "waiting"
    request = engine.workflow_status("wf_described_publish_choice")["review_requests"][0]
    assert request["request_schema"]["fields"] == [
        {
            "name": "action",
            "kind": "choice",
            "required": True,
            "description": "Publish decision to record",
            "options": ["ship", "revise"],
        },
        {
            "name": "expected_attendees",
            "kind": "number",
            "required": True,
            "description": "Expected attendee count used for venue planning",
        },
        {
            "name": "feedback",
            "kind": "text",
            "required": False,
            "description": "Optional reviewer feedback",
        },
    ]


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


@workflow
async def inferred_agent_names_workflow(inputs):
    research = await agent(prompt="Research inferred names", input=inputs, returns=ResearchPacket)
    repeat = await agent(prompt="Repeat inferred name once", input=inputs, returns=ResearchPacket)
    repeat = await agent(prompt="Repeat inferred name twice", input=inputs, returns=ResearchPacket)
    explicit = await agent("explicit_writer", prompt="Explicit name still wins", key="writer-key", returns=DraftPacket)
    return {
        "research": research.summary,
        "repeat": repeat.summary,
        "explicit": explicit.text,
    }


def draft_answer(previous=None):
    return agent(prompt="Draft until accepted", input={"previous": getattr(previous, "text", None)}, returns=DraftPacket)


def score_draft(draft):
    return agent(prompt="Score draft", input={"draft": draft.text}, returns=bool)


@workflow
async def goal_inferred_names_workflow(inputs):
    draft = await goal(draft_answer, score_draft, max_iters=2)
    return draft.text


def test_agent_infers_public_names_and_repeated_keys_are_deterministic(tmp_path):
    calls = []

    def runner(request):
        calls.append(request)
        if request["returns"].endswith(":ResearchPacket"):
            return {"output": {"summary": request["public_name"], "sources": [request["step_key"]]}}
        return {"output": {"text": request["public_name"]}}

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db, agent_runner=runner)
    result = engine.run_until_idle(inferred_agent_names_workflow, {"topic": "names"}, workflow_id="wf_inferred_names")

    assert result.status == "completed"
    assert result.result == {"research": "research", "repeat": "repeat", "explicit": "explicit_writer"}
    assert [call["name"] for call in calls] == ["research", "repeat", "repeat", "explicit_writer"]
    assert [call["public_name"] for call in calls] == ["research", "repeat", "repeat", "explicit_writer"]
    assert [call["name_source"] for call in calls] == ["assignment", "assignment", "assignment", "explicit"]
    assert [call["step_key"] for call in calls] == [
        "agent:research:0",
        "agent:repeat:0",
        "agent:repeat:1",
        "writer-key",
    ]

    status = engine.workflow_status("wf_inferred_names")
    agent_steps = [step for step in status["steps"] if step.get("step_type") != "operator"]
    assert [step["label"] for step in agent_steps] == ["research", "repeat", "repeat", "explicit writer"]
    assert [step["public_name"] for step in agent_steps] == ["research", "repeat", "repeat", "explicit_writer"]

    replay_calls = []
    replay = WorkflowEngine(db, agent_runner=lambda request: replay_calls.append(request) or {"output": "wrong"})
    replayed = replay.run_until_idle(inferred_agent_names_workflow, {"topic": "names"}, workflow_id="wf_inferred_names")
    assert replayed.status == "completed"
    assert replayed.result == result.result
    assert replay_calls == []


def test_goal_infers_callable_names_for_agent_steps(tmp_path):
    score_attempts = 0
    calls = []

    def runner(request):
        nonlocal score_attempts
        calls.append(request)
        if request["public_name"] == "score_draft":
            score_attempts += 1
            return {"output": score_attempts >= 2}
        return {"output": {"text": f"draft-{score_attempts + 1}"}}

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db, agent_runner=runner)
    result = engine.run_until_idle(goal_inferred_names_workflow, {}, workflow_id="wf_goal_names")

    assert result.status == "completed"
    assert result.result == "draft-2"
    assert [call["public_name"] for call in calls] == ["draft_answer", "score_draft", "draft_answer", "score_draft"]
    assert [call["step_key"] for call in calls] == [
        "agent:draft_answer:0",
        "agent:score_draft:0",
        "agent:draft_answer:1",
        "agent:score_draft:1",
    ]
    status = engine.workflow_status("wf_goal_names")
    assert [step["label"] for step in status["steps"]] == ["draft answer", "score draft", "draft answer", "score draft"]

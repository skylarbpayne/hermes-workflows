from pathlib import Path

import pytest

from hermes_workflows import AgentPrompt, WorkflowEngine, workflow
from hermes_workflows.prompts import render_prompt


@workflow
async def prompt_workflow(ctx, inputs):
    return await AgentPrompt(
        Path(inputs["prompt_path"]),
        goal=inputs["goal"],
        commands=inputs["commands"],
    )(ctx)


@workflow
async def prompt_then_approval_workflow(ctx, inputs):
    prompt = await AgentPrompt(inputs["prompt_path"], goal=inputs["goal"])(ctx)
    decision = await ctx.approval.request(
        "Approve rendered prompt?",
        key="approve_prompt",
        artifact=prompt,
        approver="human:skylar",
    )
    return {"prompt": prompt, "decision": decision}


@workflow
async def gather_prompts_workflow(ctx, inputs):
    left, right = await ctx.gather(
        AgentPrompt(inputs["left_path"], value="left")(ctx),
        AgentPrompt(inputs["right_path"], value="right")(ctx),
    )
    return {"left": left["rendered_prompt"], "right": right["rendered_prompt"]}


def test_render_prompt_substitutes_strings_and_json_values():
    rendered = render_prompt(
        "Goal: {{goal}}\nCommands:\n{{commands}}\n",
        {"goal": "Ship it", "commands": ["pytest -q", "git diff --check"]},
    )

    assert rendered == 'Goal: Ship it\nCommands:\n[\n  "pytest -q",\n  "git diff --check"\n]\n'


def test_render_prompt_rejects_missing_variables():
    with pytest.raises(KeyError, match="missing prompt variables: repo_path"):
        render_prompt("Repo: {{repo_path}}", {})


def test_agent_prompt_runs_as_durable_step(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# Plan\n\nGoal: {{goal}}\n\nCommands:\n{{commands}}\n")
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    result = engine.run_until_idle(
        prompt_workflow,
        {"prompt_path": str(prompt), "goal": "Ship it", "commands": ["pytest -q"]},
        workflow_id="wf_prompt",
    )

    assert result.status == "completed"
    assert result.result["kind"] == "agent_prompt.rendered.v1"
    assert result.result["prompt_path"] == str(prompt)
    assert result.result["variables"] == {"goal": "Ship it", "commands": ["pytest -q"]}
    assert "Goal: Ship it" in result.result["rendered_prompt"]
    assert '"pytest -q"' in result.result["rendered_prompt"]
    assert len(result.result["prompt_sha256"]) == 64
    assert len(result.result["rendered_prompt_sha256"]) == 64


def test_agent_prompt_step_requested_snapshots_prompt_text(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Goal: {{goal}}\n")
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    first = engine.start(
        prompt_then_approval_workflow,
        {"prompt_path": str(prompt), "goal": "Ship it"},
        workflow_id="wf_prompt_snapshot",
    )

    assert first.status == "waiting"
    requested = [event for event in engine.events("wf_prompt_snapshot") if event["type"] == "StepRequested"][0]
    request = requested["payload"]["args"][0]
    assert request["prompt_text"] == "Goal: {{goal}}\n"
    assert request["rendered_prompt"] == "Goal: Ship it\n"
    assert request["prompt_path"] == str(prompt)


def test_completed_agent_prompt_replay_does_not_need_prompt_file(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Goal: {{goal}}\nCommands: {{commands}}\n")
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    result = engine.run_until_idle(
        prompt_workflow,
        {"prompt_path": str(prompt), "goal": "Ship it", "commands": []},
        workflow_id="wf_prompt_replay",
    )
    assert result.status == "completed"

    prompt.unlink()
    restarted = WorkflowEngine(tmp_path / "workflow.sqlite")
    replayed = restarted.run_until_idle(
        prompt_workflow,
        {"prompt_path": str(prompt), "goal": "Ship it", "commands": []},
        workflow_id="wf_prompt_replay",
    )

    assert replayed.status == "completed"
    assert replayed.result == result.result


def test_pending_agent_prompt_uses_requested_snapshot_after_file_edit(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Goal: {{goal}}\n")
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    started = engine.start(
        prompt_workflow,
        {"prompt_path": str(prompt), "goal": "Original", "commands": []},
        workflow_id="wf_prompt_edit",
    )
    assert started.status == "waiting"

    prompt.write_text("CHANGED: {{goal}}\n")
    drained = engine.drain("wf_prompt_edit")

    assert drained.status == "completed"
    assert drained.result["rendered_prompt"] == "Goal: Original\n"


def test_agent_prompt_works_with_gather(tmp_path):
    left = tmp_path / "left.md"
    right = tmp_path / "right.md"
    left.write_text("Left {{value}}")
    right.write_text("Right {{value}}")
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    result = engine.run_until_idle(
        gather_prompts_workflow,
        {"left_path": str(left), "right_path": str(right)},
        workflow_id="wf_prompt_gather",
    )

    assert result.status == "completed"
    assert result.result == {"left": "Left left", "right": "Right right"}

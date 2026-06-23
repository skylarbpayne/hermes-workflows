from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from hermes_workflows import WorkflowEngine
from hermes_workflows.examples.reviewable_draft import reviewable_draft_workflow


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_example(filename: str, attr: str):
    path = REPO_ROOT / "examples" / filename
    spec = importlib.util.spec_from_file_location(f"launch_example_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return getattr(module, attr)


def _drain(engine: WorkflowEngine, workflow_id: str, *, max_commands: int = 20):
    result = None
    for _ in range(max_commands):
        result = engine.worker_once(workflow_id, worker_id="launch-example-worker")
        if result.status in {"completed", "failed"}:
            break
    assert result is not None
    return result


def _review_keys(engine: WorkflowEngine, workflow_id: str) -> set[str]:
    status = engine.workflow_status(workflow_id)
    return {request["key"] for request in status["review_requests"]} | {approval["key"] for approval in status["approvals"]}


def _drain_until_review_key(engine: WorkflowEngine, workflow_id: str, key: str, *, max_commands: int = 30):
    result = None
    for _ in range(max_commands):
        result = engine.worker_once(workflow_id, worker_id="launch-example-worker")
        if key in _review_keys(engine, workflow_id):
            return result
        if result.status == "failed":
            break
    assert result is not None
    return result


def test_installed_reviewable_draft_quickstart_reaches_typed_review_queue(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    started = engine.start(reviewable_draft_workflow, {"topic": "Launch docs"}, workflow_id="wf_reviewable_draft")
    assert started.status == "running"

    result = _drain(engine, "wf_reviewable_draft")

    assert result.status == "waiting"
    assert _review_keys(engine, "wf_reviewable_draft") == {"review_draft_packet"}


def test_launch_examples_reach_expected_review_queue_requests(tmp_path):
    cases = [
        ("bash_repo_health.py", "bash_repo_health_workflow", "wf_bash", {"review_repo_health"}),
        ("parallel_research.py", "parallel_research_workflow", "wf_parallel", {"review_research_packet"}),
        (
            "pipeline_section_review.py",
            "pipeline_section_review_workflow",
            "wf_pipeline",
            {"review_section_api", "review_section_worker"},
        ),
    ]
    for filename, attr, workflow_id, expected_keys in cases:
        workflow_fn = _load_example(filename, attr)
        engine = WorkflowEngine(tmp_path / f"{workflow_id}.sqlite")
        engine.start(workflow_fn, {}, workflow_id=workflow_id)

        result = _drain(engine, workflow_id)

        assert result.status == "waiting"
        assert expected_keys <= _review_keys(engine, workflow_id)


def test_content_and_event_planning_examples_reach_approval_gates(tmp_path):
    cases = [
        ("content_asset_lane.py", "content_asset_lane_workflow", "wf_content_asset_lane", "select_content_topic"),
        ("event_planning_demo.py", "event_planning_demo_workflow", "wf_event_planning", "approve_event_ops_packet"),
    ]
    for filename, attr, workflow_id, expected_key in cases:
        workflow_fn = _load_example(filename, attr)
        engine = WorkflowEngine(tmp_path / f"{workflow_id}.sqlite")
        engine.start(workflow_fn, {}, workflow_id=workflow_id)

        result = _drain_until_review_key(engine, workflow_id, expected_key)

        assert result.status in {"running", "waiting"}
        assert expected_key in _review_keys(engine, workflow_id)


def test_content_asset_lane_includes_gemini_visual_generation_step():
    source = (REPO_ROOT / "examples" / "content_asset_lane.py").read_text()

    assert "gemini-nano-banana-2" in source
    assert "plan_blog_visual_elements" in source
    assert "approve_blog_visual_elements_plan" in source
    assert "generate_blog_visual_elements_with_gemini_nano_banana_2" in source
    assert "visual-generation receipt" in source or "visual-generation receipts" in source


def test_dynamic_workflow_return_example_generates_and_runs_child_workflows(tmp_path):
    workflow_fn = _load_example("dynamic_workflow_return.py", "dynamic_workflow_return_workflow")
    engine = WorkflowEngine(tmp_path / "wf_dynamic_return.sqlite")
    engine.start(workflow_fn, {}, workflow_id="wf_dynamic_return")

    result = _drain(engine, "wf_dynamic_return", max_commands=20)

    assert result.status == "completed"
    assert result.result["generated_workflow"]["symbol"] == "process_launch_item"
    assert [item["id"] for item in result.result["processed"]] == ["dynamic-examples", "subworkflow-ui"]
    assert result.result["processed"][0]["summary"].startswith("Dynamic workflow examples -> docs")
    events = engine.events("wf_dynamic_return")
    assert [event["type"] for event in events].count("ChildWorkflowRequested") == 2
    assert [event["type"] for event in events].count("ChildWorkflowCompleted") == 2
    child_ids = [
        event["payload"]["child_workflow_id"]
        for event in events
        if event["type"] == "ChildWorkflowRequested"
    ]
    assert child_ids == [
        "wf_dynamic_return.child.map:0:process_launch_item:"
        + result.result["generated_workflow"]["source_sha256"][:12]
        + ".dynamic-examples",
        "wf_dynamic_return.child.map:0:process_launch_item:"
        + result.result["generated_workflow"]["source_sha256"][:12]
        + ".subworkflow-ui",
    ]
    assert all(engine.workflow_status(child_id)["status"] == "completed" for child_id in child_ids)


def test_goal_and_local_model_examples_complete_without_provider_credentials(tmp_path):
    cases = [
        ("goal_revision_loop.py", "goal_revision_loop_workflow", "wf_goal"),
        ("local_model_adapter_workflow.py", "local_model_adapter_workflow", "wf_local_model"),
    ]
    loaded = {}
    for filename, attr, workflow_id in cases:
        workflow_fn = _load_example(filename, attr)
        loaded[attr] = workflow_fn
        engine = WorkflowEngine(tmp_path / f"{workflow_id}.sqlite")
        engine.start(workflow_fn, {}, workflow_id=workflow_id)

        result = _drain(engine, workflow_id)

        assert result.status == "completed"

    engine = WorkflowEngine(tmp_path / "wf_goal_exhausted.sqlite")
    engine.start(loaded["goal_revision_loop_workflow"], {"target_score": 3}, workflow_id="wf_goal_exhausted")
    exhausted = _drain(engine, "wf_goal_exhausted")

    assert exhausted.status == "completed"
    assert exhausted.result["accepted"] is False

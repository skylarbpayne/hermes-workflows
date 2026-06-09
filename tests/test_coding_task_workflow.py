from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_workflows import WorkflowEngine
from examples.coding_task_workflow import coding_task_workflow
from tests.test_repo_pr_workflow import init_repo, run


def test_coding_task_workflow_emits_plan_evidence_and_review_artifacts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "README.md").write_text("# Demo\n\nCoding workflow dogfood.\n")
    run(["git", "add", "README.md"], repo)

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    inputs = {
        "repo_path": str(repo),
        "task": "Add /workflows code view and run DAG artifact explorer",
        "verification_commands": ["python -m py_compile demo.py"],
        "plan_path": str(tmp_path / "coding-plan.md"),
        "evidence_path": str(tmp_path / "coding-evidence.md"),
        "review_packet_path": str(tmp_path / "coding-review.md"),
    }

    result = engine.run_until_idle(
        coding_task_workflow,
        inputs,
        workflow_id="wf_coding_task",
        workflow_ref="examples.coding_task_workflow:coding_task_workflow",
    )

    assert result.status == "completed"
    assert result.result["task"] == inputs["task"]
    assert result.result["artifact_paths"] == {
        "plan": str(tmp_path / "coding-plan.md"),
        "evidence": str(tmp_path / "coding-evidence.md"),
        "review_packet": str(tmp_path / "coding-review.md"),
    }
    assert result.result["verification"]["ok"] is False
    assert "demo.py" in result.result["verification"]["results"][0]["output"]

    plan = (tmp_path / "coding-plan.md").read_text()
    assert "# Coding task plan" in plan
    assert "Add /workflows code view" in plan
    assert "## Acceptance checks" in plan
    assert "## Artifacts this workflow will produce" in plan

    evidence = (tmp_path / "coding-evidence.md").read_text()
    assert "# Coding task evidence" in evidence
    assert "git status --short" in evidence
    assert "python -m py_compile demo.py" in evidence
    assert "README.md" in evidence

    review = (tmp_path / "coding-review.md").read_text()
    assert "# Coding task review packet" in review
    assert "Time-saving check" in review
    assert "ready_for_pr: false" in review

    events = engine.workflow_status("wf_coding_task", recent_events=100)["events"]
    completed = [event for event in events if event["type"] == "StepCompleted"]
    assert [event["key"] for event in completed] == [
        "step:inspect_coding_repo:0",
        "step:write_coding_plan:0",
        "step:collect_coding_evidence:0",
        "step:write_coding_review_packet:0",
    ]
    for event in completed:
        output = event["payload"]["output"]
        assert output["kind"].startswith("coding_task_")
        assert output["artifact_path"]


def test_coding_task_workflow_passes_when_repo_is_clean_and_checks_pass(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "demo.py").write_text("VALUE = 1\n")
    run(["git", "add", "demo.py"], repo)
    subprocess.run(
        ["git", "commit", "-m", "add demo"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )

    result = WorkflowEngine(tmp_path / "workflow.sqlite").run_until_idle(
        coding_task_workflow,
        {
            "repo_path": str(repo),
            "task": "Verify a clean coding task",
            "verification_commands": ["python -m py_compile demo.py"],
            "plan_path": str(tmp_path / "plan.md"),
            "evidence_path": str(tmp_path / "evidence.md"),
            "review_packet_path": str(tmp_path / "review.md"),
        },
        workflow_id="wf_coding_task_clean",
        workflow_ref="examples.coding_task_workflow:coding_task_workflow",
    )

    assert result.status == "completed"
    assert result.result["verification"]["ok"] is True
    assert result.result["ready_for_pr"] is True
    assert "ready_for_pr: true" in (tmp_path / "review.md").read_text()

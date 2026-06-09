from __future__ import annotations

import subprocess
from pathlib import Path
from typing import get_type_hints

from hermes_workflows import WorkflowEngine
from hermes_workflows.workflows.coding import CodingWorkflowInput, CodingWorkflowResult, coding_workflow
from tests.test_repo_pr_workflow import init_repo, run


def _human_source(message_id: str = "10") -> dict:
    return {
        "kind": "human",
        "id": "skylar",
        "channel": "discord",
        "message_url": f"discord://thread/1/message/{message_id}",
    }


def test_coding_workflow_has_typed_public_input_and_output_contract():
    hints = get_type_hints(coding_workflow)
    input_hints = get_type_hints(CodingWorkflowInput)
    output_hints = get_type_hints(CodingWorkflowResult)

    assert hints["inputs"] is CodingWorkflowInput
    assert hints["return"] is CodingWorkflowResult
    assert input_hints["repo_path"] is str
    assert output_hints["ready"] is bool
    assert output_hints["artifact_paths"] == dict[str, str]


def test_coding_workflow_requires_plan_ready_signal_and_review_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "README.md").write_text("# Demo\n\nCoding workflow dogfood.\n")
    run(["git", "add", "README.md"], repo)
    subprocess.run(
        ["git", "commit", "-m", "add readme"],
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

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    inputs = {
        "repo_path": str(repo),
        "goal": "Add dashboard workflow DAG source loading regression",
        "verification_commands": ["python -m py_compile demo.py"],
        "plan_path": str(tmp_path / "coding-plan.md"),
        "evidence_path": str(tmp_path / "coding-evidence.md"),
        "review_packet_path": str(tmp_path / "coding-review.md"),
        "commit": False,
        "push": False,
    }

    first = engine.run_until_idle(
        coding_workflow,
        inputs,
        workflow_id="wf_coding",
        workflow_ref="hermes_workflows.workflows.coding:coding_workflow",
    )

    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_coding_plan"
    plan = (tmp_path / "coding-plan.md").read_text()
    assert "# Coding workflow plan" in plan
    assert "Add dashboard workflow DAG" in plan
    assert "approve_coding_plan" in plan
    assert "handoff.completed:coding_ready" in plan
    assert "approve_coding_review" in plan

    after_plan = WorkflowEngine(db).signal(
        "wf_coding",
        "approval.decision",
        key="approve_coding_plan",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source("10"),
        idempotency_key="plan-approval",
    )
    assert after_plan.status == "waiting"
    assert after_plan.waiting_on == "signal:handoff.completed:coding_ready"

    (repo / "demo.py").write_text("VALUE = 1\n")
    after_ready = WorkflowEngine(db).signal(
        "wf_coding",
        "handoff.completed",
        key="coding_ready",
        payload={"by": "palmer", "summary": "Added demo module and ran local implementation."},
        source={"kind": "agent", "id": "palmer", "channel": "test"},
        idempotency_key="implementation-ready",
    )
    assert after_ready.status == "waiting"
    assert after_ready.waiting_on == "signal:approval.decision:approve_coding_review"

    evidence = (tmp_path / "coding-evidence.md").read_text()
    assert "# Coding workflow evidence" in evidence
    assert "python -m py_compile demo.py" in evidence
    assert "demo.py" in evidence
    review = (tmp_path / "coding-review.md").read_text()
    assert "# Coding workflow review packet" in review
    assert "Plan approved by: skylar" in review
    assert "Implementation signaled by: palmer" in review
    assert "Recommendation: approve" in review

    final = WorkflowEngine(db).signal(
        "wf_coding",
        "approval.decision",
        key="approve_coding_review",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source("11"),
        idempotency_key="review-approval",
    )
    assert final.status == "completed"
    assert final.result["ready"] is True
    assert final.result["committed"] is False
    assert final.result["pushed"] is False
    assert final.result["approval_gates"] == ["approve_coding_plan", "handoff.completed:coding_ready", "approve_coding_review"]


def test_coding_workflow_rejection_stops_before_implementation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)

    db = tmp_path / "workflow.sqlite"
    first = WorkflowEngine(db).run_until_idle(
        coding_workflow,
        {
            "repo_path": str(repo),
            "goal": "Rejected change",
            "plan_path": str(tmp_path / "plan.md"),
            "evidence_path": str(tmp_path / "evidence.md"),
            "review_packet_path": str(tmp_path / "review.md"),
        },
        workflow_id="wf_coding_reject",
        workflow_ref="hermes_workflows.workflows.coding:coding_workflow",
    )
    assert first.status == "waiting"
    result = WorkflowEngine(db).signal(
        "wf_coding_reject",
        "approval.decision",
        key="approve_coding_plan",
        payload={"action": "reject", "by": "skylar"},
        source=_human_source("20"),
        idempotency_key="plan-reject",
    )
    assert result.status == "completed"
    assert result.result["ready"] is False
    assert result.result["stage"] == "plan_rejected"
    assert not (tmp_path / "evidence.md").exists()


def test_coding_workflow_allows_review_approver_to_match_implementer(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)

    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        coding_workflow,
        {
            "repo_path": str(repo),
            "goal": "Change README",
            "verification_commands": ["python -m py_compile demo.py"],
            "plan_path": str(tmp_path / "plan.md"),
            "evidence_path": str(tmp_path / "evidence.md"),
            "review_packet_path": str(tmp_path / "review.md"),
        },
        workflow_id="wf_coding_same_person",
    )
    WorkflowEngine(db).signal(
        "wf_coding_same_person",
        "approval.decision",
        key="approve_coding_plan",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source("30"),
        idempotency_key="plan-approval",
    )
    (repo / "demo.py").write_text("VALUE = 1\n")
    WorkflowEngine(db).signal(
        "wf_coding_same_person",
        "handoff.completed",
        key="coding_ready",
        payload={"by": "skylar", "summary": "Changed code directly."},
        idempotency_key="implementation-ready",
    )

    final = WorkflowEngine(db).signal(
        "wf_coding_same_person",
        "approval.decision",
        key="approve_coding_review",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source("31"),
        idempotency_key="review-approval",
    )

    assert final.status == "completed"
    assert final.result["ready"] is True
    assert final.result["committed"] is False
    assert "review approver must be different from implementer" not in (final.error or "")

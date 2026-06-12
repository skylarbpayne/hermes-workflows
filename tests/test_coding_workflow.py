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


def _drain_signal(db: Path, workflow_id: str, signal_type: str, **kwargs):
    engine = WorkflowEngine(db)
    result = engine.signal(workflow_id, signal_type, **kwargs)
    return engine.drain(workflow_id, initial=result)


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
    (repo / "demo.py").write_text("VALUE = 1\n")
    run(["git", "add", "README.md", "demo.py"], repo)
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
        "goal": "Change demo module VALUE from 1 to 2",
        "verification_commands": ["python -m py_compile demo.py", "python -c 'import demo; assert demo.VALUE == 2'"],
        "before_after": {
            "before": "demo.VALUE is 1 in demo.py before implementation.",
            "after": "demo.VALUE is 2 after implementation, and importing demo confirms the value.",
        },
        "implementation_steps": [
            "Open demo.py and change the configured VALUE constant from 1 to 2.",
            "Run py_compile and a tiny import/value check.",
            "Leave the diff for the evidence packet after approval.",
        ],
        "non_goals": ["Do not touch package/runtime/dashboard files in this demo."],
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
    assert "Change demo module VALUE" in plan
    assert "approve_coding_plan" in plan
    assert "implementation_handoff" in plan
    assert "approve_coding_review" in plan
    assert "handoff.completed:coding_ready" not in plan
    assert "ctx.handoff" not in plan
    assert "## Before / after" in plan
    assert "## Concrete implementation steps" in plan
    assert "## Dashboard preview" in plan
    assert "## Non-goals" in plan
    assert "## Rollback / stop conditions" in plan
    assert "demo.VALUE is 1" in plan
    assert "demo.VALUE is 2" in plan
    assert "Do not touch package/runtime/dashboard files" in plan
    assert "No source files will be modified before this approval is recorded." in plan
    approval = engine.get_approval("wf_coding", "approve_coding_plan")
    assert approval.artifact["kind"] == "markdown"
    assert approval.authority == []
    assert "authority" not in approval.artifact
    assert approval.artifact["render"] == "inline-markdown"
    assert approval.artifact["markdown"] == plan
    assert approval.artifact["summary"].startswith("Approve a concrete coding plan")
    assert approval.artifact["sections"]["before_after"]["before"]
    assert approval.artifact["sections"]["examples"]
    assert approval.artifact["sections"]["visuals"][0]["type"] == "flow"
    assert approval.artifact["sections"]["non_goals"] == ["Do not touch package/runtime/dashboard files in this demo."]
    assert approval.artifact["sections"]["rollback"]

    assert run(["git", "status", "--short"], repo).stdout.strip() == ""

    after_plan = _drain_signal(
        db,
        "wf_coding",
        "approval.decision",
        key="approve_coding_plan",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source("10"),
        idempotency_key="plan-approval",
    )
    assert after_plan.status == "waiting"
    assert after_plan.waiting_on == "signal:handoff.completed:coding_ready"

    (repo / "demo.py").write_text("VALUE = 2\n")
    after_ready = _drain_signal(
        db,
        "wf_coding",
        "handoff.completed",
        key="coding_ready",
        payload={"by": "palmer", "summary": "Added demo module and ran local implementation."},
        source={"kind": "agent", "id": "palmer", "channel": "test"},
        idempotency_key="implementation-ready",
    )
    assert after_ready.status == "waiting"
    assert after_ready.waiting_on == "signal:approval.decision:approve_coding_review"
    review_approval = WorkflowEngine(db).get_approval("wf_coding", "approve_coding_review")
    assert review_approval.authority == []
    assert "authority" not in review_approval.artifact
    assert "handoff.completed:coding_ready" not in review_approval.artifact["plan"]["markdown"]

    evidence = (tmp_path / "coding-evidence.md").read_text()
    assert "# Coding workflow evidence" in evidence
    assert "python -m py_compile demo.py" in evidence
    assert "demo.py" in evidence
    review = (tmp_path / "coding-review.md").read_text()
    assert "# Coding workflow review packet" in review
    assert "Plan approved by: skylar" in review
    assert "Implementation signaled by: palmer" in review
    assert "Recommendation: approve" in review

    final = _drain_signal(
        db,
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
    assert final.result["approval_gates"] == ["approve_coding_plan", "implementation_handoff", "approve_coding_review"]


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
    plan = (tmp_path / "plan.md").read_text()
    assert "implementation_handoff" in plan
    assert "handoff.completed:coding_ready" not in plan
    assert "ctx.handoff" not in plan
    result = _drain_signal(
        db,
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
    _drain_signal(
        db,
        "wf_coding_same_person",
        "approval.decision",
        key="approve_coding_plan",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source("30"),
        idempotency_key="plan-approval",
    )
    (repo / "demo.py").write_text("VALUE = 1\n")
    _drain_signal(
        db,
        "wf_coding_same_person",
        "handoff.completed",
        key="coding_ready",
        payload={"by": "skylar", "summary": "Changed code directly."},
        idempotency_key="implementation-ready",
    )

    final = _drain_signal(
        db,
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

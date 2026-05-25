import subprocess
from pathlib import Path

from hermes_workflows import WorkflowEngine
from examples.repo_change_review_workflow import repo_change_review_workflow


def run(command, cwd):
    return subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)


def init_repo(path: Path) -> None:
    (path / "README.md").write_text("# Demo\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_demo.py").write_text("def test_demo():\n    assert True\n")
    run(["git", "init", "-b", "main"], path)
    run(["git", "add", "."], path)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
        env={"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"},
    )


def test_repo_change_review_pauses_for_plan_then_implementation_then_landing_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    report_path = tmp_path / "report.md"
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        repo_change_review_workflow,
        {
            "repo_path": str(repo),
            "goal": "Change README",
            "verification_commands": ["pytest -q"],
            "report_path": str(report_path),
            "commit": False,
            "push": False,
        },
        workflow_id="wf_change",
    )
    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_change_plan"

    after_plan = WorkflowEngine(db).signal(
        "wf_change",
        "approval.decision",
        key="approve_change_plan",
        payload={"action": "approve", "by": "skylar"},
        idempotency_key="plan-approval",
    )
    assert after_plan.status == "waiting"
    assert after_plan.waiting_on == "signal:implementation.ready:change_ready"

    (repo / "README.md").write_text("# Demo\n\nChanged.\n")
    (repo / "NEW.md").write_text("new file\n")

    after_ready = WorkflowEngine(db).signal(
        "wf_change",
        "implementation.ready",
        key="change_ready",
        payload={"by": "palmer", "summary": "README changed"},
        idempotency_key="implementation-ready",
    )
    assert after_ready.status == "waiting"
    assert after_ready.waiting_on == "signal:approval.decision:approve_change_landing"

    landing = WorkflowEngine(db).signal(
        "wf_change",
        "approval.decision",
        key="approve_change_landing",
        payload={"action": "approve", "by": "skylar"},
        idempotency_key="landing-approval",
    )
    assert landing.status == "completed"
    assert landing.result["ready"] is True
    assert landing.result["committed"] is False
    assert report_path.exists()
    report = report_path.read_text()
    assert "# Repo change review: Change README" in report
    assert "Plan approved by: skylar" in report
    assert "Landing approved by: skylar" in report
    assert "Tests: pass" in report
    assert "NEW.md" in report

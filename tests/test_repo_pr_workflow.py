import asyncio
import subprocess
from pathlib import Path

from hermes_workflows import WorkflowEngine
from examples import repo_pr_workflow as pr_module
from examples.repo_pr_workflow import repo_pr_workflow


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
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    run(["git", "remote", "add", "origin", str(path)], path)
    run(["git", "fetch", "origin", "main:refs/remotes/origin/main"], path)
    run(["git", "checkout", "-b", "feat/pr-path"], path)
    (path / "README.md").write_text("# Demo\n\nWorkflow-backed PR path.\n")
    run(["git", "add", "."], path)
    subprocess.run(
        ["git", "commit", "-m", "feat: add workflow-backed PR path"],
        cwd=path,
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


def workflow_inputs(repo: Path, tmp_path: Path):
    return {
        "repo_path": str(repo),
        "goal": "Add workflow-backed PR path",
        "summary": ["Adds durable PR evidence, verification, status, and approval provenance."],
        "verification_commands": ["pytest -q"],
        "pr_body_path": str(tmp_path / "pr-body.md"),
        "status_report_path": str(tmp_path / "pr-status.md"),
        "create_pr": False,
        "watch_checks": False,
        "merge": False,
    }


def test_repo_pr_workflow_gathers_tests_writes_body_then_waits_for_landing_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    db = tmp_path / "workflow.sqlite"

    first = WorkflowEngine(db).run_until_idle(
        repo_pr_workflow,
        workflow_inputs(repo, tmp_path),
        workflow_id="wf_pr",
    )

    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_pr_landing"
    body = (tmp_path / "pr-body.md").read_text()
    assert "## Workflow-backed PR evidence" in body
    assert "Branch: `feat/pr-path`" in body
    assert "pytest -q" in body
    assert "README.md" in body

    approval = WorkflowEngine(db).signal(
        "wf_pr",
        "approval.decision",
        key="approve_pr_landing",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "kanban", "message_url": "kanban://t_5fc570b1/comment/1"},
        idempotency_key="landing-approval",
    )

    assert approval.status == "completed"
    assert approval.result["ready"] is True
    assert approval.result["verification_ok"] is True
    assert approval.result["pr_url"] == ""
    report = (tmp_path / "pr-status.md").read_text()
    assert "Landing approval: skylar via kanban kanban://t_5fc570b1/comment/1" in report
    assert "PR: create_pr disabled" in report


def test_repo_pr_workflow_rejects_agent_or_missing_landing_provenance(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(repo_pr_workflow, workflow_inputs(repo, tmp_path), workflow_id="wf_pr")

    result = WorkflowEngine(db).signal(
        "wf_pr",
        "approval.decision",
        key="approve_pr_landing",
        payload={"action": "approve", "by": "palmer"},
        source={"kind": "agent", "id": "palmer", "channel": "kanban", "event_id": "run-1"},
        idempotency_key="bad-approval",
    )

    assert result.status == "failed"
    assert "requires human approval source" in (result.error or "")

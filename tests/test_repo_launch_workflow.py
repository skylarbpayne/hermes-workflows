import subprocess
from pathlib import Path

from hermes_workflows import WorkflowEngine
from examples.repo_launch_workflow import repo_launch_workflow


def init_tiny_repo(path: Path) -> None:
    (path / "pyproject.toml").write_text(
        """
[tool.pytest.ini_options]
testpaths = ["tests"]
""".strip()
        + "\n"
    )
    (path / "tests").mkdir()
    (path / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert 1 + 1 == 2\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"},
    )


def test_repo_launch_workflow_runs_tests_waits_for_approval_then_writes_report(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_tiny_repo(repo)
    report = tmp_path / "launch-report.md"
    db = tmp_path / "workflow.sqlite"

    engine = WorkflowEngine(db)
    waiting = engine.run_until_idle(
        repo_launch_workflow,
        {"repo_path": str(repo), "report_path": str(report), "project": "tiny repo"},
        workflow_id="wf_repo_launch",
    )

    assert waiting.status == "waiting"
    assert waiting.waiting_on == "signal:approval.decision:approve_repo_launch"
    assert not report.exists()

    approval = next(event for event in engine.events("wf_repo_launch") if event["type"] == "ApprovalRequested")
    artifact = approval["payload"]["artifact"]
    assert artifact["project"] == "tiny repo"
    assert artifact["tests"]["ok"] is True
    assert "1 passed" in artifact["tests"]["output"]
    assert artifact["git"]["clean"] is True

    signal_engine = WorkflowEngine(db)
    completed = signal_engine.signal(
        "wf_repo_launch",
        "approval.decision",
        key="approve_repo_launch",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/1/message/20"},
        idempotency_key="test-approval-1",
    )
    completed = signal_engine.drain("wf_repo_launch", initial=completed)

    assert completed.status == "completed"
    assert completed.result["report_path"] == str(report)
    assert report.exists()
    contents = report.read_text()
    assert "# Repo launch packet: tiny repo" in contents
    assert "Approved by: skylar" in contents
    assert "Approval source: discord discord://thread/1/message/20" in contents
    assert "Tests: pass" in contents

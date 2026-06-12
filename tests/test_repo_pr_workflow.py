import asyncio
import subprocess
from pathlib import Path

import pytest

from hermes_workflows import WorkflowEngine
from examples import repo_pr_workflow as pr_module
from examples.repo_pr_workflow import repo_change_plan_workflow, repo_pr_workflow


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


def approved_plan(tmp_path: Path):
    return {
        "ready_for_implementation": True,
        "plan_artifact_path": str(tmp_path / "implementation-plan.md"),
        "plan_workflow_id": "wf_plan",
        "approved_by": "skylar",
        "approval_source": {
            "kind": "human",
            "id": "skylar",
            "channel": "discord",
            "message_url": "discord://thread/plan-approved",
        },
    }


_UNSET = object()


def workflow_inputs(repo: Path, tmp_path: Path, *, implementation_plan=_UNSET):
    return {
        "repo_path": str(repo),
        "goal": "Add workflow-backed PR path",
        "summary": ["Adds durable PR evidence, verification, status, and approval provenance."],
        "verification_commands": ["pytest -q"],
        "pr_body_path": str(tmp_path / "pr-body.md"),
        "status_report_path": str(tmp_path / "pr-status.md"),
        "plan_artifact_path": str(tmp_path / "implementation-plan.md"),
        "create_pr": False,
        "watch_checks": False,
        "merge": False,
        "implementation_plan": approved_plan(tmp_path) if implementation_plan is _UNSET else implementation_plan,
    }


def _drain_signal(engine: WorkflowEngine, workflow_id: str, signal_type: str, **kwargs):
    result = engine.signal(workflow_id, signal_type, **kwargs)
    return engine.drain(workflow_id, initial=result)


def approve_plan_workflow(engine: WorkflowEngine, repo: Path, tmp_path: Path):
    engine.run_until_idle(
        repo_change_plan_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=None),
        workflow_id="wf_plan",
    )
    result = _drain_signal(
        engine,
        "wf_plan",
        "approval.decision",
        key="approve_implementation_plan",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/plan-approved"},
        idempotency_key="plan-approval",
    )
    assert result.status == "completed"
    return result.result


def test_repo_change_plan_workflow_writes_agent_prompt_plan_then_waits_for_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        repo_change_plan_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=None),
        workflow_id="wf_plan",
    )

    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_implementation_plan"
    events = engine.events("wf_plan")
    requested_steps = [event for event in events if event["type"] == "StepRequested"]
    assert [event["key"] for event in requested_steps] == [
        "step:agent_prompt:0",
        "step:write_implementation_plan:0",
        "approve_implementation_plan",
    ]
    prompt_request = requested_steps[0]["payload"]["args"][0]
    assert prompt_request["kind"] == "agent_prompt.request.v1"
    assert prompt_request["prompt_path"].endswith("examples/prompts/repo_change_plan.md")
    assert prompt_request["variables"]["workflow_id"] == "wf_plan"
    assert "# Implementation plan: Add workflow-backed PR path" in prompt_request["rendered_prompt"]
    write_request = requested_steps[1]["payload"]
    assert write_request["step_name"] == "write_implementation_plan"
    assert write_request["args"][1]["rendered_prompt_sha256"] == prompt_request["rendered_prompt_sha256"]
    plan = (tmp_path / "implementation-plan.md").read_text()
    assert plan == prompt_request["rendered_prompt"]
    assert "## Goal" in plan
    assert "## Non-goals" in plan
    assert "## Proposed file/module changes" in plan
    assert "## Approval gates" in plan
    assert "## Tests / verification" in plan
    assert "## Risks / rollback" in plan


def test_repo_change_plan_workflow_renders_plan_from_agentprompt_template(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    prompt = tmp_path / "plan-template.md"
    prompt.write_text(
        "# Custom plan for {{goal}}\n\n"
        "Repo: {{repo_path}}\n\n"
        "Workflow: {{workflow_id}}\n\n"
        "Commands:\n{{verification_commands}}\n"
    )
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    inputs = workflow_inputs(repo, tmp_path, implementation_plan=None)
    inputs["plan_prompt_path"] = str(prompt)
    inputs["verification_commands"] = ["pytest -q", "git diff --check"]

    first = engine.run_until_idle(repo_change_plan_workflow, inputs, workflow_id="wf_plan")

    assert first.status == "waiting"
    events = engine.workflow_status("wf_plan", recent_events=50)["events"]
    prompt_requests = [
        event
        for event in events
        if event["type"] == "StepRequested" and event["payload"].get("step_name") == "agent_prompt"
    ]
    assert prompt_requests
    prompt_payload = prompt_requests[0]["payload"]["args"][0]
    assert prompt_payload["prompt_path"] == str(prompt)
    assert prompt_payload["prompt_text"].startswith("# Custom plan")
    assert prompt_payload["variables"]["workflow_id"] == "wf_plan"

    plan_text = (tmp_path / "implementation-plan.md").read_text()
    assert "# Custom plan for Add workflow-backed PR path" in plan_text
    assert f"Repo: {repo.resolve()}" in plan_text
    assert "Workflow: wf_plan" in plan_text
    assert "- `pytest -q`" in plan_text
    assert "- `git diff --check`" in plan_text

    result = _drain_signal(
        engine,
        "wf_plan",
        "approval.decision",
        key="approve_implementation_plan",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/plan-approved"},
        idempotency_key="plan-approval",
    )

    assert result.status == "completed"
    assert result.result["plan_prompt_path"] == str(prompt)
    assert len(result.result["plan_prompt_sha256"]) == 64
    assert len(result.result["plan_rendered_prompt_sha256"]) == 64


def test_repo_change_plan_workflow_records_human_plan_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    db = tmp_path / "workflow.sqlite"
    approval_engine = WorkflowEngine(db)
    approval_engine.run_until_idle(
        repo_change_plan_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=None),
        workflow_id="wf_plan",
    )

    result = _drain_signal(
        approval_engine,
        "wf_plan",
        "approval.decision",
        key="approve_implementation_plan",
        payload={"action": "approve", "by": "skylar"},
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/plan-approved"},
        idempotency_key="plan-approval",
    )

    assert result.status == "completed"
    assert result.result["ready_for_implementation"] is True
    assert result.result["plan_workflow_id"] == "wf_plan"
    assert result.result["approval_source"]["message_url"] == "discord://thread/plan-approved"
    assert result.result["plan_artifact_sha256"]


def test_repo_change_plan_workflow_rejects_agent_plan_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(
        repo_change_plan_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=None),
        workflow_id="wf_plan",
    )

    with pytest.raises(ValueError, match="requires human approval source"):
        engine.signal(
            "wf_plan",
            "approval.decision",
            key="approve_implementation_plan",
            payload={"action": "approve", "by": "palmer"},
            source={"kind": "agent", "id": "palmer", "channel": "kanban", "event_id": "run-1"},
            idempotency_key="bad-plan-approval",
        )

    status = engine.workflow_status("wf_plan", recent_events=20)
    assert status["status"] == "waiting"
    assert status["waiting_on"] == "signal:approval.decision:approve_implementation_plan"
    assert not [event for event in status["events"] if event["type"] == "SignalReceived"]


def test_repo_pr_workflow_hard_requires_plan_approval_before_pr_work(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)

    result = WorkflowEngine(tmp_path / "workflow.sqlite").run_until_idle(
        repo_pr_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=None),
        workflow_id="wf_pr",
    )

    assert result.status == "failed"
    assert "requires approved implementation_plan" in (result.error or "")
    assert not (tmp_path / "pr-body.md").exists()


def test_repo_pr_workflow_rejects_fabricated_plan_without_durable_workflow(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (tmp_path / "implementation-plan.md").write_text("# fake plan\n")

    result = WorkflowEngine(tmp_path / "workflow.sqlite").run_until_idle(
        repo_pr_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=approved_plan(tmp_path)),
        workflow_id="wf_pr",
    )

    assert result.status == "failed"
    assert "requires completed implementation plan workflow" in (result.error or "")
    assert not (tmp_path / "pr-body.md").exists()


def test_repo_pr_workflow_rejects_missing_plan_artifact_after_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    implementation_plan = approve_plan_workflow(engine, repo, tmp_path)
    Path(implementation_plan["plan_artifact_path"]).unlink()

    result = engine.run_until_idle(
        repo_pr_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=implementation_plan),
        workflow_id="wf_pr",
    )

    assert result.status == "failed"
    assert "plan artifact does not exist" in (result.error or "")
    assert not (tmp_path / "pr-body.md").exists()


def test_repo_pr_workflow_rejects_tampered_plan_artifact_after_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    implementation_plan = approve_plan_workflow(engine, repo, tmp_path)
    Path(implementation_plan["plan_artifact_path"]).write_text("# changed after approval\n")

    result = engine.run_until_idle(
        repo_pr_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=implementation_plan),
        workflow_id="wf_pr",
    )

    assert result.status == "failed"
    assert "plan artifact hash does not match" in (result.error or "")
    assert not (tmp_path / "pr-body.md").exists()


def test_repo_pr_workflow_gathers_tests_writes_body_then_waits_for_landing_approval(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    implementation_plan = approve_plan_workflow(engine, repo, tmp_path)

    first = engine.run_until_idle(
        repo_pr_workflow,
        workflow_inputs(repo, tmp_path, implementation_plan=implementation_plan),
        workflow_id="wf_pr",
    )

    assert first.status == "waiting"
    assert first.waiting_on == "signal:approval.decision:approve_pr_landing"
    body = (tmp_path / "pr-body.md").read_text()
    assert "## Workflow-backed PR evidence" in body
    assert "Branch: `feat/pr-path`" in body
    assert "pytest -q" in body
    assert "## Implementation plan approval" in body
    assert "Plan workflow: `wf_plan`" in body
    assert f"Plan artifact SHA-256: `{implementation_plan['plan_artifact_sha256']}`" in body
    assert f"Plan prompt: `{implementation_plan['plan_prompt_path']}`" in body
    assert f"Plan prompt SHA-256: `{implementation_plan['plan_prompt_sha256']}`" in body
    assert "Approved by: skylar via discord discord://thread/plan-approved" in body
    pending_report = (tmp_path / "pr-status.md").read_text()
    assert "Landing approval: not requested" in pending_report
    assert "PR: create_pr disabled" in pending_report
    assert "Implementation plan: skylar via discord discord://thread/plan-approved" in pending_report
    assert f"Plan artifact SHA-256: `{implementation_plan['plan_artifact_sha256']}`" in pending_report
    assert f"Plan prompt: `{implementation_plan['plan_prompt_path']}`" in pending_report
    assert f"Plan prompt SHA-256: `{implementation_plan['plan_prompt_sha256']}`" in pending_report

    approval = _drain_signal(
        engine,
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
    engine = WorkflowEngine(db)
    implementation_plan = approve_plan_workflow(engine, repo, tmp_path)
    engine.run_until_idle(repo_pr_workflow, workflow_inputs(repo, tmp_path, implementation_plan=implementation_plan), workflow_id="wf_pr")

    with pytest.raises(ValueError, match="requires human approval source"):
        engine.signal(
            "wf_pr",
            "approval.decision",
            key="approve_pr_landing",
            payload={"action": "approve", "by": "palmer"},
            source={"kind": "agent", "id": "palmer", "channel": "kanban", "event_id": "run-1"},
            idempotency_key="bad-approval",
        )

    status = engine.workflow_status("wf_pr", recent_events=20)
    assert status["status"] == "waiting"
    assert status["waiting_on"] == "signal:approval.decision:approve_pr_landing"
    assert not [
        event
        for event in status["events"]
        if event["type"] == "SignalReceived" and event["payload"].get("key") == "approve_pr_landing"
    ]


def test_check_watcher_waits_for_github_to_report_new_branch_checks(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, *, cwd, env=None, timeout=300):
        calls.append(command)
        output = "pytest\tpass\t0\thttps://github.com/example/actions/runs/1"
        ok = True
        if command == ["gh", "pr", "checks"] and len([c for c in calls if c == command]) == 1:
            output = "no checks reported on the 'feat/pr-path' branch"
            ok = False
        if "--watch" in command and len([c for c in calls if "--watch" in c]) == 1:
            output = "no checks reported on the 'feat/pr-path' branch"
            ok = False
        return {"command": " ".join(command), "returncode": 0 if ok else 1, "ok": ok, "output": output}

    monkeypatch.setattr(pr_module, "_run", fake_run)

    step_body = getattr(pr_module.watch_pull_request_checks, "__step_body__")
    result = asyncio.run(
        step_body(
            None,
            {
                "repo_path": str(tmp_path),
                "watch_checks": True,
                "check_interval_seconds": 0,
                "check_appearance_attempts": 3,
            },
            {"opened": True},
        )
    )

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert result["final"]["output"].startswith("pytest")

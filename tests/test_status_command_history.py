import json

from hermes_workflows import WorkflowEngine, step, workflow
from hermes_workflows.cli import main as cli_main


@step
async def status_history_explode(text):
    raise ValueError("command-history-smoke-boom")


@workflow
async def status_history_workflow(inputs):
    return await status_history_explode(inputs["text"])


def test_status_command_history_is_opt_in_and_shows_failed_command_details(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        status_history_workflow,
        {"text": "x" * 200},
        workflow_id="wf_status_history",
    )

    assert result.status == "failed"
    default_status = engine.workflow_status("wf_status_history", recent_events=1)
    assert default_status["pending_commands"] == []
    assert "command_history" not in default_status

    status = engine.workflow_status(
        "wf_status_history",
        recent_events=1,
        command_history="failed",
        command_limit=5,
        command_payload_chars=80,
    )

    assert status["pending_commands"] == []
    assert status["command_history_mode"] == "failed"
    assert status["command_history_truncated"] is False
    assert len(status["command_history"]) == 1
    failed = status["command_history"][0]
    assert failed["key"] == "step:status_history_explode:0"
    assert failed["type"] == "run_step"
    assert failed["status"] == "failed"
    assert failed["claimed_by"] == "local-drain"
    assert failed["attempts"] == 1
    assert failed["last_error"] == {"type": "ValueError", "message": "command-history-smoke-boom"}
    assert isinstance(failed["created_at"], int)
    assert isinstance(failed["updated_at"], int)
    assert failed["payload_context"]["truncated"] is True
    assert failed["payload_context"]["limit"] == 80
    assert "payload" not in failed


def test_status_cli_accepts_commands_failed_flag(capsys, tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(status_history_workflow, {"text": "short"}, workflow_id="wf_status_history_cli")

    exit_code = cli_main([
        "status",
        "--db",
        str(db),
        "--id",
        "wf_status_history_cli",
        "--recent-events",
        "1",
        "--commands",
        "failed",
        "--command-limit",
        "5",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow_id"] == "wf_status_history_cli"
    assert payload["command_history_mode"] == "failed"
    assert [command["status"] for command in payload["command_history"]] == ["failed"]
    assert payload["command_history"][0]["last_error"]["type"] == "ValueError"

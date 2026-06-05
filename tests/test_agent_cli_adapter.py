from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hermes_workflows import AgentRunnerError, AgentStep, SubprocessAgentRunner, Workflow, WorkflowEngine, workflow
from hermes_workflows.agent_cli_adapter import collect_secret_values, redact_text


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
FAKE_AGENT = REPO_ROOT / "examples" / "runners" / "fake_json_cli_agent.py"
ADAPTER_MODULE = "hermes_workflows.agent_cli_adapter"


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) if not existing else f"{SRC_DIR}{os.pathsep}{existing}"
    env.update(extra or {})
    return env


def _request(**overrides: Any) -> dict[str, Any]:
    request = {
        "kind": "agent_step.runner_request.v1",
        "name": "summarize_item",
        "prompt": "Summarize {{item}} as JSON.",
        "prompt_sha256": "prompt-sha",
        "rendered_prompt": "Summarize alpha as JSON.",
        "rendered_prompt_sha256": "rendered-sha",
        "variables": {"item": "alpha"},
        "variables_sha256": "variables-sha",
        "returns": "json",
        "workflow_id": "wf_summary",
        "step_key": "step:agent_step:0",
    }
    request.update(overrides)
    return request


def _sha256_json(value: Any) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _adapter_command(*agent_args: str, timeout_seconds: float = 5.0, max_stdout: int = 65_536, max_stderr: int = 4096) -> list[str]:
    command = [
        sys.executable,
        "-m",
        ADAPTER_MODULE,
        "--agent-command",
        sys.executable,
        "--timeout-seconds",
        str(timeout_seconds),
        "--max-agent-stdout-bytes",
        str(max_stdout),
        "--max-agent-stderr-bytes",
        str(max_stderr),
        "--agent-arg",
        str(FAKE_AGENT),
    ]
    for arg in agent_args:
        command.extend(["--agent-arg", arg])
    return command


def _runner(*agent_args: str, env: dict[str, str] | None = None, timeout_seconds: float = 10.0, max_stdout: int = 65_536) -> SubprocessAgentRunner:
    return SubprocessAgentRunner(
        _adapter_command(*agent_args, max_stdout=max_stdout),
        timeout_seconds=timeout_seconds,
        env=_subprocess_env(env),
        max_stdout_bytes=65_536,
    )


def _run_adapter_direct(request: dict[str, Any] | str, *agent_args: str, timeout_seconds: float = 5.0, max_stdout: int = 65_536) -> subprocess.CompletedProcess[str]:
    stdin = request if isinstance(request, str) else json.dumps(request)
    return subprocess.run(
        _adapter_command(*agent_args, timeout_seconds=timeout_seconds, max_stdout=max_stdout),
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        timeout=10,
        check=False,
    )


def test_agent_cli_adapter_turns_agentstep_request_into_provider_prompt_and_records_provenance():
    secret = "env-secret-token-123"
    request = _request(prompt="Summarize {{item}} with {{token}}.", rendered_prompt="Summarize alpha with public-value.")
    response = _runner(env={"API_TOKEN": secret})(request)

    assert response["output"] == {"kind": "fake.agent_response.v1", "prompt_seen": True}
    provenance = response["provenance"]
    assert provenance["runner"] == "hermes_workflows.agent_cli_adapter"
    assert provenance["adapter_version"] == 1
    assert provenance["request_kind"] == "agent_step.runner_request.v1"
    assert provenance["request_name"] == "summarize_item"
    assert provenance["request_sha256"] == _sha256_json(request)
    assert provenance["rendered_prompt_sha256"] == "rendered-sha"
    assert provenance["agent_command"]["argv0"] == Path(sys.executable).name
    assert provenance["agent_command"]["argv"][0] == Path(sys.executable).name
    assert provenance["agent_command"]["argv"][1] == str(FAKE_AGENT)
    assert provenance["provider_provenance"]["model"] == "fake-1"
    assert provenance["exit_code"] == 0
    assert isinstance(provenance["duration_ms"], int)

    serialized = json.dumps(response, sort_keys=True)
    assert "env" not in provenance
    assert secret not in serialized
    assert request["prompt"] not in serialized
    assert request["rendered_prompt"] not in serialized


def test_agent_cli_adapter_rejects_invalid_runner_request_json():
    completed = _run_adapter_direct("{not-json")

    assert completed.returncode != 0
    assert completed.stdout == ""
    error = json.loads(completed.stderr)
    assert error["kind"] == "agent_cli_adapter.error.v1"
    assert error["error"] == "invalid_runner_request_json"


def test_agent_cli_adapter_fails_closed_on_provider_invalid_json():
    with pytest.raises(AgentRunnerError) as excinfo:
        _runner()( _request(rendered_prompt="FAIL_INVALID_JSON") )

    assert "exited with code" in str(excinfo.value)
    assert "provider_invalid_json" in excinfo.value.details["stderr_tail"]
    assert "env-secret" not in json.dumps(excinfo.value.details)


def test_agent_cli_adapter_fails_closed_on_provider_nonzero_exit():
    with pytest.raises(AgentRunnerError) as excinfo:
        _runner("--exit-code", "7", "--stderr", "boom stderr")( _request() )

    assert excinfo.value.details["exit_code"] != 0
    assert "provider_nonzero_exit" in excinfo.value.details["stderr_tail"]
    assert "boom stderr" in excinfo.value.details["stderr_tail"]


def test_agent_cli_adapter_provider_timeout_is_redacted():
    completed = _run_adapter_direct(
        _request(),
        "--sleep-seconds",
        "5",
        "--stderr",
        "TOKEN=timeout-secret-token",
        timeout_seconds=0.1,
    )

    assert completed.returncode != 0
    details = completed.stderr
    assert "provider_timeout" in details
    assert "timeout-secret-token" not in details
    assert "[REDACTED]" in details


def test_agent_cli_adapter_redacts_secret_bearing_argv_and_provider_output():
    completed = _run_adapter_direct(
        _request(),
        "--api-key",
        "sk-test-secret",
        "--stdout",
        "TOKEN=sk-output-secret",
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    diagnostic = completed.stderr
    assert "provider_invalid_json" in diagnostic
    assert "[REDACTED]" in diagnostic
    assert "sk-test-secret" not in diagnostic
    assert "sk-output-secret" not in diagnostic
    error = json.loads(diagnostic)
    argv = error["agent_command"]["argv"]
    assert "--api-key" in argv
    assert "[REDACTED]" in argv


def test_agent_cli_adapter_success_redacts_secret_bearing_argv_in_provenance():
    response = _runner("--api-key", "sk-test-secret")( _request() )

    serialized = json.dumps(response, sort_keys=True)
    assert "sk-test-secret" not in serialized
    assert "[REDACTED]" in serialized
    assert response["provenance"]["agent_command"]["argv"][-1] == "[REDACTED]"


def test_agent_cli_adapter_redacts_raw_prompt_text_from_provider_provenance():
    request = _request(
        prompt="Summarize {{secret_business_item}} with context.",
        rendered_prompt="Summarize unreleased business plan with context.",
    )

    response = _runner("--provenance-note", request["rendered_prompt"])(request)

    serialized = json.dumps(response, sort_keys=True)
    assert request["prompt"] not in serialized
    assert request["rendered_prompt"] not in serialized
    assert "[REDACTED]" in serialized


def test_agent_cli_adapter_drops_provider_transcript_provenance():
    response = _runner(
        "--provenance-transcript",
        "raw provider transcript should not persist",
        "--provenance-message",
        "raw provider message should not persist",
    )(_request())

    provider_provenance = response["provenance"]["provider_provenance"]
    serialized = json.dumps(provider_provenance, sort_keys=True)
    assert provider_provenance["model"] == "fake-1"
    assert "transcript" not in provider_provenance
    assert "messages" not in provider_provenance
    assert "raw provider transcript" not in serialized
    assert "raw provider message" not in serialized


def test_agent_cli_adapter_redacts_raw_prompt_text_from_provider_error_tails():
    request = _request(rendered_prompt="Summarize private acquisition memo.")
    completed = _run_adapter_direct(request, "--stdout", request["rendered_prompt"])

    assert completed.returncode != 0
    diagnostic = completed.stderr
    assert "provider_invalid_json" in diagnostic
    assert request["rendered_prompt"] not in diagnostic
    assert "[REDACTED]" in diagnostic


def test_agent_cli_adapter_does_not_collect_username_env_value_as_secret(monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "skylar")
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "dashboard-password-secret")
    monkeypatch.setenv("SERVICE_USER_TOKEN", "service-user-token-secret")

    secrets = collect_secret_values([])

    assert "skylar" not in secrets
    assert "dashboard-password-secret" in secrets
    assert "service-user-token-secret" in secrets
    assert redact_text("/Users/skylarpayne/.hermes", secrets) == "/Users/skylarpayne/.hermes"
    assert redact_text("dashboard-password-secret service-user-token-secret", secrets) == "[REDACTED] [REDACTED]"


def test_agent_cli_adapter_enforces_provider_stdout_cap_while_reading():
    completed = _run_adapter_direct(
        _request(),
        "--huge-stdout-bytes",
        "1000000",
        max_stdout=64,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert len(completed.stderr) < 6000
    diagnostic = json.loads(completed.stderr)
    assert diagnostic["error"] == "provider_stdout_exceeded"
    assert diagnostic["stdout_bytes"] == 65
    assert len(diagnostic["stdout_tail"]) <= 65


def test_agent_cli_adapter_timeout_covers_prompt_write_to_nonreading_provider():
    large_request = _request(rendered_prompt="x" * 2_000_000)
    command = [
        sys.executable,
        "-m",
        ADAPTER_MODULE,
        "--agent-command",
        sys.executable,
        "--agent-arg",
        "-c",
        "--agent-arg",
        "import time; time.sleep(5)",
        "--timeout-seconds",
        "0.2",
    ]

    completed = subprocess.run(
        command,
        input=json.dumps(large_request),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        timeout=2,
        check=False,
    )

    assert completed.returncode != 0
    diagnostic = json.loads(completed.stderr)
    assert diagnostic["error"] == "provider_timeout"


@workflow
async def adapter_generated_workflow_pipeline(ctx, inputs):
    processor = await AgentStep(
        "build_processor",
        prompt="WORKFLOW_OUTPUT for {{kind}} items.",
        variables={"kind": inputs["kind"]},
        returns=Workflow,
    )(ctx)
    return await processor(ctx, inputs["item"], key=inputs["item"]["id"])


def test_agent_cli_adapter_generated_workflow_still_waits_for_approval(tmp_path):
    engine = WorkflowEngine(
        tmp_path / "workflow.sqlite",
        agent_runner=_runner(),
    )

    first = engine.run_until_idle(
        adapter_generated_workflow_pipeline,
        {"kind": "catalog", "item": {"id": "a", "label": "alpha"}},
        workflow_id="wf_generated_adapter",
    )

    assert first.status == "waiting"
    approvals = engine.workflow_status("wf_generated_adapter")["approvals"]
    assert len(approvals) == 1
    approval = approvals[0]
    assert first.waiting_on == f"signal:approval.decision:{approval['key']}"
    assert approval["key"].startswith("generated-workflow:")
    provenance = approval["artifact"]["runner_provenance"]
    assert provenance["runner"] == "hermes_workflows.agent_cli_adapter"
    assert provenance["adapter_version"] == 1
    assert provenance["provider_provenance"]["model"] == "fake-1"
    assert approval["artifact"]["symbol"] == "process_item"
    assert [event for event in engine.events("wf_generated_adapter") if event["type"] == "ChildWorkflowRequested"] == []
    assert f"hermes_generated_workflows.{approval['artifact']['source_sha256']}" not in sys.modules


@pytest.mark.skipif(
    os.environ.get("HERMES_WORKFLOWS_REAL_AGENT_ADAPTER") != "1",
    reason="real agent adapter smoke is opt-in",
)
def test_real_agent_cli_adapter_smoke(tmp_path):
    command_env = os.environ.get("HERMES_WORKFLOWS_AGENT_COMMAND")
    if not command_env:
        pytest.skip("HERMES_WORKFLOWS_AGENT_COMMAND must be supplied for real smoke")
    command = shlex.split(command_env)
    if not command:
        pytest.skip("HERMES_WORKFLOWS_AGENT_COMMAND must not be empty")

    adapter = [
        sys.executable,
        "-m",
        ADAPTER_MODULE,
        "--agent-command",
        command[0],
    ]
    for arg in command[1:]:
        adapter.extend(["--agent-arg", arg])
    runner = SubprocessAgentRunner(adapter, timeout_seconds=120, max_stdout_bytes=1_000_000, env=_subprocess_env())
    response = runner(
        _request(
            name="real_smoke_answer",
            prompt="Return exactly {\"output\": {\"answer\": 42}, \"provenance\": {\"model\": \"real-smoke\"}} as JSON.",
            rendered_prompt="Return exactly {\"output\": {\"answer\": 42}, \"provenance\": {\"model\": \"real-smoke\"}} as JSON.",
            workflow_id=f"wf_real_smoke_{tmp_path.name}",
        )
    )
    assert response["output"] == {"answer": 42}
    assert response["provenance"]["runner"] == "hermes_workflows.agent_cli_adapter"

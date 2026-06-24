import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_workflows.cli import agent_runner_from_args, normalize_agent_value_options


WORKFLOW_MODULE = '''
from hermes_workflows import approve, step, wait_for, workflow

@step
async def make_plan(inputs):
    return {"summary": f"Plan for {inputs['destination']}"}

@workflow
async def demo_workflow(inputs):
    plan = await make_plan(inputs)
    decision = await approve(
        "Approve plan?",
        key="approve_plan",
        artifact=plan,
        approver="human:skylar",
    )
    return {"plan": plan, "approved_by": decision["by"]}
'''


CHILD_WORKFLOW_MODULE = '''
from pathlib import Path
from hermes_workflows import Workflow, workflow

WAITING_SOURCE = """
from hermes_workflows import wait_for, workflow

@workflow
async def waiting_child(item):
    payload = await wait_for(\"dynamic.ready\", key=item[\"id\"])
    return {\"payload\": payload}
"""

CHILD = Workflow.from_source(
    WAITING_SOURCE,
    symbol="waiting_child",
    base_dir=Path(__file__).parent,
)

@workflow
async def parent_workflow(inputs):
    return await CHILD(inputs["item"], key=inputs["item"]["id"])
'''


DYNAMIC_CHILD_WORKFLOW_MODULE = '''
from hermes_workflows import agent, Workflow, workflow

WAITING_SOURCE = """
from hermes_workflows import wait_for, workflow

@workflow
async def waiting_child(item):
    payload = await wait_for(\"dynamic.ready\", key=item[\"id\"])
    return {\"payload\": payload}
"""

@workflow
async def generated_parent_workflow(inputs):
    child = await agent(
        "build_waiting_child",
        prompt="Build a child workflow that waits for a signal.",
        input={"purpose": "wait for a signal"},
        returns=Workflow,
        mock_output={"source": WAITING_SOURCE, "symbol": "waiting_child"},
    )
    return await child(inputs["item"], key=inputs["item"]["id"])
'''

AGENT_RUNNER_WORKFLOW_MODULE = '''
from hermes_workflows import agent, workflow

@workflow
async def agent_runner_workflow(inputs):
    packet = await agent(
        "writer",
        prompt="Write a short packet.",
        input={"topic": inputs["topic"]},
        returns=dict,
        key="write_packet",
    )
    return {"packet": packet}
'''

AGENT_PROVIDER_MODULE = '''
import json
import sys

prompt = sys.stdin.read()
if "agent.runner_request.v1" not in prompt:
    raise SystemExit("missing runner request")
print(json.dumps({
    "output": {"summary": "agent ran from provider", "saw_request": "agent.runner_request.v1" in prompt},
    "provenance": {"model": "fake-provider"},
}))
'''

AGENT_MODEL_RUNNER_WORKFLOW_MODULE = '''
from hermes_workflows import agent, workflow

@workflow
async def agent_model_runner_workflow(inputs):
    packet = await agent(
        "writer",
        prompt="Write a short packet.",
        input={"topic": inputs["topic"]},
        returns=dict,
        key="write_packet",
        model=inputs["model"],
    )
    return {"packet": packet}
'''

AGENT_MODEL_PROVIDER_MODULE = '''
import json
import sys

sys.stdin.read()
print(json.dumps({
    "output": {"argv": sys.argv[1:]},
    "provenance": {"model": "fake-provider"},
}))
'''

HERMES_SUBAGENT_PROVIDER_MODULE = '''
import json
import sys

argv = sys.argv[1:]
prompt = ""
if "--oneshot" in argv:
    prompt = argv[argv.index("--oneshot") + 1]
elif "-z" in argv:
    prompt = argv[argv.index("-z") + 1]
print(json.dumps({
    "output": {"argv": argv, "prompt_has_runner_request": "agent.runner_request.v1" in prompt},
    "provenance": {"model": argv[argv.index("--model") + 1] if "--model" in argv else None},
}))
'''


def run_cli(tmp_path, *args, env_extra=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}:{tmp_path}:{env.get('PYTHONPATH', '')}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_workflows", *args],
        cwd=Path.cwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def run_worker(tmp_path, workflow_ref, db, workflow_id, *, max_commands=None, once=False):
    args = [
        "worker",
        workflow_ref,
        "--db",
        str(db),
        "--id",
        workflow_id,
        "--worker-id",
        f"test-worker-{workflow_id}",
    ]
    if once:
        args.append("--once")
    if max_commands is not None:
        args.extend(["--max-commands", str(max_commands)])
    return json.loads(run_cli(tmp_path, *args).stdout)


def test_agent_runner_from_args_reads_agent_model_args_from_env(monkeypatch):
    monkeypatch.setenv("HERMES_WORKFLOWS_AGENT_COMMAND", "provider")
    monkeypatch.delenv("HERMES_WORKFLOWS_AGENT_ARGS_JSON", raising=False)
    monkeypatch.setenv("HERMES_WORKFLOWS_AGENT_MODEL_ARGS_JSON", json.dumps(["--provider-model", "{model}"]))
    args = argparse.Namespace(
        agent_command=None,
        agent_arg=[],
        agent_model_arg=[],
        agent_request_stdin=None,
        agent_timeout_seconds=120.0,
        max_agent_stdout_bytes=1_000_000,
        max_agent_stderr_bytes=4096,
    )

    runner = agent_runner_from_args(args)

    assert runner is not None
    assert runner.argv == ["provider"]
    assert runner.model_arg_templates == ["--provider-model", "{model}"]


def test_agent_runner_from_args_prefers_cli_model_args_over_env(monkeypatch):
    monkeypatch.setenv("HERMES_WORKFLOWS_AGENT_MODEL_ARGS_JSON", json.dumps(["--env-model", "{model}"]))
    args = argparse.Namespace(
        agent_command="provider",
        agent_arg=["--base"],
        agent_model_arg=["--cli-model={model}"],
        agent_request_stdin=None,
        agent_timeout_seconds=120.0,
        max_agent_stdout_bytes=1_000_000,
        max_agent_stderr_bytes=4096,
    )

    runner = agent_runner_from_args(args)

    assert runner is not None
    assert runner.argv == ["provider", "--base"]
    assert runner.model_arg_templates == ["--cli-model={model}"]


def test_cli_normalizes_option_like_agent_model_arg_values():
    assert normalize_agent_value_options(
        ["worker", "wf:run", "--agent-model-arg", "--model", "--agent-model-arg", "{model}"]
    ) == ["worker", "wf:run", "--agent-model-arg=--model", "--agent-model-arg={model}"]


def test_worker_cli_respects_agent_model_arg_templates(tmp_path):
    (tmp_path / "agent_model_runner_wf.py").write_text(AGENT_MODEL_RUNNER_WORKFLOW_MODULE)
    provider = tmp_path / "agent_model_provider.py"
    provider.write_text(AGENT_MODEL_PROVIDER_MODULE)
    db = tmp_path / "workflow.sqlite"

    started = json.loads(
        run_cli(
            tmp_path,
            "run",
            "agent_model_runner_wf:agent_model_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_agent_model_runner_cli",
            "--input-json",
            json.dumps({"topic": "package worker", "model": "hermes-test-model"}),
        ).stdout
    )
    assert started["status"] == "running"

    completed = json.loads(
        run_cli(
            tmp_path,
            "worker",
            "agent_model_runner_wf:agent_model_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_agent_model_runner_cli",
            "--worker-id",
            "test-agent-model-runner-worker",
            "--max-commands",
            "10",
            "--agent-command",
            sys.executable,
            "--agent-arg",
            str(provider),
            "--agent-model-arg",
            "--provider-model={model}",
        ).stdout
    )

    assert completed["status"] == "completed"
    assert completed["result"]["packet"]["argv"] == ["--provider-model=hermes-test-model"]


def test_worker_cli_respects_agent_model_arg_env_templates_end_to_end(tmp_path):
    (tmp_path / "agent_model_runner_wf.py").write_text(AGENT_MODEL_RUNNER_WORKFLOW_MODULE)
    provider = tmp_path / "agent_model_provider.py"
    provider.write_text(AGENT_MODEL_PROVIDER_MODULE)
    db = tmp_path / "workflow.sqlite"

    started = json.loads(
        run_cli(
            tmp_path,
            "run",
            "agent_model_runner_wf:agent_model_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_agent_model_runner_env",
            "--input-json",
            json.dumps({"topic": "package worker", "model": "hermes-env-model"}),
        ).stdout
    )
    assert started["status"] == "running"

    completed = json.loads(
        run_cli(
            tmp_path,
            "worker",
            "agent_model_runner_wf:agent_model_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_agent_model_runner_env",
            "--worker-id",
            "test-agent-model-env-worker",
            "--max-commands",
            "10",
            env_extra={
                "HERMES_WORKFLOWS_AGENT_COMMAND": sys.executable,
                "HERMES_WORKFLOWS_AGENT_ARGS_JSON": json.dumps([str(provider)]),
                "HERMES_WORKFLOWS_AGENT_MODEL_ARGS_JSON": json.dumps(["--provider-model", "{model}"]),
            },
        ).stdout
    )

    assert completed["status"] == "completed"
    assert completed["result"]["packet"]["argv"] == ["--provider-model", "hermes-env-model"]


def test_worker_cli_existing_agent_adapter_passes_model_to_hermes_oneshot_cli(tmp_path):
    (tmp_path / "agent_model_runner_wf.py").write_text(AGENT_MODEL_RUNNER_WORKFLOW_MODULE)
    fake_hermes = tmp_path / "fake_hermes.py"
    fake_hermes.write_text(HERMES_SUBAGENT_PROVIDER_MODULE)
    db = tmp_path / "workflow.sqlite"

    started = json.loads(
        run_cli(
            tmp_path,
            "run",
            "agent_model_runner_wf:agent_model_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_existing_agent_adapter_model_runner",
            "--input-json",
            json.dumps({"topic": "package worker", "model": "hermes-subagent-model"}),
        ).stdout
    )
    assert started["status"] == "running"

    completed = json.loads(
        run_cli(
            tmp_path,
            "worker",
            "agent_model_runner_wf:agent_model_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_existing_agent_adapter_model_runner",
            "--worker-id",
            "test-existing-agent-adapter-model-worker",
            "--max-commands",
            "10",
            env_extra={
                "HERMES_WORKFLOWS_AGENT_COMMAND": sys.executable,
                "HERMES_WORKFLOWS_AGENT_REQUEST_STDIN": "json",
                "HERMES_WORKFLOWS_AGENT_ARGS_JSON": json.dumps(
                    [
                        "-m",
                        "hermes_workflows.agent_cli_adapter",
                        "--agent-command",
                        sys.executable,
                        "--agent-arg",
                        str(fake_hermes),
                        "--agent-model-arg",
                        "--model",
                        "--agent-model-arg",
                        "{model}",
                        "--agent-prompt-arg",
                        "--oneshot",
                    ]
                ),
            },
        ).stdout
    )

    argv = completed["result"]["packet"]["argv"]
    assert completed["status"] == "completed"
    assert argv[:2] == ["--model", "hermes-subagent-model"]
    assert "--oneshot" in argv
    assert completed["result"]["packet"]["prompt_has_runner_request"] is True


def test_worker_cli_executes_agent_jobs_with_configured_provider_command(tmp_path):
    (tmp_path / "agent_runner_wf.py").write_text(AGENT_RUNNER_WORKFLOW_MODULE)
    provider = tmp_path / "agent_provider.py"
    provider.write_text(AGENT_PROVIDER_MODULE)
    db = tmp_path / "workflow.sqlite"

    started = json.loads(
        run_cli(
            tmp_path,
            "run",
            "agent_runner_wf:agent_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_agent_runner_cli",
            "--input-json",
            json.dumps({"topic": "package worker"}),
        ).stdout
    )

    assert started["status"] == "running"

    waiting = json.loads(
        run_cli(
            tmp_path,
            "worker",
            "agent_runner_wf:agent_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_agent_runner_cli",
            "--worker-id",
            "test-agent-runner-without-provider",
            "--once",
        ).stdout
    )
    assert waiting["status"] == "waiting"
    assert waiting["waiting_on"] == "signal:agent.completed:write_packet"

    completed = json.loads(
        run_cli(
            tmp_path,
            "worker",
            "agent_runner_wf:agent_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_agent_runner_cli",
            "--worker-id",
            "test-agent-runner-worker",
            "--max-commands",
            "10",
            "--agent-command",
            sys.executable,
            "--agent-arg",
            str(provider),
        ).stdout
    )

    assert completed["status"] == "completed"
    assert completed["result"]["packet"] == {"summary": "agent ran from provider", "saw_request": True}

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    outbox = [dict(row) for row in con.execute("SELECT type, key, status FROM workflow_commands_outbox ORDER BY id")]
    assert all(row["status"] == "completed" for row in outbox)
    assert not any(row["type"] == "external_agent" and row["status"] == "pending" for row in outbox)


def test_worker_config_mode_executes_agent_jobs_with_configured_provider_command(tmp_path):
    (tmp_path / "agent_runner_wf.py").write_text(AGENT_RUNNER_WORKFLOW_MODULE)
    provider = tmp_path / "agent_provider.py"
    provider.write_text(AGENT_PROVIDER_MODULE)
    db = tmp_path / "workflow.sqlite"
    registry = tmp_path / "workflows.registry.json"
    registry.write_text(
        json.dumps(
            {
                "dbs": {"service": str(db)},
                "workflows": {
                    "agent-worker": {"workflow_ref": "agent_runner_wf:agent_runner_workflow", "db": "service"}
                },
            }
        )
    )

    started = json.loads(
        run_cli(
            tmp_path,
            "run",
            "agent_runner_wf:agent_runner_workflow",
            "--db",
            str(db),
            "--id",
            "wf_agent_runner_service",
            "--input-json",
            json.dumps({"topic": "resident worker"}),
        ).stdout
    )
    assert started["status"] == "running"

    service = json.loads(
        run_cli(
            tmp_path,
            "worker",
            "--config",
            str(registry),
            "--db",
            "service",
            "--worker-id",
            "test-agent-runner-service",
            "--max-commands",
            "10",
            "--idle-exit-after",
            "0.1",
            "--agent-command",
            sys.executable,
            "--agent-arg",
            str(provider),
        ).stdout
    )

    assert service["errors"] == []
    assert service["executed"] >= 2
    assert service["executions"][-1]["status"] == "completed"

    status = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_agent_runner_service").stdout)
    assert status["status"] == "completed"
    assert status["result"]["packet"] == {"summary": "agent ran from provider", "saw_request": True}


def test_cli_help_exposes_one_public_worker_command(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "hermes_workflows", "--help"],
        cwd=Path.cwd(),
        env={**os.environ, "PYTHONPATH": f"{Path.cwd() / 'src'}:{tmp_path}:{os.environ.get('PYTHONPATH', '')}"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert "worker" in result.stdout
    assert "worker-service" not in result.stdout


def start_to_waiting(tmp_path, workflow_ref, db, workflow_id, input_json, *, max_commands=3):
    started = json.loads(
        run_cli(
            tmp_path,
            "run",
            workflow_ref,
            "--db",
            str(db),
            "--id",
            workflow_id,
            "--input-json",
            input_json,
        ).stdout
    )
    assert started["status"] == "running"
    assert started["waiting_on"] is None
    return run_worker(tmp_path, workflow_ref, db, workflow_id, max_commands=max_commands)


def test_cli_reconciles_waiting_child_workflow_across_processes(tmp_path):
    (tmp_path / "child_wf.py").write_text(CHILD_WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    run_payload = start_to_waiting(
        tmp_path,
        "child_wf:parent_workflow",
        db,
        "wf_cli_child",
        '{"item":{"id":"cli-child"}}',
        max_commands=2,
    )
    assert run_payload["status"] == "waiting"
    assert run_payload["waiting_on"].startswith("child:")

    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_cli_child").stdout)
    child_requested = [event for event in status_payload["events"] if event["type"] == "ChildWorkflowRequested"][0]
    child_key = child_requested["key"]
    child_id = child_requested["payload"]["child_workflow_id"]
    assert status_payload["child_workflows"] == [
        {
            "key": child_key,
            "child_workflow_id": child_id,
            "status": "waiting",
            "waiting_on": "signal:dynamic.ready:cli-child",
            "diagnostic_label": "child_workflow_waiting",
            "diagnostic_message": "Parent is waiting on child workflow output.",
        }
    ]

    child_signal_payload = json.loads(
        run_cli(
            tmp_path,
            "signal",
            "child_wf:parent_workflow",
            "--db",
            str(db),
            "--id",
            child_id,
            "--type",
            "dynamic.ready",
            "--key",
            "cli-child",
            "--payload-json",
            '{"ok":true}',
        ).stdout
    )
    assert child_signal_payload["status"] == "running"
    child_completed = run_worker(tmp_path, "child_wf:parent_workflow", db, child_id, max_commands=1)
    assert child_completed["status"] == "completed"
    pre_reconcile_status = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_cli_child").stdout)
    assert pre_reconcile_status["child_workflows"] == [
        {
            "key": child_key,
            "child_workflow_id": child_id,
            "status": "completed",
            "waiting_on": None,
            "diagnostic_label": "child_workflow_terminal_unreconciled",
            "diagnostic_message": "Child workflow is terminal; parent has not reconciled it yet.",
        }
    ]

    reconcile_payload = json.loads(
        run_cli(
            tmp_path,
            "reconcile-children",
            "child_wf:parent_workflow",
            "--db",
            str(db),
            "--id",
            "wf_cli_child",
        ).stdout
    )
    assert reconcile_payload["status"] == "running"
    parent_completed = run_worker(tmp_path, "child_wf:parent_workflow", db, "wf_cli_child", max_commands=1)
    assert parent_completed == {
        "workflow_id": "wf_cli_child",
        "status": "completed",
        "waiting_on": None,
        "result": {"payload": {"ok": True}},
        "error": None,
    }

    reconcile_one_payload = json.loads(
        run_cli(
            tmp_path,
            "reconcile-child",
            "child_wf:parent_workflow",
            "--db",
            str(db),
            "--id",
            "wf_cli_child",
            "--child-key",
            child_key,
        ).stdout
    )
    assert reconcile_one_payload == parent_completed


def test_cli_signal_can_resume_generated_child_loaded_from_parent_history(tmp_path):
    (tmp_path / "dynamic_child_wf.py").write_text(DYNAMIC_CHILD_WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    run_payload = start_to_waiting(
        tmp_path,
        "dynamic_child_wf:generated_parent_workflow",
        db,
        "wf_cli_generated_child",
        '{"item":{"id":"generated-cli-child"}}',
        max_commands=5,
    )
    assert run_payload["status"] == "waiting"

    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_cli_generated_child").stdout)
    child_requested = [event for event in status_payload["events"] if event["type"] == "ChildWorkflowRequested"][0]

    child_signal_payload = json.loads(
        run_cli(
            tmp_path,
            "signal",
            "dynamic_child_wf:generated_parent_workflow",
            "--db",
            str(db),
            "--id",
            child_requested["payload"]["child_workflow_id"],
            "--type",
            "dynamic.ready",
            "--key",
            "generated-cli-child",
            "--payload-json",
            '{"ok":true}',
        ).stdout
    )
    assert child_signal_payload["status"] == "running"
    child_completed = run_worker(
        tmp_path,
        "dynamic_child_wf:generated_parent_workflow",
        db,
        child_requested["payload"]["child_workflow_id"],
        max_commands=1,
    )
    assert child_completed["status"] == "completed"

    reconcile_payload = json.loads(
        run_cli(
            tmp_path,
            "reconcile-child",
            "dynamic_child_wf:generated_parent_workflow",
            "--db",
            str(db),
            "--id",
            "wf_cli_generated_child",
            "--child-key",
            child_requested["key"],
        ).stdout
    )
    assert reconcile_payload["status"] == "running"
    parent_completed = run_worker(tmp_path, "dynamic_child_wf:generated_parent_workflow", db, "wf_cli_generated_child", max_commands=1)
    assert parent_completed["status"] == "completed"
    assert parent_completed["result"] == {"payload": {"ok": True}}


def test_cli_can_run_and_signal_workflow_across_processes(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    run_payload = start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_cli",
        '{"destination":"NYC"}',
    )
    assert run_payload == {
        "workflow_id": "wf_cli",
        "status": "waiting",
        "waiting_on": "signal:approval.decision:approve_plan",
        "result": None,
        "error": None,
    }

    signal_result = run_cli(
        tmp_path,
        "signal",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli",
        "--type",
        "approval.decision",
        "--key",
        "approve_plan",
        "--payload-json",
        '{"action":"approve","by":"skylar"}',
        "--source-json",
        '{"kind":"human","id":"skylar","channel":"discord","message_url":"discord://thread/1/message/2"}',
        "--idempotency-key",
        "cli-approval-1",
    )
    signal_payload = json.loads(signal_result.stdout)
    assert signal_payload["status"] == "running"
    completed_payload = run_worker(tmp_path, "demo_wf:demo_workflow", db, "wf_cli", max_commands=1)
    assert completed_payload == {
        "workflow_id": "wf_cli",
        "status": "completed",
        "waiting_on": None,
        "result": {"plan": {"summary": "Plan for NYC"}, "approved_by": "skylar"},
        "error": None,
    }


def test_cli_status_and_list_expose_inspectable_workflow_state(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_cli",
        '{"destination":"NYC"}',
    )

    status_result = run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_cli")
    status_payload = json.loads(status_result.stdout)
    assert status_payload["workflow_id"] == "wf_cli"
    assert status_payload["workflow_name"] == "demo_workflow"
    assert status_payload["status"] == "waiting"
    assert status_payload["waiting_on"] == "signal:approval.decision:approve_plan"
    assert status_payload["event_count"] >= 1
    assert [command["key"] for command in status_payload["pending_commands"]] == ["approval:approve_plan"]
    assert status_payload["events"][-1]["type"] == "WaitRequested"

    list_result = run_cli(tmp_path, "list", "--db", str(db))
    list_payload = json.loads(list_result.stdout)
    assert list_payload == {
        "workflows": [
            {
                "workflow_id": "wf_cli",
                "workflow_name": "demo_workflow",
                "workflow_ref": "demo_wf:demo_workflow",
                "status": "waiting",
                "waiting_on": "signal:approval.decision:approve_plan",
            }
        ]
    }


def test_cli_events_outbox_list_filter_and_approval_summary(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_waiting",
        '{"destination":"NYC"}',
    )
    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_done",
        '{"destination":"LA"}',
    )
    run_cli(
        tmp_path,
        "signal",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_done",
        "--type",
        "approval.decision",
        "--key",
        "approve_plan",
        "--payload-json",
        '{"action":"approve","by":"skylar"}',
        "--source-json",
        '{"kind":"human","id":"skylar","channel":"discord","message_url":"discord://thread/1/message/3"}',
    )
    run_worker(tmp_path, "demo_wf:demo_workflow", db, "wf_done", max_commands=1)

    list_result = run_cli(tmp_path, "list", "--db", str(db), "--status", "waiting")
    list_payload = json.loads(list_result.stdout)
    assert [workflow["workflow_id"] for workflow in list_payload["workflows"]] == ["wf_waiting"]

    events_result = run_cli(tmp_path, "events", "--db", str(db), "--id", "wf_waiting", "--limit", "1")
    events_payload = json.loads(events_result.stdout)
    assert [event["type"] for event in events_payload["events"]] == ["WaitRequested"]
    assert events_payload["events"][0]["key"] == "wait:approval.decision:approve_plan"

    outbox_result = run_cli(tmp_path, "outbox", "--db", str(db), "--id", "wf_waiting", "--status", "pending")
    outbox_payload = json.loads(outbox_result.stdout)
    assert [command["key"] for command in outbox_payload["commands"]] == ["approval:approve_plan"]
    assert outbox_payload["commands"][0]["type"] == "notify_approval"
    assert outbox_payload["commands"][0]["status"] == "pending"
    assert outbox_payload["commands"][0]["attempts"] == 0
    assert outbox_payload["commands"][0]["payload"]["prompt"] == "Approve plan?"

    all_outbox_result = run_cli(tmp_path, "outbox", "--db", str(db), "--status", "pending")
    all_outbox_payload = json.loads(all_outbox_result.stdout)
    assert {command["workflow_id"] for command in all_outbox_payload["commands"]} == {"wf_waiting"}

    status_result = run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_waiting")
    status_payload = json.loads(status_result.stdout)
    assert status_payload["approvals"] == [
        {
            "key": "approve_plan",
            "status": "waiting",
            "approver": "human:skylar",
            "prompt": "Approve plan?",
            "artifact": {"summary": "Plan for NYC"},
            "allowed": ["approve", "reject"],
            "schema": None,
            "authority": [],
            "timeout": None,
            "requested_seq": 8,
            "decision": None,
            "source": None,
        }
    ]

    done_status_result = run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_done")
    done_status_payload = json.loads(done_status_result.stdout)
    assert done_status_payload["pending_commands"] == []
    assert done_status_payload["approvals"] == [
        {
            "key": "approve_plan",
            "status": "approve",
            "approver": "human:skylar",
            "prompt": "Approve plan?",
            "artifact": {"summary": "Plan for LA"},
            "allowed": ["approve", "reject"],
            "schema": None,
            "authority": [],
            "timeout": None,
            "requested_seq": 8,
            "decision": {"action": "approve", "by": "skylar"},
            "source": {"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/1/message/3"},
        }
    ]


def test_cli_events_rejects_missing_workflow_and_invalid_limit(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_waiting",
        '{"destination":"NYC"}',
    )

    with pytest.raises(subprocess.CalledProcessError):
        run_cli(tmp_path, "events", "--db", str(db), "--id", "missing")

    with pytest.raises(subprocess.CalledProcessError):
        run_cli(tmp_path, "events", "--db", str(db), "--id", "wf_waiting", "--limit", "0")


def test_cli_outbox_marks_active_approval_waits_with_read_only_diagnostics(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_waiting",
        '{"destination":"NYC"}',
    )

    outbox_payload = json.loads(
        run_cli(tmp_path, "outbox", "--db", str(db), "--id", "wf_waiting", "--status", "pending").stdout
    )
    command = outbox_payload["commands"][0]
    assert command["key"] == "approval:approve_plan"
    assert command["workflow_status"] == "waiting"
    assert command["waiting_on"] == "signal:approval.decision:approve_plan"
    assert command["diagnostic_label"] == "active_wait"
    assert command["diagnostic_labels"] == ["active_wait"]

    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_waiting").stdout)
    assert status_payload["pending_commands"][0]["diagnostic_label"] == "active_wait"
    assert status_payload["diagnostics"] == [
        {
            "command_key": "approval:approve_plan",
            "command_type": "notify_approval",
            "label": "active_wait",
            "message": "Workflow is actively waiting on this approval signal.",
            "severity": "info",
        }
    ]


def test_cli_outbox_flags_stale_completed_workflow_approval_rows_without_mutating_them(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_done",
        '{"destination":"LA"}',
    )
    run_cli(
        tmp_path,
        "signal",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_done",
        "--type",
        "approval.decision",
        "--key",
        "approve_plan",
        "--payload-json",
        '{"action":"approve","by":"skylar"}',
        "--source-json",
        '{"kind":"human","id":"skylar","channel":"discord","message_url":"discord://thread/1/message/4"}',
    )
    run_worker(tmp_path, "demo_wf:demo_workflow", db, "wf_done", max_commands=1)

    with sqlite3.connect(db) as con:
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET status = 'pending', updated_at = updated_at + 1
            WHERE workflow_id = 'wf_done' AND key = 'approval:approve_plan'
            """
        )

    outbox_payload = json.loads(
        run_cli(tmp_path, "outbox", "--db", str(db), "--id", "wf_done", "--status", "pending").stdout
    )
    command = outbox_payload["commands"][0]
    assert command["workflow_status"] == "completed"
    assert command["waiting_on"] is None
    assert command["diagnostic_label"] == "matching_signal_exists"
    assert command["diagnostic_labels"] == ["matching_signal_exists", "terminal_workflow_has_pending_command"]

    # Diagnostics are read-only: the stale pending row remains visible after inspection.
    again = json.loads(run_cli(tmp_path, "outbox", "--db", str(db), "--id", "wf_done", "--status", "pending").stdout)
    assert [command["key"] for command in again["commands"]] == ["approval:approve_plan"]

    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_done").stdout)
    assert [diagnostic["label"] for diagnostic in status_payload["diagnostics"]] == [
        "matching_signal_exists",
        "terminal_workflow_has_pending_command",
    ]


def test_cli_dashboard_renders_workflows_and_approvals_without_mutating_db(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    out = tmp_path / "dashboard.html"

    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_waiting",
        '{"destination":"NYC"}',
    )
    with sqlite3.connect(db) as con:
        before = {
            "events": con.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0],
            "commands": con.execute("SELECT COUNT(*) FROM workflow_commands_outbox").fetchone()[0],
        }

    payload = json.loads(run_cli(tmp_path, "dashboard", "--db", str(db), "--out", str(out)).stdout)

    assert payload == {"dashboard": str(out)}
    html = out.read_text(encoding="utf-8")
    assert "Hermes Workflows Dashboard" in html
    assert "Read-only local dashboard" in html
    assert "wf_waiting" in html
    assert "approval:approve_plan" in html
    assert "Approve plan?" in html
    assert "hermes-workflows approve" in html
    assert "--key approve_plan" in html
    assert "active_wait" in html
    with sqlite3.connect(db) as con:
        after = {
            "events": con.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0],
            "commands": con.execute("SELECT COUNT(*) FROM workflow_commands_outbox").fetchone()[0],
        }
    assert after == before


def test_cli_dashboard_rejects_missing_db_without_creating_it(tmp_path):
    missing_db = tmp_path / "missing.sqlite"
    with pytest.raises(subprocess.CalledProcessError):
        run_cli(tmp_path, "dashboard", "--db", str(missing_db), "--out", str(tmp_path / "dashboard.html"))
    assert not missing_db.exists()

def test_cli_approve_shortcut_sends_human_provenance_signal(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_cli_approve",
        '{"destination":"NYC"}',
    )

    payload = json.loads(
        run_cli(
            tmp_path,
            "approve",
            "demo_wf:demo_workflow",
            "--db",
            str(db),
            "--id",
            "wf_cli_approve",
            "--key",
            "approve_plan",
            "--by",
            "skylar",
            "--channel",
            "discord",
            "--message-url",
            "discord://thread/1/message/2",
            "--note",
            "reviewed in cli shortcut",
        ).stdout
    )

    assert payload["status"] == "running"
    completed_payload = run_worker(tmp_path, "demo_wf:demo_workflow", db, "wf_cli_approve", max_commands=1)
    assert completed_payload["status"] == "completed"
    assert completed_payload["result"]["approved_by"] == "skylar"
    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_cli_approve").stdout)
    approval = status_payload["approvals"][0]
    assert approval["decision"]["action"] == "approve"
    assert approval["source"] == {
        "kind": "human",
        "id": "skylar",
        "channel": "discord",
        "message_url": "discord://thread/1/message/2",
    }


def test_cli_doctor_reports_importable_packaged_example(tmp_path):
    payload = json.loads(
        run_cli(
            tmp_path,
            "doctor",
            "--db",
            str(tmp_path / "workflow.sqlite"),
            "--workflow-ref",
            "hermes_workflows.examples.trip:trip_planning_workflow",
        ).stdout
    )

    assert payload["doctor"]["ok"] is True
    assert payload["doctor"]["workflow_ref_importable"] is True
    assert payload["doctor"]["db_exists"] is False


def test_packaged_trip_example_runs_without_repo_examples_path(tmp_path):
    db = tmp_path / "workflow.sqlite"
    run_payload = start_to_waiting(
        tmp_path,
        "hermes_workflows.examples.trip:trip_planning_workflow",
        db,
        "wf_trip_quickstart",
        '{"destination":"NYC","approver":"human:operator"}',
        max_commands=10,
    )
    assert run_payload["status"] == "waiting"
    assert run_payload["waiting_on"] == "signal:approval.decision:approve_trip_plan"

    approved = json.loads(
        run_cli(
            tmp_path,
            "approve",
            "hermes_workflows.examples.trip:trip_planning_workflow",
            "--db",
            str(db),
            "--id",
            "wf_trip_quickstart",
            "--key",
            "approve_trip_plan",
            "--by",
            "operator",
            "--channel",
            "cli",
            "--message-id",
            "manual-approval-1",
        ).stdout
    )
    assert approved["status"] == "running"
    completed = run_worker(
        tmp_path,
        "hermes_workflows.examples.trip:trip_planning_workflow",
        db,
        "wf_trip_quickstart",
        max_commands=1,
    )
    assert completed["status"] == "completed"
    assert completed["result"]["approved"] is True
    assert completed["result"]["approved_by"] == "operator"


def test_cli_serve_dashboard_is_read_only_without_approval_actions(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_web_read_only",
        '{"destination":"NYC"}',
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_workflows",
            "serve-dashboard",
            "demo_wf:demo_workflow",
            "--db",
            str(db),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ],
        cwd=Path.cwd(),
        env={**os.environ, "PYTHONPATH": f"{Path.cwd() / 'src'}:{tmp_path}:{os.environ.get('PYTHONPATH', '')}"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        line = proc.stdout.readline().strip()
        payload = json.loads(line)

        from urllib.error import HTTPError
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        html = urlopen(payload["url"], timeout=5).read().decode("utf-8")
        assert "Hermes Workflows Dashboard" in html
        assert "Local approval form" not in html

        body = urlencode(
            {
                "workflow_id": "wf_web_read_only",
                "key": "approve_plan",
                "by": "skylar",
                "channel": "local-dashboard",
                "message_id": "web-click-1",
            }
        ).encode("utf-8")
        request = Request(payload["url"] + "/approve", data=body, method="POST")
        with pytest.raises(HTTPError) as exc_info:
            urlopen(request, timeout=5)
        assert exc_info.value.code == 405
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_web_read_only").stdout)
    assert status_payload["status"] == "waiting"


def test_cli_serve_dashboard_read_only_does_not_import_workflow_module(tmp_path):
    marker = tmp_path / "imported.txt"
    (tmp_path / "side_effect_wf.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "from hermes_workflows import wait_for, workflow\n"
        "@workflow\n"
        "async def side_effect_workflow(inputs):\n"
        "    return {'ok': True}\n"
    )
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_import_guard",
        '{"destination":"NYC"}',
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_workflows",
            "serve-dashboard",
            "side_effect_wf:side_effect_workflow",
            "--db",
            str(db),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ],
        cwd=Path.cwd(),
        env={**os.environ, "PYTHONPATH": f"{Path.cwd() / 'src'}:{tmp_path}:{os.environ.get('PYTHONPATH', '')}"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        json.loads(proc.stdout.readline().strip())
        assert not marker.exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_cli_serve_dashboard_can_approve_waiting_workflow(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    start_to_waiting(
        tmp_path,
        "demo_wf:demo_workflow",
        db,
        "wf_web_approval",
        '{"destination":"NYC"}',
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_workflows",
            "serve-dashboard",
            "demo_wf:demo_workflow",
            "--db",
            str(db),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--once",
            "--enable-approval-actions",
        ],
        cwd=Path.cwd(),
        env={**os.environ, "PYTHONPATH": f"{Path.cwd() / 'src'}:{tmp_path}:{os.environ.get('PYTHONPATH', '')}"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        line = proc.stdout.readline().strip()
        payload = json.loads(line)
        assert payload["url"].startswith("http://127.0.0.1:")

        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        html = urlopen(payload["url"], timeout=5).read().decode("utf-8")
        assert "Hermes Workflows Dashboard" in html
        assert "approve_plan" in html

        body = urlencode(
            {
                "workflow_id": "wf_web_approval",
                "key": "approve_plan",
                "by": "skylar",
                "channel": "local-dashboard",
                "message_id": "web-click-1",
            }
        ).encode("utf-8")
        request = Request(payload["url"] + "/approve", data=body, method="POST")
        response = urlopen(request, timeout=5)
        assert response.status == 200
        assert "Approval recorded" in response.read().decode("utf-8")
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    completed_payload = run_worker(tmp_path, "demo_wf:demo_workflow", db, "wf_web_approval", max_commands=1)
    assert completed_payload["status"] == "completed"
    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_web_approval").stdout)
    assert status_payload["status"] == "completed"
    assert status_payload["result"]["approved_by"] == "skylar"
    approval = status_payload["approvals"][0]
    assert approval["source"] == {
        "kind": "human",
        "id": "skylar",
        "channel": "local-dashboard",
        "message_id": "web-click-1",
    }


def test_hermes_workflows_run_uses_uv_and_project_default_db_for_registry_alias(tmp_path):
    (tmp_path / "alias_wf.py").write_text(
        "from hermes_workflows import wait_for, workflow\n"
        "@workflow\n"
        "async def alias_workflow(inputs):\n"
        "    return {'message': inputs['message']}\n"
    )
    registry = tmp_path / ".hermes" / "workflows.registry.json"
    registry.parent.mkdir()
    registry.write_text(
        json.dumps(
            {
                "workflows": {
                    "alias-demo": {
                        "workflow_ref": "alias_wf:alias_workflow",
                        "default_input": {"message": "from-registry"},
                    }
                }
            }
        )
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "uv-called.json"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        f"open({str(marker)!r}, 'w').write(json.dumps({{'args': sys.argv[1:], 'cwd': os.getcwd()}}))\n"
        "args = sys.argv[1:]\n"
        "assert args[0] == 'run'\n"
        "raise SystemExit(subprocess.run(args[1:]).returncode)\n"
    )
    fake_uv.chmod(0o755)

    payload = json.loads(
        run_cli(
            tmp_path,
            "run",
            "alias-demo",
            "--config",
            str(registry),
            "--project-root",
            str(tmp_path),
            env_extra={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        ).stdout
    )

    assert payload["status"] == "running"
    assert payload["waiting_on"] is None
    assert (tmp_path / ".hermes" / "workflows.sqlite").exists()
    completed_payload = run_worker(
        tmp_path,
        "alias_wf:alias_workflow",
        tmp_path / ".hermes" / "workflows.sqlite",
        payload["workflow_id"],
        max_commands=1,
    )
    assert completed_payload["status"] == "completed"
    assert completed_payload["result"] == {"message": "from-registry"}
    uv_call = json.loads(marker.read_text())
    uv_args = uv_call["args"]
    assert uv_call["cwd"] == str(tmp_path)
    assert uv_args[:4] == ["run", "python", "-m", "hermes_workflows"]
    assert "_run-engine" in uv_args


def test_workflow_run_helper_supports_direct_uv_script_style_and_default_db(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is required for direct uv script smoke")
    script = tmp_path / "direct_workflow.py"
    script.write_text(
        "from hermes_workflows import wait_for, workflow\n"
        "@workflow\n"
        "async def direct_workflow(inputs):\n"
        "    return {'ok': inputs.get('ok', False)}\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(direct_workflow.run())\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}:{env.get('PYTHONPATH', '')}"
    completed = subprocess.run(
        [
            "uv",
            "run",
            str(script),
            "--project-root",
            str(tmp_path),
            "--id",
            "wf_direct_helper",
            "--input-json",
            '{"ok":true}',
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["status"] == "running"
    assert payload["waiting_on"] is None
    assert (tmp_path / ".hermes" / "workflows.sqlite").exists()
    completed_payload = run_worker(
        tmp_path,
        str(script),
        tmp_path / ".hermes" / "workflows.sqlite",
        "wf_direct_helper",
        max_commands=1,
    )
    assert completed_payload["status"] == "completed"
    assert completed_payload["result"] == {"ok": True}


def test_run_no_drain_replays_memoized_step_outputs_from_same_entrypoint_and_db(tmp_path):
    (tmp_path / "memo_wf.py").write_text(
        "from hermes_workflows import approve, step, workflow\n"
        "@step\n"
        "async def compute(value):\n"
        "    return {'computed': value}\n"
        "@workflow\n"
        "async def memo_workflow(inputs):\n"
        "    result = await compute(inputs['value'])\n"
        "    return {'final': result}\n"
    )
    db = tmp_path / ".hermes" / "workflows.sqlite"
    first = json.loads(
        run_cli(
            tmp_path,
            "run",
            str(tmp_path / "memo_wf.py"),
            "--project-root",
            str(tmp_path),
            "--id",
            "wf_memo",
            "--input-json",
            '{"value":"from-worker"}',
            "--no-drain",
        ).stdout
    )
    assert first == {
        "workflow_id": "wf_memo",
        "status": "running",
        "waiting_on": None,
        "result": None,
        "error": None,
    }
    waiting = run_worker(tmp_path, str(tmp_path / "memo_wf.py"), db, "wf_memo", max_commands=1)
    assert waiting["status"] == "waiting"
    assert waiting["waiting_on"] == "step:compute:0"

    # Simulate an out-of-process worker publishing durable output without
    # relying on an in-memory Python stack from the first run. The completion
    # transition wakes the workflow by reusing the singleton run_workflow row.
    with sqlite3.connect(db) as con:
        event_count_before = con.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0]
        next_seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM workflow_events WHERE workflow_id = 'wf_memo'").fetchone()[0]
        con.execute(
            """
            INSERT INTO workflow_events(workflow_id, seq, type, key, payload_json, idempotency_key, created_at)
            VALUES ('wf_memo', ?, 'StepCompleted', 'step:compute:0', ?, 'completed:step:compute:0', 1)
            """,
            (next_seq, '{"output":{"computed":"from-worker"}}'),
        )
        con.execute("UPDATE workflow_commands_outbox SET status = 'completed' WHERE workflow_id = 'wf_memo' AND key = 'step:compute:0'")
        con.execute("UPDATE workflow_instances SET status = 'running', waiting_on = NULL WHERE id = 'wf_memo'")
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET status = 'pending', payload_json = ?, claimed_by = NULL, lease_expires_at = NULL, updated_at = updated_at + 1
            WHERE workflow_id = 'wf_memo' AND key = 'workflow:run'
            """,
            ('{"reason":"step_completed","source_key":"step:compute:0"}',),
        )

    resumed = run_worker(tmp_path, str(tmp_path / "memo_wf.py"), db, "wf_memo", max_commands=1)

    assert resumed["status"] == "completed"
    assert resumed["result"] == {"final": {"computed": "from-worker"}}
    with sqlite3.connect(db) as con:
        requested_count = con.execute(
            "SELECT COUNT(*) FROM workflow_events WHERE workflow_id = 'wf_memo' AND type = 'StepRequested'"
        ).fetchone()[0]
        event_count_after = con.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0]
    assert requested_count == 1
    assert event_count_after > event_count_before


def test_registry_discover_lists_workflow_files(tmp_path):
    workflow_file = tmp_path / "workflows" / "daily_ops.py"
    workflow_file.parent.mkdir()
    (workflow_file.parent / "helpers.py").write_text("DEFAULT_OUTPUT = {'from': 'sibling'}\n")
    workflow_file.write_text(
        "from hermes_workflows import wait_for, workflow\n"
        "from helpers import DEFAULT_OUTPUT\n"
        "@workflow\n"
        "async def daily_ops(inputs):\n"
        "    return DEFAULT_OUTPUT\n"
    )

    payload = json.loads(
        run_cli(
            tmp_path,
            "registry",
            "discover",
            "--project-root",
            str(tmp_path),
        ).stdout
    )

    assert payload["project_root"] == str(tmp_path)
    assert payload["workflows"] == [
        {
            "name": "daily_ops",
            "workflow_ref": str(workflow_file) + ":daily_ops",
            "path": str(workflow_file),
            "symbol": "daily_ops",
            "workflow_name": "daily_ops",
        }
    ]


def run_cli_from(cwd, pythonpath_entries, *args, check=True, env_extra=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = ":".join(str(entry) for entry in pythonpath_entries) + f":{env.get('PYTHONPATH', '')}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_workflows", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def test_registry_discover_is_static_and_does_not_execute_workflow_modules(tmp_path):
    marker = tmp_path / "import-side-effect.txt"
    workflow_file = tmp_path / "dangerous_workflow.py"
    workflow_file.write_text(
        "from pathlib import Path\n"
        "from hermes_workflows import wait_for, workflow\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "@workflow\n"
        "async def dangerous_workflow(inputs):\n"
        "    return inputs\n"
    )

    payload = json.loads(
        run_cli(
            tmp_path,
            "registry",
            "discover",
            "--project-root",
            str(tmp_path),
        ).stdout
    )

    assert not marker.exists()
    assert payload["workflows"] == [
        {
            "name": "dangerous_workflow",
            "workflow_ref": str(workflow_file) + ":dangerous_workflow",
            "path": str(workflow_file),
            "symbol": "dangerous_workflow",
            "workflow_name": "dangerous_workflow",
        }
    ]


def test_doctor_reports_bad_workflow_ref_as_json_instead_of_exiting_early(tmp_path):
    completed = run_cli_from(
        tmp_path,
        [Path.cwd() / "src", tmp_path],
        "doctor",
        "--db",
        str(tmp_path / "doctor.sqlite"),
        "--workflow-ref",
        "no_such_module:missing",
        check=False,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["doctor"]["workflow_ref_importable"] is False
    assert payload["doctor"]["ok"] is False
    assert "No module named" in payload["doctor"]["workflow_ref_error"]


def test_run_with_external_config_defaults_db_to_config_project_root(tmp_path):
    project = tmp_path / "workflow_project"
    caller = tmp_path / "caller"
    project.mkdir()
    caller.mkdir()
    (project / ".hermes").mkdir()
    (project / "configured_wf.py").write_text(
        "from hermes_workflows import wait_for, workflow\n"
        "@workflow\n"
        "async def configured_wf(inputs):\n"
        "    return {'project': inputs['project']}\n"
    )
    registry = project / ".hermes" / "workflows.registry.json"
    registry.write_text(
        json.dumps(
            {
                "workflows": {
                    "configured": {
                        "workflow_ref": str(project / "configured_wf.py") + ":configured_wf",
                        "default_input": {"project": "right-db"},
                    }
                }
            }
        )
    )

    payload = json.loads(
        run_cli_from(
            caller,
            [Path.cwd() / "src", project],
            "run",
            "configured",
            "--config",
            str(registry),
            "--id",
            "wf_configured",
        ).stdout
    )

    assert payload["status"] == "running"
    assert payload["waiting_on"] is None
    assert payload["result"] is None
    assert (project / ".hermes" / "workflows.sqlite").exists()
    assert not (caller / ".hermes" / "workflows.sqlite").exists()


def test_direct_workflow_run_defaults_db_to_workflow_file_project(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is required for direct uv script smoke")
    project = tmp_path / "workflow_project"
    caller = tmp_path / "caller"
    project.mkdir()
    caller.mkdir()
    script = project / "direct_project_workflow.py"
    script.write_text(
        "from hermes_workflows import wait_for, workflow\n"
        "@workflow\n"
        "async def direct_project_workflow(inputs):\n"
        "    return {'value': inputs['value']}\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(direct_project_workflow.run())\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}:{env.get('PYTHONPATH', '')}"
    completed = subprocess.run(
        ["uv", "run", str(script), "--id", "wf_direct_project", "--input-json", '{"value":"project-db"}'],
        cwd=caller,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["status"] == "running"
    assert payload["waiting_on"] is None
    assert payload["result"] is None
    assert (project / ".hermes" / "workflows.sqlite").exists()
    assert not (caller / ".hermes" / "workflows.sqlite").exists()


def test_run_via_uv_uses_config_project_as_child_process_cwd(tmp_path):
    project = tmp_path / "workflow_project"
    caller = tmp_path / "caller"
    project.mkdir()
    caller.mkdir()
    (project / ".hermes").mkdir()
    (project / "configured_wf.py").write_text(
        "from hermes_workflows import wait_for, workflow\n"
        "@workflow\n"
        "async def configured_wf(inputs):\n"
        "    return {'project': inputs['project']}\n"
    )
    registry = project / ".hermes" / "workflows.registry.json"
    registry.write_text(
        json.dumps(
            {
                "workflows": {
                    "configured": {
                        "workflow_ref": str(project / "configured_wf.py") + ":configured_wf",
                        "default_input": {"project": "uv-cwd"},
                    }
                }
            }
        )
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "uv-cwd.json"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        f"open({str(marker)!r}, 'w').write(json.dumps({{'args': sys.argv[1:], 'cwd': os.getcwd()}}))\n"
        "args = sys.argv[1:]\n"
        "assert args[0] == 'run'\n"
        "raise SystemExit(subprocess.run(args[1:]).returncode)\n"
    )
    fake_uv.chmod(0o755)

    payload = json.loads(
        run_cli_from(
            caller,
            [Path.cwd() / "src", project],
            "run",
            "configured",
            "--config",
            str(registry),
            "--id",
            "wf_config_uv_cwd",
            env_extra={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        ).stdout
    )

    assert payload["status"] == "running"
    assert payload["waiting_on"] is None
    assert payload["result"] is None
    uv_call = json.loads(marker.read_text())
    assert uv_call["cwd"] == str(project)
    assert (project / ".hermes" / "workflows.sqlite").exists()
    assert not (caller / ".hermes" / "workflows.sqlite").exists()


def test_module_ref_run_defaults_db_to_imported_module_project(tmp_path):
    project = tmp_path / "workflow_project"
    caller = tmp_path / "caller"
    project.mkdir()
    caller.mkdir()
    (project / "module_wf.py").write_text(
        "from hermes_workflows import wait_for, workflow\n"
        "@workflow\n"
        "async def module_workflow(inputs):\n"
        "    return {'ok': True}\n"
    )

    payload = json.loads(
        run_cli_from(
            caller,
            [Path.cwd() / "src", project],
            "run",
            "module_wf:module_workflow",
            "--direct",
            "--id",
            "wf_module_ref",
        ).stdout
    )

    assert payload["status"] == "running"
    assert payload["waiting_on"] is None
    assert payload["result"] is None
    assert (project / ".hermes" / "workflows.sqlite").exists()
    assert not (caller / ".hermes" / "workflows.sqlite").exists()


def test_internal_run_engine_command_is_hidden_from_top_level_help(tmp_path):
    completed = run_cli_from(tmp_path, [Path.cwd() / "src", tmp_path], "--help")
    assert "_run-engine" not in completed.stdout

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from hermes_workflows import AgentRunnerError, SubprocessAgentRunner, Workflow, WorkflowEngine, agent, workflow


GENERATED_SOURCE = '''
from hermes_workflows import step, workflow

# Unique source for subprocess-runner approval tests so full-suite module cache
# state from other generated-workflow tests cannot satisfy this approval gate.
@step
async def label_item(ctx, item):
    return {"id": item["id"], "label": item["label"].upper()}

@workflow
async def process_item(ctx, item):
    return {"processed": await label_item(ctx, item)}
'''


@workflow
async def subprocess_json_pipeline(ctx, inputs):
    return await agent(
        "double_value",
        prompt=f"Double {inputs['value']}",
        input={"value": inputs["value"]},
    )


@workflow
async def subprocess_generated_workflow_pipeline(ctx, inputs):
    processor = await agent(
        "build_processor",
        prompt=f"Write a Python workflow for {inputs['kind']} items.",
        input={"kind": inputs["kind"]},
        returns=Workflow,
    )
    return await processor(inputs["item"], key=inputs["item"]["id"])


def _write_runner(tmp_path, source: str):
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")
    return runner


def test_subprocess_runner_executes_agent_and_records_provenance(tmp_path):
    runner_script = _write_runner(
        tmp_path,
        '''
        import json
        import sys

        request = json.load(sys.stdin)
        assert request["kind"] == "agent.runner_request.v1"
        assert request["rendered_prompt"] == "Double 21"
        json.dump({
            "output": {"answer": request["input"]["value"] * 2},
            "provenance": {"runner": "fixture", "request_name": request["name"]},
        }, sys.stdout)
        ''',
    )

    engine = WorkflowEngine(
        tmp_path / "workflow.sqlite",
        agent_runner=SubprocessAgentRunner([sys.executable, str(runner_script)]),
    )
    result = engine.run_until_idle(subprocess_json_pipeline, {"value": 21}, workflow_id="wf_runner")

    assert result.status == "completed"
    assert result.result == {"answer": 42}

    completed = [event for event in engine.events("wf_runner") if event["type"] == "StepCompleted"]
    assert completed[0]["payload"]["metadata"]["provenance"] == {
        "runner": "fixture",
        "request_name": "double_value",
    }
    assert completed[0]["payload"]["metadata"]["request"]["kind"] == "agent.runner_request.v1"
    assert completed[0]["payload"]["metadata"]["response"] == {
        "output": {"answer": 42},
        "provenance": {"runner": "fixture", "request_name": "double_value"},
    }


def test_subprocess_runner_nonzero_exit_reports_diagnostics(tmp_path):
    runner_script = _write_runner(
        tmp_path,
        '''
        import sys
        print("bad stdout", file=sys.stdout)
        print("boom stderr", file=sys.stderr)
        raise SystemExit(7)
        ''',
    )

    runner = SubprocessAgentRunner([sys.executable, str(runner_script)])

    with pytest.raises(AgentRunnerError) as excinfo:
        runner({"kind": "agent.runner_request.v1"})

    assert "exited with code 7" in str(excinfo.value)
    assert excinfo.value.details["exit_code"] == 7
    assert "boom stderr" in excinfo.value.details["stderr_tail"]
    assert "bad stdout" in excinfo.value.details["stdout_tail"]
    assert "env" not in excinfo.value.details


def test_subprocess_runner_invalid_json_stdout_fails_closed(tmp_path):
    runner_script = _write_runner(tmp_path, 'print("not-json")')
    runner = SubprocessAgentRunner([sys.executable, str(runner_script)])

    with pytest.raises(AgentRunnerError) as excinfo:
        runner({"kind": "agent.runner_request.v1"})

    assert "invalid JSON" in str(excinfo.value)
    assert "not-json" in excinfo.value.details["stdout_tail"]


def test_subprocess_runner_missing_output_fails_closed(tmp_path):
    runner_script = _write_runner(
        tmp_path,
        '''
        import json
        import sys
        json.dump({"provenance": {"runner": "fixture"}}, sys.stdout)
        ''',
    )
    runner = SubprocessAgentRunner([sys.executable, str(runner_script)])

    with pytest.raises(AgentRunnerError) as excinfo:
        runner({"kind": "agent.runner_request.v1"})

    assert "must include an 'output' field" in str(excinfo.value)


def test_subprocess_runner_timeout_fails_closed_without_env_dump(tmp_path):
    runner_script = _write_runner(
        tmp_path,
        '''
        import time
        time.sleep(5)
        ''',
    )
    runner = SubprocessAgentRunner(
        [sys.executable, str(runner_script)],
        timeout_seconds=0.1,
        env={"API_TOKEN": "secret-token"},
    )

    with pytest.raises(AgentRunnerError) as excinfo:
        runner({"kind": "agent.runner_request.v1"})

    assert "timed out" in str(excinfo.value)
    assert excinfo.value.details["timeout_seconds"] == 0.1
    assert "secret-token" not in str(excinfo.value.details)
    assert "env" not in excinfo.value.details


def test_subprocess_runner_oversized_stdout_fails_closed_before_buffering_all_output(tmp_path):
    runner_script = _write_runner(
        tmp_path,
        '''
        import sys
        sys.stdout.write("x" * 1_000_000)
        ''',
    )
    runner = SubprocessAgentRunner([sys.executable, str(runner_script)], max_stdout_bytes=32)

    with pytest.raises(AgentRunnerError) as excinfo:
        runner({"kind": "agent.runner_request.v1"})

    assert "stdout exceeded 32 bytes" in str(excinfo.value)
    assert excinfo.value.details["stdout_bytes"] == 33


def test_subprocess_runner_errors_fail_workflow_without_step_completion(tmp_path):
    runner_script = _write_runner(tmp_path, 'print("not-json")')
    engine = WorkflowEngine(
        tmp_path / "workflow.sqlite",
        agent_runner=SubprocessAgentRunner([sys.executable, str(runner_script)]),
    )

    result = engine.run_until_idle(subprocess_json_pipeline, {"value": 21}, workflow_id="wf_bad_subprocess")

    assert result.status == "failed"
    assert "AgentRunnerError" in (result.error or "")
    events = engine.events("wf_bad_subprocess")
    assert [event for event in events if event["type"] == "StepCompleted"] == []
    failures = [event for event in events if event["type"] == "StepFailed"]
    assert len(failures) == 1
    assert failures[0]["payload"]["error"]["type"] == "AgentRunnerError"


def test_subprocess_runner_generated_workflow_waits_for_approval_before_import(tmp_path):
    runner_script = _write_runner(
        tmp_path,
        f'''
        import json
        import sys

        request = json.load(sys.stdin)
        json.dump({{
            "output": {{"source": {GENERATED_SOURCE!r}, "symbol": "process_item"}},
            "provenance": {{"runner": "fixture", "request_name": request["name"]}},
        }}, sys.stdout)
        ''',
    )
    engine = WorkflowEngine(
        tmp_path / "workflow.sqlite",
        agent_runner=SubprocessAgentRunner([sys.executable, str(runner_script)]),
    )

    first = engine.run_until_idle(
        subprocess_generated_workflow_pipeline,
        {"kind": "catalog", "item": {"id": "a", "label": "alpha"}},
        workflow_id="wf_generated_subprocess",
    )

    assert first.status == "waiting"
    approvals = engine.workflow_status("wf_generated_subprocess")["approvals"]
    assert len(approvals) == 1
    approval = approvals[0]
    assert first.waiting_on == f"signal:approval.decision:{approval['key']}"
    assert approval["key"].startswith("generated-workflow:")
    assert approval["artifact"]["runner_provenance"] == {
        "runner": "fixture",
        "request_name": "build_processor",
    }
    assert approval["artifact"]["symbol"] == "process_item"
    assert [event for event in engine.events("wf_generated_subprocess") if event["type"] == "ChildWorkflowRequested"] == []
    assert f"hermes_generated_workflows.{approval['artifact']['source_sha256']}" not in sys.modules

    approved = engine.signal(
        "wf_generated_subprocess",
        "approval.decision",
        key=approval["key"],
        payload={"action": "approve", "by": "skylar", "message": "approved in test"},
        source={"kind": "human", "id": "skylar", "channel": "test", "event_id": "evt-1"},
    )
    approved = engine.drain("wf_generated_subprocess", initial=approved)

    assert approved.status == "completed"
    assert approved.result == {"processed": {"id": "a", "label": "ALPHA"}}

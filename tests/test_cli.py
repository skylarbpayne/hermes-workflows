import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


WORKFLOW_MODULE = '''
from hermes_workflows import step, workflow

@step
async def make_plan(ctx, inputs):
    return {"summary": f"Plan for {inputs['destination']}"}

@workflow
async def demo_workflow(ctx, inputs):
    plan = await make_plan(ctx, inputs)
    decision = await ctx.approval.request(
        "Approve plan?",
        key="approve_plan",
        artifact=plan,
        approver="human:skylar",
    )
    return {"plan": plan, "approved_by": decision["by"]}
'''


def run_cli(tmp_path, *args):
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}:{tmp_path}:{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "hermes_workflows", *args],
        cwd=Path.cwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_cli_can_run_and_signal_workflow_across_processes(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    run_result = run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli",
        "--input-json",
        '{"destination":"NYC"}',
    )
    run_payload = json.loads(run_result.stdout)
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
    assert signal_payload == {
        "workflow_id": "wf_cli",
        "status": "completed",
        "waiting_on": None,
        "result": {"plan": {"summary": "Plan for NYC"}, "approved_by": "skylar"},
        "error": None,
    }


def test_cli_status_and_list_expose_inspectable_workflow_state(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli",
        "--input-json",
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
                "status": "waiting",
                "waiting_on": "signal:approval.decision:approve_plan",
            }
        ]
    }


def test_cli_events_outbox_list_filter_and_approval_summary(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_waiting",
        "--input-json",
        '{"destination":"NYC"}',
    )
    run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_done",
        "--input-json",
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
            "decision": {"action": "approve", "by": "skylar"},
            "source": {"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://thread/1/message/3"},
        }
    ]


def test_cli_events_rejects_missing_workflow_and_invalid_limit(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_waiting",
        "--input-json",
        '{"destination":"NYC"}',
    )

    with pytest.raises(subprocess.CalledProcessError):
        run_cli(tmp_path, "events", "--db", str(db), "--id", "missing")

    with pytest.raises(subprocess.CalledProcessError):
        run_cli(tmp_path, "events", "--db", str(db), "--id", "wf_waiting", "--limit", "0")

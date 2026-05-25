import json
import os
import subprocess
import sys
from pathlib import Path


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


def test_cli_inspection_commands_expose_instance_events_outbox_and_status_report(tmp_path):
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

    list_payload = json.loads(run_cli(tmp_path, "list", "--db", str(db)).stdout)
    assert list_payload == {
        "workflows": [
            {
                "workflow_id": "wf_waiting",
                "workflow_name": "demo_workflow",
                "status": "waiting",
                "waiting_on": "signal:approval.decision:approve_plan",
                "updated_at": list_payload["workflows"][0]["updated_at"],
            }
        ]
    }

    status_payload = json.loads(run_cli(tmp_path, "status", "--db", str(db), "--id", "wf_waiting").stdout)
    assert status_payload["workflow_id"] == "wf_waiting"
    assert status_payload["workflow_name"] == "demo_workflow"
    assert status_payload["status"] == "waiting"
    assert status_payload["waiting_on"] == "signal:approval.decision:approve_plan"
    assert status_payload["result"] is None
    assert status_payload["error"] is None
    assert status_payload["event_count"] >= 5
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
    assert status_payload["pending_outbox"] == [
        {
            "id": status_payload["pending_outbox"][0]["id"],
            "type": "notify_approval",
            "key": "approval:approve_plan",
            "status": "pending",
            "attempts": 0,
            "claimed_by": None,
            "lease_expires_at": None,
            "last_error": None,
            "payload": {
                "prompt": "Approve plan?",
                "key": "approve_plan",
                "artifact": {"summary": "Plan for NYC"},
                "approver": "human:skylar",
                "allowed": ["approve", "reject"],
                "authority": [],
                "timeout": None,
            },
        }
    ]
    assert status_payload["recent_events"][-1]["type"] == "WaitRequested"

    events_payload = json.loads(run_cli(tmp_path, "events", "--db", str(db), "--id", "wf_waiting", "--limit", "2").stdout)
    assert [event["type"] for event in events_payload["events"]] == ["ApprovalRequested", "WaitRequested"]

    outbox_payload = json.loads(
        run_cli(tmp_path, "outbox", "--db", str(db), "--id", "wf_waiting", "--status", "pending").stdout
    )
    assert outbox_payload["commands"] == status_payload["pending_outbox"]

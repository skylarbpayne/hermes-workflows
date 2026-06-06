import json
import os
import sqlite3
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


CHILD_WORKFLOW_MODULE = '''
from pathlib import Path
from hermes_workflows import Workflow, workflow

WAITING_SOURCE = """
from hermes_workflows import workflow

@workflow
async def waiting_child(ctx, item):
    payload = await ctx.wait_for(\"dynamic.ready\", key=item[\"id\"])
    return {\"payload\": payload}
"""

CHILD = Workflow.from_source(
    WAITING_SOURCE,
    symbol="waiting_child",
    base_dir=Path(__file__).parent,
)

@workflow
async def parent_workflow(ctx, inputs):
    return await CHILD(ctx, inputs["item"], key=inputs["item"]["id"])
'''


DYNAMIC_CHILD_WORKFLOW_MODULE = '''
from hermes_workflows import AgentStep, Workflow, workflow

WAITING_SOURCE = """
from hermes_workflows import workflow

@workflow
async def waiting_child(ctx, item):
    payload = await ctx.wait_for(\"dynamic.ready\", key=item[\"id\"])
    return {\"payload\": payload}
"""

@workflow
async def generated_parent_workflow(ctx, inputs):
    child = await AgentStep(
        "build_waiting_child",
        prompt="Build a child workflow that waits for a signal.",
        returns=Workflow,
        mock_output={"source": WAITING_SOURCE, "symbol": "waiting_child"},
    )(ctx)
    return await child(ctx, inputs["item"], key=inputs["item"]["id"])
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


def test_cli_reconciles_waiting_child_workflow_across_processes(tmp_path):
    (tmp_path / "child_wf.py").write_text(CHILD_WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    run_payload = json.loads(
        run_cli(
            tmp_path,
            "run",
            "child_wf:parent_workflow",
            "--db",
            str(db),
            "--id",
            "wf_cli_child",
            "--input-json",
            '{"item":{"id":"cli-child"}}',
        ).stdout
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
    assert child_signal_payload["status"] == "completed"
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
    assert reconcile_payload == {
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
    assert reconcile_one_payload == reconcile_payload


def test_cli_signal_can_resume_generated_child_loaded_from_parent_history(tmp_path):
    (tmp_path / "dynamic_child_wf.py").write_text(DYNAMIC_CHILD_WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"

    run_payload = json.loads(
        run_cli(
            tmp_path,
            "run",
            "dynamic_child_wf:generated_parent_workflow",
            "--db",
            str(db),
            "--id",
            "wf_cli_generated_child",
            "--input-json",
            '{"item":{"id":"generated-cli-child"}}',
        ).stdout
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
    assert child_signal_payload["status"] == "completed"

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
    assert reconcile_payload["status"] == "completed"
    assert reconcile_payload["result"] == {"payload": {"ok": True}}


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
                "workflow_ref": "demo_wf:demo_workflow",
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
            "allowed": ["approve", "reject"],
            "authority": [],
            "timeout": None,
            "requested_seq": 5,
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
            "authority": [],
            "timeout": None,
            "requested_seq": 5,
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


def test_cli_outbox_marks_active_approval_waits_with_read_only_diagnostics(tmp_path):
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
        '{"kind":"human","id":"skylar","channel":"discord","message_url":"discord://thread/1/message/4"}',
    )

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
    run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_cli_approve",
        "--input-json",
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

    assert payload["status"] == "completed"
    assert payload["result"]["approved_by"] == "skylar"
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
    run_payload = json.loads(
        run_cli(
            tmp_path,
            "run",
            "hermes_workflows.examples.trip:trip_planning_workflow",
            "--db",
            str(db),
            "--id",
            "wf_trip_quickstart",
            "--input-json",
            '{"destination":"NYC","approver":"human:operator"}',
        ).stdout
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
    assert approved["status"] == "completed"
    assert approved["result"]["approved"] is True
    assert approved["result"]["approved_by"] == "operator"


def test_cli_serve_dashboard_is_read_only_without_approval_actions(tmp_path):
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_web_read_only",
        "--input-json",
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
        "from hermes_workflows import workflow\n"
        "@workflow\n"
        "async def side_effect_workflow(ctx, inputs):\n"
        "    return {'ok': True}\n"
    )
    (tmp_path / "demo_wf.py").write_text(WORKFLOW_MODULE)
    db = tmp_path / "workflow.sqlite"
    run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_import_guard",
        "--input-json",
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
    run_cli(
        tmp_path,
        "run",
        "demo_wf:demo_workflow",
        "--db",
        str(db),
        "--id",
        "wf_web_approval",
        "--input-json",
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

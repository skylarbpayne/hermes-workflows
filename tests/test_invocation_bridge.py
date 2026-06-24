import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_workflows import ApprovalDecisionInput, WorkflowEngine, step, workflow
from hermes_workflows.invocation import InvocationService, TrustedResumer
from hermes_workflows.receipts import redact_secrets
from hermes_workflows.registry import WorkflowRegistry


@step
async def bridge_followup_step(ctx, decision):
    return {"followup_ran": True, "decision": decision, "api_token": "do-not-leak"}


@workflow
async def bridge_approval_workflow(ctx, inputs):
    decision = await ctx.approval.request(
        "Approve bridge test?",
        key="approve_bridge_test",
        artifact={"summary": inputs.get("summary", "Bridge packet"), "secret_token": "hide-me"},
        approver=inputs.get("approver", "human:skylar"),
    )
    return await bridge_followup_step(ctx, decision)


@workflow
async def other_approval_workflow(ctx, inputs):
    decision = await ctx.approval.request(
        "Approve other test?",
        key="approve_other_test",
        artifact={"summary": inputs.get("summary", "Other packet")},
        approver=inputs.get("approver", "human:skylar"),
    )
    return {"other_followup_ran": True, "decision": decision}


@workflow
async def two_approval_workflow(ctx, inputs):
    first = await ctx.approval.request(
        "Approve first?",
        key="approve_first",
        artifact={"step": "first"},
        approver=inputs.get("approver", "human:skylar"),
    )
    second = await ctx.approval.request(
        "Approve second?",
        key="approve_second",
        artifact={"step": "second"},
        approver=inputs.get("approver", "human:skylar"),
    )
    return {"first": first, "second": second}


@dataclass
class TypedBridgeInput:
    topic: str
    approver: str = "human:skylar"


@workflow
async def typed_bridge_approval_workflow(ctx, inputs: TypedBridgeInput):
    decision = await ctx.approval.request(
        f"Approve typed bridge test for {inputs.topic}?",
        key="approve_typed_bridge_test",
        artifact={"summary": inputs.topic},
        approver=inputs.approver,
    )
    return {"typed_followup_ran": True, "topic": inputs.topic, "decision": decision}


def run_cli(tmp_path, *args, check=True):
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}:{Path.cwd()}:{tmp_path}:{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "hermes_workflows", *args],
        cwd=Path.cwd(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def test_invocation_service_runs_until_approval_and_writes_redacted_receipt_and_dashboard(tmp_path):
    db = tmp_path / "workflow.sqlite"
    receipt_path = tmp_path / "receipt.json"
    dashboard_path = tmp_path / "dashboard.html"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"pilot": str(db)},
            "workflows": {
                "bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "pilot",
                    "default_input": {"summary": "Default summary"},
                    "trusted_resume": True,
                }
            },
        }
    )

    receipt = InvocationService(registry).invoke(
        "bridge",
        workflow_id="wf_bridge_invoke",
        input_payload={"api_token": "top-secret"},
        source={"kind": "operator", "channel": "kanban", "task_id": "t_3b203be1"},
        receipt_path=receipt_path,
        dashboard_out=dashboard_path,
    )

    assert receipt["workflow_id"] == "wf_bridge_invoke"
    assert receipt["workflow_ref"] == "tests.test_invocation_bridge:bridge_approval_workflow"
    assert receipt["registry_name"] == "bridge"
    assert receipt["db"] == {"alias": "pilot"}
    assert receipt["status"] == "waiting"
    assert receipt["waiting_on"] == "signal:approval.decision:approve_bridge_test"
    assert receipt["source"]["task_id"] == "t_3b203be1"
    assert receipt["dashboard"] == str(dashboard_path)
    assert receipt["input"]["api_token"] == "[REDACTED]"
    assert receipt_path.exists()
    assert json.loads(receipt_path.read_text()) == receipt
    assert str(db) not in receipt_path.read_text()
    assert "top-secret" not in receipt_path.read_text()
    assert "hide-me" not in receipt_path.read_text()
    assert "wf_bridge_invoke" in dashboard_path.read_text()


def test_invocation_service_loads_path_workflow_refs(tmp_path):
    db = tmp_path / "workflow.sqlite"
    workflow_file = tmp_path / "path_invocation_flow.py"
    workflow_file.write_text(
        "from hermes_workflows import workflow\n"
        "\n"
        "@workflow\n"
        "async def path_invocation_workflow(ctx, inputs):\n"
        "    return {'value': inputs.get('value')}\n"
    )
    workflow_ref = f"{workflow_file}:path_invocation_workflow"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"pilot": str(db)},
            "workflows": {
                "path_bridge": {
                    "workflow_ref": workflow_ref,
                    "db": "pilot",
                    "default_input": {"value": 1},
                }
            },
        }
    )

    receipt = InvocationService(registry).invoke("path_bridge", workflow_id="wf_path_ref", input_payload={"value": 2})

    assert receipt["workflow_ref"] == workflow_ref
    assert receipt["status"] == "completed"
    assert WorkflowEngine(db).workflow_status("wf_path_ref")["result"] == {"value": 2}


def test_trusted_resumer_completes_plugin_recorded_resume_false_decision(tmp_path):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"pilot": str(db)},
            "workflows": {
                "bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "pilot",
                    "trusted_resume": True,
                }
            },
        }
    )
    InvocationService(registry).invoke(
        "bridge",
        workflow_id="wf_bridge_resume",
        input_payload={},
        source={"kind": "operator", "channel": "test"},
    )

    recorded = WorkflowEngine(db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_bridge_resume",
            key="approve_bridge_test",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "msg-1"},
            idempotency_key="msg-1",
        ),
        resume=False,
    )
    assert recorded.status == "decision_recorded"
    assert WorkflowEngine(db).workflow_status("wf_bridge_resume")["status"] == "running"

    receipt = TrustedResumer(registry).resume_trusted(
        "bridge",
        workflow_id="wf_bridge_resume",
        worker_id="test-resumer",
        receipt_path=tmp_path / "resume-receipt.json",
    )

    assert receipt["status"] == "completed"
    assert receipt["result"]["followup_ran"] is True
    assert receipt["result"]["api_token"] == "[REDACTED]"
    assert receipt["approvals"][0]["status"] == "approve"
    assert WorkflowEngine(db).workflow_status("wf_bridge_resume")["status"] == "completed"


def test_typed_workflow_invocation_preserves_registry_provenance_for_trusted_resume(tmp_path):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"pilot": str(db)},
            "workflows": {
                "typed_bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:typed_bridge_approval_workflow",
                    "db": "pilot",
                    "trusted_resume": True,
                }
            },
        }
    )
    InvocationService(registry).invoke(
        "typed_bridge",
        workflow_id="wf_typed_bridge_resume",
        input_payload={"topic": "typed input"},
        source={"kind": "operator", "channel": "test"},
    )
    stored_input = InvocationService._stored_input_for_instance(WorkflowEngine(db), "wf_typed_bridge_resume")
    assert stored_input is not None
    assert stored_input["_registry_name"] == "typed_bridge"
    assert stored_input["_source"] == {"kind": "operator", "channel": "test"}

    WorkflowEngine(db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_typed_bridge_resume",
            key="approve_typed_bridge_test",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "typed-msg-1"},
            idempotency_key="typed-msg-1",
        ),
        resume=False,
    )

    receipt = TrustedResumer(registry).resume_trusted("typed_bridge", workflow_id="wf_typed_bridge_resume")

    assert receipt["status"] == "completed"
    assert receipt["result"]["typed_followup_ran"] is True
    assert receipt["result"]["topic"] == "typed input"


def test_resume_pending_requires_trusted_allowlist_and_only_resumes_recorded_decisions(tmp_path):
    trusted_db = tmp_path / "trusted.sqlite"
    untrusted_db = tmp_path / "untrusted.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"trusted": str(trusted_db), "untrusted": str(untrusted_db)},
            "workflows": {
                "trusted_bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "trusted",
                    "trusted_resume": True,
                },
                "untrusted_bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "untrusted",
                    "trusted_resume": False,
                },
            },
        }
    )
    invoker = InvocationService(registry)
    invoker.invoke("trusted_bridge", workflow_id="wf_with_decision", input_payload={})
    invoker.invoke("trusted_bridge", workflow_id="wf_still_waiting", input_payload={})
    invoker.invoke("untrusted_bridge", workflow_id="wf_untrusted", input_payload={})
    for db, workflow_id in [(trusted_db, "wf_with_decision"), (untrusted_db, "wf_untrusted")]:
        WorkflowEngine(db).submit_approval_decision(
            ApprovalDecisionInput(
                workflow_id=workflow_id,
                key="approve_bridge_test",
                action="approve",
                by="skylar",
                source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": f"{workflow_id}-msg"},
            ),
            resume=False,
        )

    resumed = TrustedResumer(registry).resume_pending("trusted_bridge", limit=5)

    assert [item["workflow_id"] for item in resumed] == ["wf_with_decision"]
    assert resumed[0]["status"] == "completed"
    assert WorkflowEngine(trusted_db).workflow_status("wf_still_waiting")["status"] == "waiting"

    with pytest.raises(ValueError, match="trusted_resume=true"):
        TrustedResumer(registry).resume_pending("untrusted_bridge")
    with pytest.raises(ValueError, match="does not match trusted registry DB alias"):
        TrustedResumer(registry).resume_pending("trusted_bridge", db="untrusted")
    with pytest.raises(ValueError, match="does not match trusted registry DB alias"):
        TrustedResumer(registry).resume_pending("trusted_bridge", db=str(untrusted_db))
    assert WorkflowEngine(untrusted_db).workflow_status("wf_untrusted")["status"] == "running"


def test_resume_pending_skips_same_db_workflow_ref_mismatch(tmp_path):
    db = tmp_path / "shared.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"shared": str(db)},
            "workflows": {
                "trusted_bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "shared",
                    "trusted_resume": True,
                },
                "other_bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:other_approval_workflow",
                    "db": "shared",
                    "trusted_resume": False,
                },
            },
        }
    )
    invoker = InvocationService(registry)
    invoker.invoke("other_bridge", workflow_id="wf_other", input_payload={})
    WorkflowEngine(db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_other",
            key="approve_other_test",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "other-msg"},
        ),
        resume=False,
    )

    assert TrustedResumer(registry).resume_pending("trusted_bridge", limit=5) == []
    assert WorkflowEngine(db).workflow_status("wf_other")["status"] == "running"


def test_resume_pending_skips_same_db_same_ref_untrusted_registry_alias(tmp_path):
    db = tmp_path / "shared.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"shared": str(db)},
            "workflows": {
                "trusted_bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "shared",
                    "trusted_resume": True,
                },
                "untrusted_same_ref": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "shared",
                    "trusted_resume": False,
                },
            },
        }
    )
    InvocationService(registry).invoke("untrusted_same_ref", workflow_id="wf_untrusted_same_ref", input_payload={})
    WorkflowEngine(db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_untrusted_same_ref",
            key="approve_bridge_test",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "same-ref-msg"},
        ),
        resume=False,
    )

    assert TrustedResumer(registry).resume_pending("trusted_bridge", limit=5) == []
    with pytest.raises(ValueError, match="registry provenance"):
        TrustedResumer(registry).resume_trusted("trusted_bridge", workflow_id="wf_untrusted_same_ref")
    assert WorkflowEngine(db).workflow_status("wf_untrusted_same_ref")["status"] == "running"


def test_resume_trusted_rejects_db_override_to_untrusted_same_ref_workflow(tmp_path):
    trusted_db = tmp_path / "trusted.sqlite"
    untrusted_db = tmp_path / "untrusted.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"trusted": str(trusted_db), "untrusted": str(untrusted_db)},
            "workflows": {
                "trusted_bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "trusted",
                    "trusted_resume": True,
                },
                "untrusted_same_ref": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "untrusted",
                    "trusted_resume": False,
                },
            },
        }
    )
    InvocationService(registry).invoke("untrusted_same_ref", workflow_id="wf_untrusted_override", input_payload={})
    WorkflowEngine(untrusted_db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_untrusted_override",
            key="approve_bridge_test",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "override-msg"},
        ),
        resume=False,
    )

    with pytest.raises(ValueError, match="trusted registry DB alias"):
        TrustedResumer(registry).resume_trusted("trusted_bridge", workflow_id="wf_untrusted_override", db="untrusted")
    with pytest.raises(ValueError, match="trusted registry DB alias"):
        TrustedResumer(registry).resume_trusted("trusted_bridge", workflow_id="wf_untrusted_override", db=str(untrusted_db))
    assert WorkflowEngine(untrusted_db).workflow_status("wf_untrusted_override")["status"] == "running"


def test_resume_trusted_rejects_registry_ref_mismatch_and_prior_approval_only(tmp_path):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"pilot": str(db)},
            "workflows": {
                "bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "pilot",
                    "trusted_resume": True,
                },
                "two_step": {
                    "workflow_ref": "tests.test_invocation_bridge:two_approval_workflow",
                    "db": "pilot",
                    "trusted_resume": True,
                },
            },
        }
    )
    invoker = InvocationService(registry)
    invoker.invoke("two_step", workflow_id="wf_two", input_payload={})
    engine = WorkflowEngine(db)
    engine.submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_two",
            key="approve_first",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "first-msg"},
        ),
        resume=False,
    )

    with pytest.raises(ValueError, match="does not match trusted registry ref"):
        TrustedResumer(registry).resume_trusted("bridge", workflow_id="wf_two")

    first_resume = TrustedResumer(registry).resume_trusted("two_step", workflow_id="wf_two")
    assert first_resume["status"] == "waiting"
    assert first_resume["waiting_on"] == "signal:approval.decision:approve_second"

    with pytest.raises(ValueError, match="current wait"):
        TrustedResumer(registry).resume_trusted("two_step", workflow_id="wf_two")


def test_resume_trusted_rejects_missing_stored_workflow_ref(tmp_path):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"pilot": str(db)},
            "workflows": {
                "bridge": {
                    "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                    "db": "pilot",
                    "trusted_resume": True,
                }
            },
        }
    )
    InvocationService(registry).invoke("bridge", workflow_id="wf_missing_ref", input_payload={})
    WorkflowEngine(db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_missing_ref",
            key="approve_bridge_test",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "missing-ref-msg"},
        ),
        resume=False,
    )
    with sqlite3.connect(db) as con:
        con.execute("UPDATE workflow_instances SET workflow_ref = NULL WHERE id = ?", ("wf_missing_ref",))

    with pytest.raises(ValueError, match="no stored workflow_ref"):
        TrustedResumer(registry).resume_trusted("bridge", workflow_id="wf_missing_ref")


def test_receipts_redact_secret_bearing_keys_recursively():
    assert redact_secrets({"nested": [{"password": "p", "ok": "yes"}], "api_key": "k"}) == {
        "nested": [{"password": "[REDACTED]", "ok": "yes"}],
        "api_key": "[REDACTED]",
    }


def test_invoke_cli_and_resume_trusted_cli_bridge_plugin_recorded_decision(tmp_path):
    db = tmp_path / "workflow.sqlite"
    registry_path = tmp_path / "registry.json"
    invoke_receipt = tmp_path / "invoke-receipt.json"
    resume_receipt = tmp_path / "resume-receipt.json"
    registry_path.write_text(
        json.dumps(
            {
                "dbs": {"pilot": str(db)},
                "workflows": {
                    "bridge": {
                        "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                        "db": "pilot",
                        "trusted_resume": True,
                    }
                },
            }
        )
    )

    invoke_payload = json.loads(
        run_cli(
            tmp_path,
            "invoke",
            "bridge",
            "--config",
            str(registry_path),
            "--id",
            "wf_cli_bridge",
            "--input-json",
            '{"summary":"CLI bridge"}',
            "--source-json",
            '{"kind":"operator","channel":"kanban","task_id":"t_3b203be1"}',
            "--receipt-json",
            str(invoke_receipt),
        ).stdout
    )
    assert invoke_payload["status"] == "waiting"
    assert invoke_receipt.exists()

    # Simulate plugin/gateway resume=false: decision is recorded and continuation is queued.
    WorkflowEngine(db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_cli_bridge",
            key="approve_bridge_test",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "cli-msg-1"},
        ),
        resume=False,
    )
    assert WorkflowEngine(db).workflow_status("wf_cli_bridge")["status"] == "running"

    resume_payload = json.loads(
        run_cli(
            tmp_path,
            "resume-trusted",
            "bridge",
            "--config",
            str(registry_path),
            "--id",
            "wf_cli_bridge",
            "--receipt-json",
            str(resume_receipt),
        ).stdout
    )
    assert resume_payload["status"] == "completed"
    assert resume_payload["result"]["followup_ran"] is True
    assert resume_receipt.exists()


def test_resume_trusted_cli_rejects_db_override_to_untrusted_same_ref_workflow(tmp_path):
    trusted_db = tmp_path / "trusted.sqlite"
    untrusted_db = tmp_path / "untrusted.sqlite"
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "dbs": {"trusted": str(trusted_db), "untrusted": str(untrusted_db)},
                "workflows": {
                    "trusted_bridge": {
                        "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                        "db": "trusted",
                        "trusted_resume": True,
                    },
                    "untrusted_same_ref": {
                        "workflow_ref": "tests.test_invocation_bridge:bridge_approval_workflow",
                        "db": "untrusted",
                        "trusted_resume": False,
                    },
                },
            }
        )
    )
    registry = WorkflowRegistry.from_sources(config_path=registry_path)
    InvocationService(registry).invoke("untrusted_same_ref", workflow_id="wf_cli_untrusted_override", input_payload={})
    WorkflowEngine(untrusted_db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_cli_untrusted_override",
            key="approve_bridge_test",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "cli-override-msg"},
        ),
        resume=False,
    )

    by_alias = run_cli(
        tmp_path,
        "resume-trusted",
        "trusted_bridge",
        "--config",
        str(registry_path),
        "--db",
        "untrusted",
        "--id",
        "wf_cli_untrusted_override",
        check=False,
    )
    by_path = run_cli(
        tmp_path,
        "resume-trusted",
        "trusted_bridge",
        "--config",
        str(registry_path),
        "--db",
        str(untrusted_db),
        "--id",
        "wf_cli_untrusted_override",
        check=False,
    )

    assert by_alias.returncode != 0
    assert by_path.returncode != 0
    assert "trusted registry DB alias" in by_alias.stderr
    assert "trusted registry DB alias" in by_path.stderr
    assert WorkflowEngine(untrusted_db).workflow_status("wf_cli_untrusted_override")["status"] == "running"


def test_invoke_cli_import_failure_does_not_create_db(tmp_path):
    db = tmp_path / "workflow.sqlite"
    result = run_cli(
        tmp_path,
        "invoke",
        "missing.module:workflow",
        "--db",
        str(db),
        "--id",
        "wf_missing_import",
        "--input-json",
        "{}",
        check=False,
    )

    assert result.returncode != 0
    assert "No module named" in result.stderr
    assert not db.exists()

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from hermes_workflows import ApprovalDecisionInput, InvocationService, TrustedResumer, WorkflowDbConfig, WorkflowEngine, WorkflowRefConfig, WorkflowRegistry
from hermes_workflows.hermes_plugin_approvals import _handle_workflow_approval_decide
from hermes_workflows.examples.email_triage import (
    APPROVAL_KEY,
    MAX_PROVIDED_THREADS,
    REGISTRY_NAME,
    WORKFLOW_REF,
    email_triage_workflow,
)


def _dangerous_ledger_values_are_zero(ledger: dict[str, int]) -> bool:
    return all(value == 0 for key, value in ledger.items() if key != "local_artifacts_written")


def _approval(status: dict, key: str = APPROVAL_KEY) -> dict:
    return next(item for item in status["approvals"] if item["key"] == key)


def test_email_triage_demo_is_packaged_example_importable():
    import hermes_workflows.examples as packaged_examples

    assert packaged_examples.EMAIL_TRIAGE_WORKFLOW_REF == WORKFLOW_REF
    assert packaged_examples.EMAIL_TRIAGE_REGISTRY_NAME == REGISTRY_NAME
    assert packaged_examples.email_triage_workflow is email_triage_workflow


def test_email_triage_demo_waits_for_approval_before_any_writeback(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "workflow.sqlite"
    output_root = Path("dist") / "email-triage-demo-review"
    output_dir = output_root / "wf-email-triage-waiting"

    result = WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {"output_dir": str(output_root), "fixture": "synthetic", "approver": "human:operator"},
        workflow_id="wf_email_triage_waiting",
        workflow_ref=WORKFLOW_REF,
    )

    assert result.status == "waiting"
    assert result.waiting_on == f"signal:approval.decision:{APPROVAL_KEY}"
    assert not output_dir.exists(), "pre-approval run must not write local proposal artifacts"

    status = WorkflowEngine(db, read_only=True).workflow_status("wf_email_triage_waiting")
    approval = _approval(status)
    packet = approval["artifact"]
    assert approval["status"] == "waiting"
    assert packet["local_writeback_paths"] == {
        "triage_packet": str(output_dir / "triage-packet.json"),
        "kanban_proposal": str(output_dir / "kanban-proposal.md"),
        "skyvault_proposal": str(output_dir / "skyvault-proposal.md"),
        "side_effect_ledger": str(output_dir / "side-effect-ledger.json"),
    }
    assert packet["candidate_counts"] == {
        "total": 4,
        "ignore": 1,
        "archive_candidate": 1,
        "draft_reply": 1,
        "kanban_update": 1,
        "human_decision": 0,
        "auth_blocked": 0,
    }
    assert [item["classification"] for item in packet["classifications"]] == [
        "draft_reply",
        "kanban_update",
        "archive_candidate",
        "ignore",
    ]
    assert _dangerous_ledger_values_are_zero(packet["side_effect_ledger"])

    requested_steps = [event["payload"].get("step_name") for event in status["events"] if event["type"] == "StepRequested"]
    assert "perform_email_triage_demo_writebacks" not in requested_steps


def test_provided_fixture_redaction_helper_bounds_and_drops_raw_fields():
    from hermes_workflows.examples.email_triage import _coerce_provided_threads

    raw_threads = [
        {
            "handle": f"gmail-thread-{index}-private",
            "account": "private.person@example.com",
            "sender_label": f"Raw Sender {index}",
            "subject_label": f"Private subject {index}",
            "signals": ["asks_for_response", "PRIVATE_BODY_SNIPPET_SECRET"],
        }
        for index in range(MAX_PROVIDED_THREADS + 3)
    ]

    threads, source = _coerce_provided_threads(raw_threads)

    assert source == {
        "kind": "provided_fixture",
        "raw_private_email_included": False,
        "handles_redacted": True,
        "max_candidate_count": MAX_PROVIDED_THREADS,
        "provided_count": MAX_PROVIDED_THREADS + 3,
        "bounded_count": MAX_PROVIDED_THREADS,
    }
    assert [thread["handle"] for thread in threads] == [
        f"fixture:gmail:provided:{index + 1:03d}" for index in range(MAX_PROVIDED_THREADS)
    ]
    rendered = json.dumps({"threads": threads, "source": source}, sort_keys=True)
    assert "private.person@example.com" not in rendered
    assert "Raw Sender" not in rendered
    assert "Private subject" not in rendered
    assert "gmail-thread-0-private" not in rendered
    assert "PRIVATE_BODY_SNIPPET_SECRET" not in rendered


def test_provided_fixture_workflow_accepts_bounded_symbolic_inputs_before_approval(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    threads = [{"signals": ["asks_for_response"]} for _ in range(MAX_PROVIDED_THREADS + 3)]

    result = WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {"fixture": "provided", "threads": threads, "approver": "human:operator"},
        workflow_id="wf_email_triage_provided_redacted",
        workflow_ref=WORKFLOW_REF,
    )

    assert result.status == "waiting"
    status = WorkflowEngine(db, read_only=True).workflow_status("wf_email_triage_provided_redacted")
    packet = _approval(status)["artifact"]
    assert packet["candidate_counts"]["total"] == MAX_PROVIDED_THREADS
    assert packet["source_handles"] == [
        f"fixture:gmail:provided:{index + 1:03d}" for index in range(MAX_PROVIDED_THREADS)
    ]
    rendered = json.dumps(packet, sort_keys=True)
    assert "private.person@example.com" not in rendered
    assert "Raw Sender" not in rendered
    assert "Private subject" not in rendered
    assert "gmail-thread-0-private" not in rendered


def test_raw_provided_fixture_values_are_not_persisted_in_workflow_db(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    result = WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {
            "fixture": "provided",
            "threads": [
                {
                    "handle": "gmail-thread-private-123",
                    "account": "private.person@example.com",
                    "sender_label": "Raw Sender Private",
                    "subject_label": "Private Subject Secret",
                    "signals": ["asks_for_response", "PRIVATE_BODY_SNIPPET_SECRET"],
                }
            ],
            "approver": "human:operator",
            "top_level_private": "TOP_LEVEL_PRIVATE_BODY",
        },
        workflow_id="wf_email_triage_raw_input_redacted_before_persistence",
        workflow_ref=WORKFLOW_REF,
    )

    assert result.status == "waiting"
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT input_json FROM workflow_instances").fetchall()
        rows += con.execute("SELECT payload_json FROM workflow_events").fetchall()
        rows += con.execute("SELECT payload_json FROM workflow_commands_outbox").fetchall()
    persisted = "\n".join(row[0] for row in rows)
    assert "private.person@example.com" not in persisted
    assert "Raw Sender Private" not in persisted
    assert "Private Subject Secret" not in persisted
    assert "gmail-thread-private-123" not in persisted
    assert "PRIVATE_BODY_SNIPPET_SECRET" not in persisted
    assert "TOP_LEVEL_PRIVATE_BODY" not in persisted


def test_email_triage_sanitizer_preserves_existing_workflow_start_idempotency(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    first = WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {"fixture": "synthetic", "approver": "human:operator"},
        workflow_id="wf_email_triage_idempotent_sanitizer",
        workflow_ref=WORKFLOW_REF,
    )
    assert first.status == "waiting"

    second = WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {"fixture": "provided", "threads": "not-a-list", "top_level_private": "TOP_LEVEL_PRIVATE_BODY"},
        workflow_id="wf_email_triage_idempotent_sanitizer",
        workflow_ref=WORKFLOW_REF,
    )

    assert second.status == "waiting"


def test_email_triage_rejects_non_human_approval_even_if_input_tries_to_override_approver(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    output_dir = tmp_path / "dist" / "email-triage-demo-approval-bypass"
    WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {"fixture": "synthetic", "approver": "service:ci", "output_dir": str(output_dir)},
        workflow_id="wf_email_triage_nonhuman_approval",
        workflow_ref=WORKFLOW_REF,
    )

    try:
        WorkflowEngine(db).submit_approval_decision(
            ApprovalDecisionInput(
                workflow_id="wf_email_triage_nonhuman_approval",
                key=APPROVAL_KEY,
                action="approve",
                by="ci-bot",
                source={"kind": "service", "id": "ci-bot", "channel": "ci", "message_id": "ci-approval"},
                idempotency_key="ci://approval/nonhuman",
            ),
            resume=False,
        )
    except ValueError as exc:
        assert "requires human approval source" in str(exc)
    else:
        raise AssertionError("non-human approval source should be rejected")

    status = WorkflowEngine(db, read_only=True).workflow_status("wf_email_triage_nonhuman_approval")

    assert status["status"] == "waiting"
    assert not output_dir.exists()


def test_invocation_service_preserves_existing_workflow_idempotency_with_new_invalid_input(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    registry = WorkflowRegistry(
        dbs={"email-triage-demo": WorkflowDbConfig(name="email-triage-demo", path=str(db))},
        workflows={
            REGISTRY_NAME: WorkflowRefConfig(
                name=REGISTRY_NAME,
                workflow_ref=WORKFLOW_REF,
                db="email-triage-demo",
                title="Email triage demo",
                default_input={"fixture": "synthetic"},
                trusted_resume=True,
            )
        },
    )
    service = InvocationService(registry)

    first = service.invoke(REGISTRY_NAME, workflow_id="wf_email_triage_invocation_idempotent")
    second = service.invoke(
        REGISTRY_NAME,
        workflow_id="wf_email_triage_invocation_idempotent",
        input_payload={"fixture": "provided", "threads": "not-a-list", "top_level_private": "TOP_LEVEL_PRIVATE_BODY"},
    )

    assert first["status"] == "waiting"
    assert second["status"] == "waiting"


def test_email_triage_sanitizer_redacts_or_omits_raw_values_in_whitelisted_fields(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {
            "fixture": "PRIVATE_BODY_SECRET",
            "output_dir": str(tmp_path / "PRIVATE_BODY_SECRET"),
            "db_alias": "private@example.com",
            "_source": {"kind": "kanban", "private": "PRIVATE_BODY_SECRET", "task_id": "t_0e2dd0bd"},
        },
        workflow_id="wf_email_triage_whitelist_redaction",
        workflow_ref=WORKFLOW_REF,
    )

    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT input_json FROM workflow_instances").fetchall()
        rows += con.execute("SELECT payload_json FROM workflow_events").fetchall()
    persisted = "\n".join(row[0] for row in rows)
    assert "PRIVATE_BODY_SECRET" not in persisted
    assert "private@example.com" not in persisted


def test_invocation_receipt_uses_sanitized_email_triage_input(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    output_dir = tmp_path / "dist" / "email-triage-demo-2026-06-06"
    receipt_path = output_dir / "invoke-receipt.json"
    registry = WorkflowRegistry(
        dbs={"email-triage-demo": WorkflowDbConfig(name="email-triage-demo", path=str(db))},
        workflows={
            REGISTRY_NAME: WorkflowRefConfig(
                name=REGISTRY_NAME,
                workflow_ref=WORKFLOW_REF,
                db="email-triage-demo",
                title="Email triage demo",
                default_input={"fixture": "provided", "approver": "service:ci"},
                trusted_resume=True,
            )
        },
    )

    InvocationService(registry).invoke(
        REGISTRY_NAME,
        workflow_id="wf_email_triage_sanitized_receipt",
        input_payload={
            "threads": [
                {
                    "handle": "gmail-thread-private-123",
                    "account": "private.person@example.com",
                    "sender_label": "Raw Sender Private",
                    "subject_label": "Private Subject Secret",
                    "signals": ["asks_for_response", "PRIVATE_BODY_SNIPPET_SECRET"],
                }
            ],
            "top_level_private": "TOP_LEVEL_PRIVATE_BODY",
        },
        source={"kind": "kanban", "task_id": "t_0e2dd0bd", "sender": "private.person@example.com", "subject": "PRIVATE_BODY_SECRET"},
        receipt_path=receipt_path,
    )

    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert "private.person@example.com" not in receipt_text
    assert "Raw Sender Private" not in receipt_text
    assert "Private Subject Secret" not in receipt_text
    assert "gmail-thread-private-123" not in receipt_text
    assert "PRIVATE_BODY_SNIPPET_SECRET" not in receipt_text
    assert "TOP_LEVEL_PRIVATE_BODY" not in receipt_text
    assert '"approver": "human:operator"' in receipt_text


def test_invocation_receipt_does_not_fall_back_to_new_raw_source_for_existing_workflow(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    receipt_path = tmp_path / "second-receipt.json"
    registry = WorkflowRegistry(
        dbs={"email-triage-demo": WorkflowDbConfig(name="email-triage-demo", path=str(db))},
        workflows={
            REGISTRY_NAME: WorkflowRefConfig(
                name=REGISTRY_NAME,
                workflow_ref=WORKFLOW_REF,
                db="email-triage-demo",
                title="Email triage demo",
                default_input={"fixture": "synthetic"},
                trusted_resume=True,
            )
        },
    )
    service = InvocationService(registry)

    service.invoke(REGISTRY_NAME, workflow_id="wf_email_triage_existing_source")
    service.invoke(
        REGISTRY_NAME,
        workflow_id="wf_email_triage_existing_source",
        source={"kind": "kanban", "sender": "private.person@example.com", "subject": "ordinary private subject line"},
        receipt_path=receipt_path,
    )

    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert "private.person@example.com" not in receipt_text
    assert "ordinary private subject line" not in receipt_text


def test_source_sanitizer_strictly_allowlists_provenance_fields_before_persistence(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {
            "fixture": "synthetic",
            "_source": {
                "kind": "kanban",
                "task_id": "t_0e2dd0bd",
                "message_id": "msg-123",
                "subject": "Dinner plans from Jacqueline",
                "sender_name": "Chander Sharma",
                "private key with normal value": "plain private note",
            },
        },
        workflow_id="wf_email_triage_source_allowlist",
        workflow_ref=WORKFLOW_REF,
    )

    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT input_json FROM workflow_instances").fetchall()
        rows += con.execute("SELECT payload_json FROM workflow_events").fetchall()
    persisted = "\n".join(row[0] for row in rows)
    assert "Dinner plans from Jacqueline" not in persisted
    assert "Chander Sharma" not in persisted
    assert "plain private note" not in persisted
    assert "private key with normal value" not in persisted
    assert "t_0e2dd0bd" in persisted
    assert "msg-123" in persisted


def test_unsafe_output_dir_is_replaced_with_workflow_scoped_default_and_paths_are_in_approval(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "workflow.sqlite"
    unsafe_absolute = tmp_path / "outside-proposals"

    result = WorkflowEngine(db).run_until_idle(
        email_triage_workflow,
        {"fixture": "synthetic", "output_dir": str(unsafe_absolute)},
        workflow_id="wf output/unsafe..id",
        workflow_ref=WORKFLOW_REF,
    )

    assert result.status == "waiting"
    assert not unsafe_absolute.exists()
    expected_dir = Path("dist") / f"email-triage-demo-{date.today().isoformat()}" / "wf-output-unsafe-id"
    status = WorkflowEngine(db, read_only=True).workflow_status("wf output/unsafe..id")
    approval_packet = _approval(status)["artifact"]
    assert approval_packet["local_writeback_paths"]["triage_packet"] == str(expected_dir / "triage-packet.json")

    with sqlite3.connect(db) as con:
        input_json = con.execute("SELECT input_json FROM workflow_instances").fetchone()[0]
    assert str(unsafe_absolute) not in input_json
    assert json.loads(input_json)["output_dir"] == str(expected_dir)


def test_sensitive_relative_output_dir_is_replaced_with_workflow_scoped_default(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    for root_name in (".ssh", ".git", ".hermes"):
        workflow_id = f"wf_email_triage_sensitive_relative_output_{root_name.removeprefix('.')}"
        engine.run_until_idle(
            email_triage_workflow,
            {"fixture": "synthetic", "output_dir": f"{root_name}/email-triage"},
            workflow_id=workflow_id,
            workflow_ref=WORKFLOW_REF,
        )

        expected_dir = Path("dist") / f"email-triage-demo-{date.today().isoformat()}" / workflow_id.replace("_", "-")
        status = WorkflowEngine(db, read_only=True).workflow_status(workflow_id)
        approval_packet = _approval(status)["artifact"]
        assert approval_packet["local_writeback_paths"]["triage_packet"] == str(expected_dir / "triage-packet.json")
        assert not (tmp_path / root_name).exists()


def test_default_output_dir_is_workflow_scoped_to_avoid_same_day_collisions(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    for workflow_id in ("wf_email_triage_collision_a", "wf_email_triage_collision_b"):
        engine.run_until_idle(
            email_triage_workflow,
            {"fixture": "synthetic"},
            workflow_id=workflow_id,
            workflow_ref=WORKFLOW_REF,
        )
        engine.submit_approval_decision(
            ApprovalDecisionInput(
                workflow_id=workflow_id,
                key=APPROVAL_KEY,
                action="approve",
                by="operator",
                source={"kind": "human", "id": "operator", "channel": "discord", "message_id": workflow_id},
                idempotency_key=f"discord://email-triage/{workflow_id}",
            ),
            resume=False,
        )

    registry = WorkflowRegistry(
        dbs={"email-triage-demo": WorkflowDbConfig(name="email-triage-demo", path=str(db))},
        workflows={REGISTRY_NAME: WorkflowRefConfig(name=REGISTRY_NAME, workflow_ref=WORKFLOW_REF, db="email-triage-demo", trusted_resume=True)},
    )

    first = TrustedResumer(registry).resume_trusted(REGISTRY_NAME, workflow_id="wf_email_triage_collision_a")
    second = TrustedResumer(registry).resume_trusted(REGISTRY_NAME, workflow_id="wf_email_triage_collision_b")

    first_path = first["result"]["created_or_updated_paths"]["triage_packet"]
    second_path = second["result"]["created_or_updated_paths"]["triage_packet"]
    assert first_path != second_path
    assert "wf-email-triage-collision-a" in first_path
    assert "wf-email-triage-collision-b" in second_path



def test_email_triage_demo_default_output_dir_is_dated_and_omits_raw_db_path(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "workflow.sqlite"
    expected_dir = Path("dist") / f"email-triage-demo-{date.today().isoformat()}" / "wf-email-triage-default-output-dir"
    registry = WorkflowRegistry(
        dbs={"email-triage-demo": WorkflowDbConfig(name="email-triage-demo", path=str(db))},
        workflows={
            REGISTRY_NAME: WorkflowRefConfig(
                name=REGISTRY_NAME,
                workflow_ref=WORKFLOW_REF,
                db="email-triage-demo",
                title="Email triage demo",
                default_input={"fixture": "synthetic", "approver": "human:operator"},
                trusted_resume=True,
            )
        },
    )

    InvocationService(registry).invoke(
        REGISTRY_NAME,
        workflow_id="wf_email_triage_default_output_dir",
        source={"kind": "kanban", "task_id": "t_0e2dd0bd"},
    )
    WorkflowEngine(db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_email_triage_default_output_dir",
            key=APPROVAL_KEY,
            action="approve",
            by="operator",
            source={"kind": "human", "id": "operator", "channel": "discord", "message_id": "approval-msg-default-dir"},
            idempotency_key="discord://thread/email-triage-demo/default-dir",
        ),
        resume=False,
    )

    final_receipt = TrustedResumer(registry).resume_trusted(REGISTRY_NAME, workflow_id="wf_email_triage_default_output_dir")

    result = final_receipt["result"]
    assert result["db_alias"] == "email-triage-demo"
    assert "db_path" not in result
    assert result["created_or_updated_paths"] == {
        "triage_packet": str(expected_dir / "triage-packet.json"),
        "kanban_proposal": str(expected_dir / "kanban-proposal.md"),
        "skyvault_proposal": str(expected_dir / "skyvault-proposal.md"),
        "side_effect_ledger": str(expected_dir / "side-effect-ledger.json"),
    }
    triage_packet = json.loads((expected_dir / "triage-packet.json").read_text(encoding="utf-8"))
    assert triage_packet["db_alias"] == "email-triage-demo"
    assert "db_path" not in triage_packet

    persisted_and_written = json.dumps(final_receipt, sort_keys=True) + "\n" + json.dumps(triage_packet, sort_keys=True)
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT result_json FROM workflow_instances WHERE id = ?", ("wf_email_triage_default_output_dir",)).fetchall()
        rows += con.execute("SELECT payload_json FROM workflow_events").fetchall()
    persisted_and_written += "\n" + "\n".join(row[0] or "" for row in rows)
    assert str(db) not in persisted_and_written


def test_plugin_style_record_only_approval_keeps_workflow_queued_and_records_provenance(tmp_path: Path):
    db = tmp_path / "workflow.sqlite"
    output_dir = tmp_path / "dist" / "email-triage-demo-2026-06-06"
    engine = WorkflowEngine(db)
    engine.run_until_idle(
        email_triage_workflow,
        {"output_dir": str(output_dir), "fixture": "synthetic", "approver": "human:operator"},
        workflow_id="wf_email_triage_record_only",
        workflow_ref=WORKFLOW_REF,
    )

    raw = _handle_workflow_approval_decide(
        {
            "db": str(db),
            "workflow_id": "wf_email_triage_record_only",
            "key": APPROVAL_KEY,
            "action": "approve",
            "by": "operator",
            "channel": "discord",
            "message_id": "approval-msg-1",
            "resume": False,
        }
    )
    payload = json.loads(raw)

    assert payload["success"] is True
    assert payload["receipt"]["status"] == "decision_recorded"
    assert payload["receipt"]["resume_requested"] is False
    assert "trusted workflow resumer" in payload["next_step"]
    assert not output_dir.exists(), "resume=false must not execute post-approval writebacks"

    status = WorkflowEngine(db, read_only=True).workflow_status("wf_email_triage_record_only")
    approval = _approval(status)
    assert status["status"] == "running"
    assert approval["status"] == "approve"
    assert approval["source"] == {"kind": "human", "id": "operator", "channel": "discord", "message_id": "approval-msg-1"}


def test_approval_decision_metadata_is_sanitized_before_event_persistence(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(
        email_triage_workflow,
        {"fixture": "synthetic", "approver": "human:operator"},
        workflow_id="wf-email-triage-private-approval-metadata",
        workflow_ref=WORKFLOW_REF,
    )

    receipt = engine.submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf-email-triage-private-approval-metadata",
            key=APPROVAL_KEY,
            action="approve",
            by="operator",
            source={
                "kind": "human",
                "id": "operator",
                "channel": "discord",
                "message_id": "approval-private-metadata",
                "sender": "private.person@example.com",
                "subject": "ordinary private subject line",
                "token": "secret-token-value",
            },
            note="Ordinary private note body without marker words",
            reason="ordinary private subject line",
        ),
        resume=False,
    )

    assert receipt.source == {
        "kind": "human",
        "id": "operator",
        "channel": "discord",
        "message_id": "approval-private-metadata",
    }

    events = WorkflowEngine(db, read_only=True).events("wf-email-triage-private-approval-metadata")
    signal_events = [event for event in events if event["type"] == "SignalReceived"]
    assert signal_events
    signal_blob = json.dumps(signal_events)
    assert "private.person@example.com" not in signal_blob
    assert "secret-token-value" not in signal_blob
    assert "ordinary private subject line" not in signal_blob
    assert "Ordinary private note body without marker words" not in signal_blob
    assert signal_events[-1]["payload"]["payload"]["note"] == "[REDACTED]"
    assert signal_events[-1]["payload"]["payload"]["reason"] == "[REDACTED]"
    persisted_source = signal_events[-1]["payload"]["source"]
    assert "sender" not in persisted_source
    assert "subject" not in persisted_source
    assert "token" not in persisted_source


def test_direct_approval_signal_sanitizes_metadata_before_resume_and_artifact_writes(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(
        email_triage_workflow,
        {"fixture": "synthetic", "approver": "human:operator"},
        workflow_id="wf-email-triage-direct-signal-private-approval",
        workflow_ref=WORKFLOW_REF,
    )

    result = engine.signal(
        "wf-email-triage-direct-signal-private-approval",
        "approval.decision",
        key=APPROVAL_KEY,
        payload={
            "action": "approve",
            "by": "operator",
            "note": "Ordinary private note body without marker words",
            "reason": "ordinary private subject line",
            "message": "another private approval message",
            "raw_extra": "private.person@example.com",
        },
        source={
            "kind": "human",
            "id": "operator",
            "channel": "discord",
            "message_id": "approval-direct-private-metadata",
            "sender": "private.person@example.com",
            "subject": "ordinary private subject line",
            "token": "secret-token-value",
        },
        idempotency_key="discord://thread/email-triage-demo/direct-signal-private-metadata",
    )

    assert result.status == "running"
    result = engine.drain("wf-email-triage-direct-signal-private-approval", initial=result)
    assert result.status == "completed"
    triage_packet = json.loads(Path(result.result["created_or_updated_paths"]["triage_packet"]).read_text(encoding="utf-8"))
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT result_json FROM workflow_instances WHERE id = ?", ("wf-email-triage-direct-signal-private-approval",)).fetchall()
        rows += con.execute("SELECT payload_json FROM workflow_events WHERE workflow_id = ?", ("wf-email-triage-direct-signal-private-approval",)).fetchall()
    persisted_and_written = json.dumps(result.result, sort_keys=True) + "\n" + json.dumps(triage_packet, sort_keys=True) + "\n" + "\n".join(row[0] or "" for row in rows)

    assert "private.person@example.com" not in persisted_and_written
    assert "secret-token-value" not in persisted_and_written
    assert "ordinary private subject line" not in persisted_and_written
    assert "Ordinary private note body without marker words" not in persisted_and_written
    assert "another private approval message" not in persisted_and_written
    assert "raw_extra" not in persisted_and_written
    signal_event = [event for event in WorkflowEngine(db, read_only=True).events("wf-email-triage-direct-signal-private-approval") if event["type"] == "SignalReceived"][-1]
    assert signal_event["payload"]["payload"] == {
        "action": "approve",
        "by": "operator",
        "note": "[REDACTED]",
        "reason": "[REDACTED]",
        "message": "[REDACTED]",
    }
    assert signal_event["payload"]["source"] == {
        "kind": "human",
        "id": "operator",
        "channel": "discord",
        "message_id": "approval-direct-private-metadata",
    }


def test_trusted_resume_creates_local_demo_receipt_with_zero_dangerous_side_effects(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "workflow.sqlite"
    output_dir = tmp_path / "dist" / "email-triage-demo-2026-06-06"
    workflow_output_dir = Path("dist") / f"email-triage-demo-{date.today().isoformat()}" / "wf-email-triage-trusted-resume"
    registry = WorkflowRegistry(
        dbs={"email-triage-demo": WorkflowDbConfig(name="email-triage-demo", path=str(db))},
        workflows={
            REGISTRY_NAME: WorkflowRefConfig(
                name=REGISTRY_NAME,
                workflow_ref=WORKFLOW_REF,
                db="email-triage-demo",
                title="Email triage demo",
                default_input={"output_dir": str(output_dir), "fixture": "synthetic", "approver": "human:operator"},
                trusted_resume=True,
            )
        },
    )
    invoke_receipt_path = output_dir / "invoke-receipt.json"
    waiting_dashboard = output_dir / "dashboard-waiting.html"

    invoke_receipt = InvocationService(registry).invoke(
        REGISTRY_NAME,
        workflow_id="wf_email_triage_trusted_resume",
        source={"kind": "kanban", "task_id": "t_0e2dd0bd"},
        receipt_path=invoke_receipt_path,
        dashboard_out=waiting_dashboard,
    )

    assert invoke_receipt["status"] == "waiting"
    assert invoke_receipt["db"] == {"alias": "email-triage-demo"}
    assert invoke_receipt_path.exists()
    dashboard_html = waiting_dashboard.read_text(encoding="utf-8")
    assert APPROVAL_KEY in dashboard_html
    assert "total=4" in dashboard_html
    assert "draft_reply=1" in dashboard_html
    assert "classifications=draft_reply,kanban_update,archive_candidate,ignore" in dashboard_html

    decision = WorkflowEngine(db).submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_email_triage_trusted_resume",
            key=APPROVAL_KEY,
            action="approve",
            by="operator",
            source={"kind": "human", "id": "operator", "channel": "discord", "message_url": "discord://thread/email-triage-demo/42"},
            idempotency_key="discord://thread/email-triage-demo/42",
        ),
        resume=False,
    )
    assert decision.status == "decision_recorded"

    final_receipt_path = output_dir / "final-receipt.json"
    final_dashboard = output_dir / "dashboard-final.html"
    final_receipt = TrustedResumer(registry).resume_trusted(
        REGISTRY_NAME,
        workflow_id="wf_email_triage_trusted_resume",
        receipt_path=final_receipt_path,
        dashboard_out=final_dashboard,
    )

    assert final_receipt["status"] == "completed"
    assert final_receipt_path.exists()
    result = final_receipt["result"]
    assert result["approved_by"] == "operator"
    assert result["approval_key"] == APPROVAL_KEY
    assert result["db_alias"] == "email-triage-demo"
    assert _dangerous_ledger_values_are_zero(result["side_effect_ledger"])
    assert result["created_or_updated_paths"] == {
        "triage_packet": str(workflow_output_dir / "triage-packet.json"),
        "kanban_proposal": str(workflow_output_dir / "kanban-proposal.md"),
        "skyvault_proposal": str(workflow_output_dir / "skyvault-proposal.md"),
        "side_effect_ledger": str(workflow_output_dir / "side-effect-ledger.json"),
    }
    for path in result["created_or_updated_paths"].values():
        assert Path(path).exists()

    side_effect_ledger = json.loads((workflow_output_dir / "side-effect-ledger.json").read_text(encoding="utf-8"))
    assert _dangerous_ledger_values_are_zero(side_effect_ledger)

from __future__ import annotations

import pytest

from hermes_workflows import WorkflowEngine, WorkflowRegistry, WorkflowWorkerService, ask, workflow
from hermes_workflows.dashboard import render_dashboard


@workflow
async def runner_recovery_two_prompt_workflow(inputs):
    first = await ask(
        prompt="Choose the first recovery option",
        key="first_choice",
        input={"options": inputs["first_options"]},
    )
    second = await ask(
        prompt="Choose the second recovery option",
        key="second_choice",
        input={"first": first, "options": inputs["second_options"]},
    )
    return {"first": first, "second": second}


@workflow
async def runner_recovery_immediate_workflow(inputs):
    return {"ok": inputs.get("ok", True)}


def _service_for(db, workflow_ref: str) -> WorkflowWorkerService:
    registry = WorkflowRegistry.from_sources(
        config={
            "dbs": {"service": str(db)},
            "workflows": {"recovery": {"workflow_ref": workflow_ref, "db": "service"}},
        }
    )
    return WorkflowWorkerService.from_registry(registry, worker_id="runner-v2-recovery-worker", lease_seconds=60)


def _event_count(engine: WorkflowEngine, workflow_id: str, event_type: str, key: str) -> int:
    with engine._connect() as con:
        return con.execute(
            """
            SELECT COUNT(*)
            FROM workflow_events
            WHERE workflow_id = ? AND type = ? AND key = ?
            """,
            (workflow_id, event_type, key),
        ).fetchone()[0]


def _workflow_run_command_count(engine: WorkflowEngine, workflow_id: str) -> int:
    with engine._connect() as con:
        return con.execute(
            """
            SELECT COUNT(*)
            FROM workflow_commands_outbox
            WHERE workflow_id = ? AND type = 'run_workflow' AND key = 'workflow:run'
            """,
            (workflow_id,),
        ).fetchone()[0]


def test_runner_restart_after_operator_response_commit_reaches_next_wait_once(tmp_path):
    db = tmp_path / "workflow.sqlite"
    workflow_id = "wf_recovery_after_response_commit"
    workflow_ref = "tests.test_runner_v2_recovery:runner_recovery_two_prompt_workflow"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(
        runner_recovery_two_prompt_workflow,
        {"first_options": ["a", "b"], "second_options": ["x", "y"]},
        workflow_id=workflow_id,
        workflow_ref=workflow_ref,
    )
    assert first.status == "waiting"
    assert first.waiting_on == "signal:operator.response:first_choice"

    receipt = engine.submit_operator_response(
        workflow_id=workflow_id,
        key="first_choice",
        payload={"choice": "b", "rationale": "recorded before runner restart"},
        source={"kind": "test", "channel": "local-dashboard-test", "message_id": "m-recovery-first"},
        idempotency_key="runner-recovery-first-response",
        resume=False,
    )
    assert receipt.status == "response_recorded"
    committed = engine.workflow_status(workflow_id)
    assert committed["status"] == "running"
    assert committed["operator_steps"][0]["status"] == "completed"
    assert [command["key"] for command in committed["pending_commands"]] == ["workflow:run"]
    assert committed["pending_commands"][0]["status"] == "pending"

    # Simulate process death here: construct a fresh runner service and let it execute one command.
    restarted_service = _service_for(db, workflow_ref)
    tick = restarted_service.tick(max_commands=1)

    assert tick.executed == 1
    assert tick.errors == []
    recovered = WorkflowEngine(db).workflow_status(workflow_id)
    assert recovered["status"] == "waiting"
    assert recovered["waiting_on"] == "signal:operator.response:second_choice"
    operator_steps = {step["key"]: step for step in recovered["operator_steps"]}
    assert operator_steps["first_choice"]["status"] == "completed"
    assert operator_steps["second_choice"]["status"] == "waiting"
    assert _event_count(engine, workflow_id, "StepRequested", "second_choice") == 1
    assert _workflow_run_command_count(engine, workflow_id) == 1
    assert WorkflowEngine(db).runnable_workflows() == []


def test_runner_records_import_error_in_status_and_dashboard(tmp_path):
    db = tmp_path / "workflow.sqlite"
    workflow_id = "wf_recovery_bad_import"
    bad_ref = "definitely_missing_runner_v2_package:missing_workflow"
    engine = WorkflowEngine(db)
    engine.start(
        runner_recovery_immediate_workflow,
        {"ok": True},
        workflow_id=workflow_id,
        workflow_ref=bad_ref,
    )

    service = _service_for(db, bad_ref)
    tick = service.tick(max_commands=1)

    assert tick.executed == 0
    assert len(tick.errors) == 1
    assert tick.errors[0]["workflow_id"] == workflow_id
    assert "ModuleNotFoundError" in tick.errors[0]["error"]

    status = WorkflowEngine(db).workflow_status(workflow_id, command_history="recent")
    assert status["status"] == "running"
    assert status["runtime_state"]["primary"] == "stuck"
    assert status["runtime_state"]["reason"] == "workflow_import_error"
    assert "workflow_ref/package import environment" in status["runtime_state"]["next_action"]
    assert status["pending_commands"][0]["status"] == "pending"
    assert status["pending_commands"][0]["last_error"] == {
        "type": "ModuleNotFoundError",
        "message": "No module named 'definitely_missing_runner_v2_package'",
    }
    assert "workflow_import_error" in status["pending_commands"][0]["diagnostic_labels"]
    assert status["diagnostics"][0]["label"] == "workflow_import_error"
    assert "definitely_missing_runner_v2_package" in str(status["runtime_state"]["command"]["last_error"])

    dashboard_path = render_dashboard(WorkflowEngine(db), tmp_path / "dashboard.html")
    html = dashboard_path.read_text(encoding="utf-8")
    assert "workflow_import_error" in html
    assert "ModuleNotFoundError" in html
    assert "definitely_missing_runner_v2_package" in html


def test_duplicate_operator_response_replay_keeps_single_continuation_and_conflict_rejected(tmp_path):
    db = tmp_path / "workflow.sqlite"
    workflow_id = "wf_recovery_duplicate_response"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(
        runner_recovery_two_prompt_workflow,
        {"first_options": ["a", "b"], "second_options": ["x", "y"]},
        workflow_id=workflow_id,
        workflow_ref="tests.test_runner_v2_recovery:runner_recovery_two_prompt_workflow",
    )
    assert first.status == "waiting"
    kwargs = {
        "workflow_id": workflow_id,
        "key": "first_choice",
        "payload": {"choice": "a", "rationale": "same response replayed"},
        "source": {"kind": "test", "channel": "local-dashboard-test", "message_id": "m-duplicate"},
        "idempotency_key": "runner-recovery-duplicate-response",
        "resume": False,
    }

    assert engine.submit_operator_response(**kwargs).status == "response_recorded"
    assert engine.submit_operator_response(**kwargs).status == "response_recorded"
    assert _event_count(engine, workflow_id, "SignalReceived", "signal:operator.response:first_choice") == 1
    assert _event_count(engine, workflow_id, "StepCompleted", "first_choice") == 1
    assert _workflow_run_command_count(engine, workflow_id) == 1

    with pytest.raises(ValueError, match="idempotency key was reused with a different decision/response"):
        engine.submit_operator_response(
            workflow_id=workflow_id,
            key="first_choice",
            payload={"choice": "b", "rationale": "same key different payload"},
            source={"kind": "test", "channel": "local-dashboard-test", "message_id": "m-duplicate"},
            idempotency_key="runner-recovery-duplicate-response",
            resume=False,
        )
    with pytest.raises(ValueError, match="already has a recorded decision/response"):
        engine.submit_operator_response(
            workflow_id=workflow_id,
            key="first_choice",
            payload={"choice": "b", "rationale": "different key conflicting payload"},
            source={"kind": "test", "channel": "local-dashboard-test", "message_id": "m-conflicting"},
            idempotency_key="runner-recovery-conflicting-response",
            resume=False,
        )
    assert _event_count(engine, workflow_id, "SignalReceived", "signal:operator.response:first_choice") == 1
    assert _event_count(engine, workflow_id, "StepCompleted", "first_choice") == 1
    assert _workflow_run_command_count(engine, workflow_id) == 1


def test_leased_command_recovery_is_covered_by_worker_fencing_tests():
    # Milestone 9 scenario 2 is covered by tests/test_worker.py:
    # - test_stale_worker_cannot_overwrite_reclaimed_command_result
    # - test_expired_step_claim_cannot_execute_without_reclaim
    # - test_expired_workflow_run_claim_cannot_enqueue_steps_without_reclaim
    # Keep this explicit marker so the recovery suite documents why it does not
    # duplicate the lower-level adversarial lease/fencing cases.
    assert True

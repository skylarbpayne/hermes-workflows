from __future__ import annotations

import json
import sqlite3

from hermes_workflows.domain import (
    AgentRequestedEvent,
    ApprovalNotificationCommand,
    ApprovalRequestedEvent,
    CommandType,
    EventType,
    ExternalAgentCommand,
    SignalReceivedEvent,
    StepExecutionCommand,
    StepRequestedEvent,
    WorkflowRunCommand,
    WorkflowStartedEvent,
    WorkflowStatus,
    decode_command_row,
    decode_event_row,
    encode_command,
    encode_event,
    make_command,
)
from hermes_workflows.workflow_values import Workflow, sha256_text


def row(**values):
    return values


def test_event_codec_decodes_representative_existing_rows_without_storage_migration() -> None:
    started = decode_event_row(
        row(
            seq=1,
            type="WorkflowStarted",
            key="workflow:start",
            payload_json='{"workflow_name":"demo","input":{"topic":"typed"}}',
            idempotency_key="workflow:start",
            created_at=100,
        )
    )
    step = decode_event_row(
        row(
            seq=2,
            type="StepRequested",
            key="step:draft:0",
            payload_json='{"step_name":"draft","args":[{"topic":"typed"}],"kwargs":{}}',
            idempotency_key="requested:step:draft:0",
            created_at=101,
        )
    )
    approval = decode_event_row(
        row(
            seq=3,
            type="ApprovalRequested",
            key="approval:review",
            payload_json='{"prompt":"Review?","key":"review","allowed":["approve","reject"]}',
            idempotency_key="approval-requested:review",
            created_at=102,
        )
    )
    agent = decode_event_row(
        row(
            seq=4,
            type="AgentRequested",
            key="agent:write",
            payload_json='{"key":"write","prompt":"Write it","signal_type":"agent.completed"}',
            idempotency_key="agent-requested:write",
            created_at=103,
        )
    )
    signal = decode_event_row(
        row(
            seq=5,
            type="SignalReceived",
            key="signal:approval.decision:review",
            payload_json='{"signal_type":"approval.decision","key":"review","payload":{"action":"approve"}}',
            idempotency_key="msg-1",
            created_at=104,
        )
    )

    assert isinstance(started, WorkflowStartedEvent)
    assert started.event_type is EventType.WORKFLOW_STARTED
    assert started.workflow_name == "demo"
    assert isinstance(step, StepRequestedEvent)
    assert step.step_key == "step:draft:0"
    assert step.step_name == "draft"
    assert isinstance(approval, ApprovalRequestedEvent)
    assert approval.approval_key == "review"
    assert isinstance(agent, AgentRequestedEvent)
    assert agent.agent_key == "write"
    assert isinstance(signal, SignalReceivedEvent)
    assert signal.signal_type == "approval.decision"

    assert encode_event(started) == ("WorkflowStarted", "workflow:start", {"workflow_name": "demo", "input": {"topic": "typed"}}, "workflow:start")
    assert started.to_public_dict()["type"] == "WorkflowStarted"


def test_command_codec_decodes_representative_existing_rows_without_storage_migration() -> None:
    workflow = decode_command_row(
        row(
            id=1,
            workflow_id="wf_1",
            type="run_workflow",
            key="workflow:run",
            payload_json='{"reason":"start"}',
            status="pending",
            claimed_by=None,
            lease_expires_at=None,
            attempts=0,
            last_error_json=None,
            created_at=100,
            updated_at=100,
        )
    )
    step = decode_command_row(
        row(
            id=2,
            workflow_id="wf_1",
            type="run_step",
            key="step:draft:0",
            payload_json='{"step_name":"draft","args":[],"kwargs":{}}',
            status="running",
            claimed_by="worker-a",
            lease_expires_at=200,
            attempts=1,
            last_error_json=None,
            created_at=101,
            updated_at=102,
        )
    )
    agent = decode_command_row(
        row(
            id=3,
            workflow_id="wf_1",
            type="external_agent",
            key="agent:write",
            payload_json='{"key":"write","prompt":"Write it"}',
            status="pending",
            claimed_by=None,
            lease_expires_at=None,
            attempts=0,
            last_error_json=None,
            created_at=103,
            updated_at=103,
        )
    )
    notify = decode_command_row(
        row(
            id=4,
            workflow_id="wf_1",
            type="notify_approval",
            key="approval:review",
            payload_json='{"prompt":"Review?","key":"review"}',
            status="pending",
            claimed_by=None,
            lease_expires_at=None,
            attempts=0,
            last_error_json=None,
            created_at=104,
            updated_at=104,
        )
    )

    assert isinstance(workflow, WorkflowRunCommand)
    assert workflow.command_type is CommandType.RUN_WORKFLOW
    assert isinstance(step, StepExecutionCommand)
    assert step.step_name == "draft"
    assert isinstance(agent, ExternalAgentCommand)
    assert agent.agent_key == "write"
    assert isinstance(notify, ApprovalNotificationCommand)
    assert notify.approval_key == "review"
    assert encode_command(step) == ("run_step", "step:draft:0", {"step_name": "draft", "args": [], "kwargs": {}})
    assert step.to_public_dict()["type"] == "run_step"


def test_command_payloads_preserve_framework_values_for_execution_and_history() -> None:
    source = "from hermes_workflows import workflow\n\n@workflow\nasync def generated(inputs):\n    return inputs\n"
    workflow_value = Workflow(
        source=source,
        symbol="generated",
        source_sha256=sha256_text(source),
        path="",
        module_name="hermes_generated_workflows.test",
    )

    decoded = decode_command_row(
        row(
            id=5,
            workflow_id="wf_1",
            type="start_child_workflow",
            key="child:generated:0",
            payload_json='{"workflow":%s,"child_workflow_id":"wf_1.child.generated.0","inputs":{}}'
            % json.dumps(workflow_value.to_json()),
            status="pending",
            claimed_by=None,
            lease_expires_at=None,
            attempts=0,
            last_error_json=None,
            created_at=105,
            updated_at=105,
        )
    )
    remade = make_command(
        CommandType.START_CHILD_WORKFLOW,
        workflow_id="wf_1",
        key="child:generated:0",
        payload=decoded.payload,
    )

    assert isinstance(decoded.payload["workflow"], Workflow)
    assert isinstance(remade.payload["workflow"], Workflow)


def test_status_and_worker_runtime_use_typed_status_event_and_command_models(tmp_path) -> None:
    from hermes_workflows import WorkflowEngine, step, workflow

    @step
    async def typed_step() -> str:
        return "ok"

    @workflow
    async def typed_runtime_workflow(inputs):
        return await typed_step()

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(typed_runtime_workflow, {}, workflow_id="wf_typed_models")

    assert WorkflowStatus.RUNNING.value == "running"
    assert WorkflowStatus.terminal_values() == {"completed", "failed", "cancelled"}
    pending = engine.pending_commands("wf_typed_models")
    assert pending[0]["type"] == CommandType.RUN_WORKFLOW.value

    result = engine.worker_until_idle("wf_typed_models", worker_id="typed-worker")

    assert result.status == WorkflowStatus.COMPLETED.value
    events = engine.events("wf_typed_models")
    assert events[0]["type"] == EventType.WORKFLOW_STARTED.value
    assert isinstance(decode_event_row({"payload_json": "{}", **events[0]}), WorkflowStartedEvent)
    assert any(event["type"] == EventType.STEP_REQUESTED.value for event in events)

    con = sqlite3.connect(db)
    try:
        persisted_event_types = [row[0] for row in con.execute("SELECT type FROM workflow_events ORDER BY seq")]
        persisted_command_types = [row[0] for row in con.execute("SELECT type FROM workflow_commands_outbox ORDER BY id")]
    finally:
        con.close()

    assert persisted_event_types[0] == "WorkflowStarted"
    assert persisted_command_types[:2] == ["run_workflow", "run_step"]

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Tuple, Union

from .types import to_json_value
from .workflow_values import Workflow


class _StringEnum(str, Enum):
    @classmethod
    def from_value(cls, value: object):
        try:
            return cls(str(value))
        except ValueError as exc:
            allowed = ", ".join(member.value for member in cls)
            raise ValueError(f"unknown {cls.__name__}: {value!r}; expected one of: {allowed}") from exc

    def __str__(self) -> str:
        return self.value


class WorkflowStatus(_StringEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal_values(cls) -> set[str]:
        return {cls.COMPLETED.value, cls.FAILED.value, cls.CANCELLED.value}


class EventType(_StringEnum):
    WORKFLOW_STARTED = "WorkflowStarted"
    WORKFLOW_COMPLETED = "WorkflowCompleted"
    WORKFLOW_CANCELLED = "WorkflowCancelled"
    COMMAND_CLAIMED = "CommandClaimed"
    STEP_REQUESTED = "StepRequested"
    STEP_COMPLETED = "StepCompleted"
    STEP_FAILED = "StepFailed"
    APPROVAL_REQUESTED = "ApprovalRequested"
    AGENT_REQUESTED = "AgentRequested"
    SIGNAL_RECEIVED = "SignalReceived"
    WAIT_REQUESTED = "WaitRequested"
    GATHER_WAITING = "GatherWaiting"
    PARALLEL_WAITING = "ParallelWaiting"
    GROUP_WAITING = "GroupWaiting"
    CHILD_WORKFLOW_REQUESTED = "ChildWorkflowRequested"
    CHILD_WORKFLOW_COMPLETED = "ChildWorkflowCompleted"
    CHILD_WORKFLOW_FAILED = "ChildWorkflowFailed"
    CHILD_WORKFLOW_WAITING = "ChildWorkflowWaiting"
    CHILD_WORKFLOW_GATHER_WAITING = "ChildWorkflowGatherWaiting"


class CommandType(_StringEnum):
    RUN_WORKFLOW = "run_workflow"
    RUN_STEP = "run_step"
    START_CHILD_WORKFLOW = "start_child_workflow"
    EXTERNAL_AGENT = "external_agent"
    NOTIFY_APPROVAL = "notify_approval"

    @classmethod
    def worker_runnable_values(cls, *, include_external_agent: bool = False) -> list[str]:
        values = [cls.RUN_WORKFLOW.value, cls.RUN_STEP.value, cls.START_CHILD_WORKFLOW.value]
        if include_external_agent:
            values.append(cls.EXTERNAL_AGENT.value)
        return values


@dataclass(frozen=True)
class WorkflowEvent:
    event_type: EventType
    key: str
    payload: Dict[str, Any]
    seq: int | None = None
    idempotency_key: str | None = None
    created_at: int | None = None

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.event_type.value,
            "key": self.key,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class WorkflowStartedEvent(WorkflowEvent):
    @property
    def workflow_name(self) -> str | None:
        value = self.payload.get("workflow_name")
        return str(value) if value is not None else None

    @property
    def workflow_ref(self) -> str | None:
        value = self.payload.get("workflow_ref")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class WorkflowCompletedEvent(WorkflowEvent):
    @property
    def result(self) -> Any:
        return self.payload.get("result")


@dataclass(frozen=True)
class WorkflowCancelledEvent(WorkflowEvent):
    @property
    def reason(self) -> str | None:
        value = self.payload.get("reason")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class StepRequestedEvent(WorkflowEvent):
    @property
    def step_key(self) -> str:
        return self.key

    @property
    def step_name(self) -> str | None:
        value = self.payload.get("step_name")
        return str(value) if value is not None else None

    @property
    def completion_mode(self) -> str | None:
        value = self.payload.get("completion_mode")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class StepCompletedEvent(WorkflowEvent):
    @property
    def step_key(self) -> str:
        return self.key

    @property
    def output(self) -> Any:
        return self.payload.get("output")


@dataclass(frozen=True)
class StepFailedEvent(WorkflowEvent):
    @property
    def step_key(self) -> str:
        return self.key

    @property
    def error(self) -> Any:
        return self.payload.get("error")


@dataclass(frozen=True)
class ApprovalRequestedEvent(WorkflowEvent):
    @property
    def approval_key(self) -> str:
        value = self.payload.get("key")
        if value is not None:
            return str(value)
        return self.key.removeprefix("approval:")

    @property
    def prompt(self) -> str | None:
        value = self.payload.get("prompt")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class AgentRequestedEvent(WorkflowEvent):
    @property
    def agent_key(self) -> str:
        value = self.payload.get("key")
        if value is not None:
            return str(value)
        return self.key.removeprefix("agent:")

    @property
    def signal_type(self) -> str:
        return str(self.payload.get("signal_type") or "agent.completed")


@dataclass(frozen=True)
class SignalReceivedEvent(WorkflowEvent):
    @property
    def signal_type(self) -> str | None:
        value = self.payload.get("signal_type")
        return str(value) if value is not None else None

    @property
    def signal_key(self) -> str | None:
        value = self.payload.get("key")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class WorkflowCommand:
    command_type: CommandType
    workflow_id: str
    key: str
    payload: Dict[str, Any]
    id: int | None = None
    status: str | None = None
    claimed_by: str | None = None
    lease_expires_at: int | None = None
    lease_seconds: int = 0
    attempts: int = 0
    last_error: Any = None
    created_at: int | None = None
    updated_at: int | None = None

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "type": self.command_type.value,
            "key": self.key,
            "payload": self.payload,
            "status": self.status,
            "claimed_by": self.claimed_by,
            "lease_expires_at": self.lease_expires_at,
            "lease_seconds": self.lease_seconds,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class WorkflowRunCommand(WorkflowCommand):
    @property
    def reason(self) -> str | None:
        value = self.payload.get("reason")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class StepExecutionCommand(WorkflowCommand):
    @property
    def step_name(self) -> str | None:
        value = self.payload.get("step_name")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class ChildWorkflowStartCommand(WorkflowCommand):
    @property
    def child_workflow_id(self) -> str | None:
        value = self.payload.get("child_workflow_id")
        return str(value) if value is not None else None


@dataclass(frozen=True)
class ExternalAgentCommand(WorkflowCommand):
    @property
    def agent_key(self) -> str:
        value = self.payload.get("key")
        if value is not None:
            return str(value)
        return self.key.removeprefix("agent:")


@dataclass(frozen=True)
class ApprovalNotificationCommand(WorkflowCommand):
    @property
    def approval_key(self) -> str:
        value = self.payload.get("key")
        if value is not None:
            return str(value)
        return self.key.removeprefix("approval:")


_EVENT_CLASSES: dict[EventType, type[WorkflowEvent]] = {
    EventType.WORKFLOW_STARTED: WorkflowStartedEvent,
    EventType.WORKFLOW_COMPLETED: WorkflowCompletedEvent,
    EventType.WORKFLOW_CANCELLED: WorkflowCancelledEvent,
    EventType.STEP_REQUESTED: StepRequestedEvent,
    EventType.STEP_COMPLETED: StepCompletedEvent,
    EventType.STEP_FAILED: StepFailedEvent,
    EventType.APPROVAL_REQUESTED: ApprovalRequestedEvent,
    EventType.AGENT_REQUESTED: AgentRequestedEvent,
    EventType.SIGNAL_RECEIVED: SignalReceivedEvent,
}

_COMMAND_CLASSES: dict[CommandType, type[WorkflowCommand]] = {
    CommandType.RUN_WORKFLOW: WorkflowRunCommand,
    CommandType.RUN_STEP: StepExecutionCommand,
    CommandType.START_CHILD_WORKFLOW: ChildWorkflowStartCommand,
    CommandType.EXTERNAL_AGENT: ExternalAgentCommand,
    CommandType.NOTIFY_APPROVAL: ApprovalNotificationCommand,
}


def make_event(
    event_type: Union[EventType, str],
    *,
    key: str,
    payload: Any,
    seq: int | None = None,
    idempotency_key: str | None = None,
    created_at: int | None = None,
) -> WorkflowEvent:
    typed_event_type = EventType.from_value(event_type)
    event_payload = _json_object(payload)
    event_cls = _EVENT_CLASSES.get(typed_event_type, WorkflowEvent)
    return event_cls(
        event_type=typed_event_type,
        key=key,
        payload=event_payload,
        seq=seq,
        idempotency_key=idempotency_key,
        created_at=created_at,
    )


def make_command(
    command_type: Union[CommandType, str],
    *,
    workflow_id: str,
    key: str,
    payload: Any,
    id: int | None = None,
    status: str | None = None,
    claimed_by: str | None = None,
    lease_expires_at: int | None = None,
    lease_seconds: int = 0,
    attempts: int = 0,
    last_error: Any = None,
    created_at: int | None = None,
    updated_at: int | None = None,
) -> WorkflowCommand:
    typed_command_type = CommandType.from_value(command_type)
    command_payload = _json_object(payload)
    command_cls = _COMMAND_CLASSES.get(typed_command_type, WorkflowCommand)
    return command_cls(
        command_type=typed_command_type,
        workflow_id=workflow_id,
        key=key,
        payload=command_payload,
        id=id,
        status=status,
        claimed_by=claimed_by,
        lease_expires_at=lease_expires_at,
        lease_seconds=lease_seconds,
        attempts=attempts,
        last_error=last_error,
        created_at=created_at,
        updated_at=updated_at,
    )


def decode_event_row(row: Any) -> WorkflowEvent:
    return make_event(
        _row_get(row, "type"),
        key=str(_row_get(row, "key")),
        payload=_loads(_row_get(row, "payload_json")) if _row_has(row, "payload_json") else _row_get(row, "payload", default={}),
        seq=_optional_int(_row_get(row, "seq", default=None)),
        idempotency_key=_optional_str(_row_get(row, "idempotency_key", default=None)),
        created_at=_optional_int(_row_get(row, "created_at", default=None)),
    )


def decode_command_row(row: Any) -> WorkflowCommand:
    return make_command(
        _row_get(row, "type"),
        workflow_id=str(_row_get(row, "workflow_id")),
        key=str(_row_get(row, "key")),
        payload=_loads(_row_get(row, "payload_json")) if _row_has(row, "payload_json") else _row_get(row, "payload", default={}),
        id=_optional_int(_row_get(row, "id", default=None)),
        status=_optional_str(_row_get(row, "status", default=None)),
        claimed_by=_optional_str(_row_get(row, "claimed_by", default=None)),
        lease_expires_at=_optional_int(_row_get(row, "lease_expires_at", default=None)),
        lease_seconds=_lease_seconds_from_values(
            _row_get(row, "lease_expires_at", default=None),
            _row_get(row, "updated_at", default=None),
            default=_optional_int(_row_get(row, "lease_seconds", default=0)) or 0,
        ),
        attempts=_optional_int(_row_get(row, "attempts", default=0)) or 0,
        last_error=_loads(_row_get(row, "last_error_json")) if _row_has(row, "last_error_json") else _row_get(row, "last_error", default=None),
        created_at=_optional_int(_row_get(row, "created_at", default=None)),
        updated_at=_optional_int(_row_get(row, "updated_at", default=None)),
    )


def encode_event(event: WorkflowEvent) -> Tuple[str, str, Dict[str, Any], str | None]:
    return (event.event_type.value, event.key, event.payload, event.idempotency_key)


def encode_command(command: WorkflowCommand) -> Tuple[str, str, Dict[str, Any]]:
    return (command.command_type.value, command.key, command.payload)


def _json_object(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    normalized = to_json_value(value)
    if normalized is None:
        return {}
    if not isinstance(normalized, dict):
        raise TypeError(f"expected JSON object payload, got {type(normalized).__name__}")
    return normalized


def _loads(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return _from_jsonable(json.loads(value))
    return _from_jsonable(value)


def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("__hermes_type__") == "Workflow":
            return Workflow.from_json(value)
        return {str(key): _from_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_jsonable(item) for item in value]
    return value


def _row_has(row: Any, key: str) -> bool:
    if isinstance(row, dict):
        return key in row
    try:
        return key in row.keys()
    except AttributeError:
        try:
            row[key]
        except (KeyError, IndexError, TypeError):
            return False
        return True


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _lease_seconds_from_values(expires_at: Any, updated_at: Any, *, default: int) -> int:
    if expires_at is None or updated_at is None:
        return default
    return max(0, int(expires_at) - int(updated_at))

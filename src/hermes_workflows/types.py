from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING, Any, Protocol, Union, cast

if TYPE_CHECKING:
    from typing import TypeAlias

    JsonScalar: TypeAlias = Union[str, int, float, bool, None]
    JsonValue: TypeAlias = Union[JsonScalar, list["JsonValue"], dict[str, "JsonValue"]]
    JsonObject: TypeAlias = dict[str, JsonValue]
else:
    JsonScalar = Union[str, int, float, bool, None]
    JsonValue = Union[JsonScalar, list, dict]
    JsonObject = dict


class _WorkflowJsonValue(Protocol):
    def to_json(self) -> dict[str, Any]: ...


class _ApprovalDecisionJsonValue(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


def to_json_value(value: object) -> JsonValue:
    """Normalize framework payload values to JSON-compatible containers.

    This is the single boundary for values that will be persisted, hashed, or
    exposed through workflow API views. Workflow-specific parsing still belongs
    at typed input boundaries; this helper only guarantees JSON shape.
    """

    from .runtime_services import RuntimeOnlyServiceRegistry

    if isinstance(value, RuntimeOnlyServiceRegistry):
        raise TypeError("runtime service registries are process-local and cannot be serialized")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _is_framework_json_value(value):
        return to_json_value(cast(_WorkflowJsonValue, value).to_json())
    if _is_approval_decision(value):
        return to_json_value(cast(_ApprovalDecisionJsonValue, value).to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            _reject_runtime_service_registry(key)
            normalized[str(key)] = to_json_value(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_json_value(item) for item in value]
    raise TypeError(f"value of type {type(value).__name__} is not JSON-serializable")


def to_json_object(value: object) -> JsonObject:
    """Normalize a value and require the result to be a JSON object."""

    normalized = to_json_value(value)
    if not isinstance(normalized, dict):
        raise TypeError(f"expected JSON object, got {type(normalized).__name__}")
    return normalized


def _reject_runtime_service_registry(value: object) -> None:
    """Reject process-local registries nested in values about to become mapping keys."""

    from .runtime_services import RuntimeOnlyServiceRegistry

    if isinstance(value, RuntimeOnlyServiceRegistry):
        raise TypeError("runtime service registries are process-local and cannot be serialized")
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _reject_runtime_service_registry(getattr(value, field.name))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_runtime_service_registry(key)
            _reject_runtime_service_registry(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_runtime_service_registry(item)
    elif isinstance(value, Set):
        for item in value:
            _reject_runtime_service_registry(item)


def _is_framework_json_value(value: object) -> bool:
    try:
        from .workflow_values import Workflow
    except Exception:  # pragma: no cover - defensive for import cycles.
        Workflow = None  # type: ignore[assignment]
    try:
        from .artifacts import Artifact
    except Exception:  # pragma: no cover - defensive for import cycles.
        Artifact = None  # type: ignore[assignment]
    return (Workflow is not None and isinstance(value, Workflow)) or (Artifact is not None and isinstance(value, Artifact))


def _is_workflow_value(value: object) -> bool:
    return _is_framework_json_value(value)


def _is_approval_decision(value: object) -> bool:
    try:
        from .approvals import ApprovalDecision
    except Exception:  # pragma: no cover - defensive for import cycles.
        return False
    return isinstance(value, ApprovalDecision)

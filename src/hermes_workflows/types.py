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

    if _mapping_key_contains_registry_or_cycle(value, active_ids=set()):
        raise TypeError("cyclic mapping keys are not JSON-serializable")


def _mapping_key_contains_registry_or_cycle(value: object, *, active_ids: set[int]) -> bool:
    """Reject registries eagerly and report cycles after inspecting sibling values."""

    from .runtime_services import RuntimeOnlyServiceRegistry

    if isinstance(value, RuntimeOnlyServiceRegistry):
        raise TypeError("runtime service registries are process-local and cannot be serialized")

    is_dataclass_instance = is_dataclass(value) and not isinstance(value, type)
    is_mapping = isinstance(value, Mapping)
    is_sequence = isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    is_set = isinstance(value, Set)
    if not (is_dataclass_instance or is_mapping or is_sequence or is_set):
        return False

    value_id = id(value)
    if value_id in active_ids:
        return True

    active_ids.add(value_id)
    cycle_found = False
    try:
        if is_dataclass_instance:
            for field in fields(cast(Any, value)):
                if _mapping_key_contains_registry_or_cycle(getattr(value, field.name), active_ids=active_ids):
                    cycle_found = True
        if is_mapping:
            for key, item in cast(Mapping[object, object], value).items():
                if _mapping_key_contains_registry_or_cycle(key, active_ids=active_ids):
                    cycle_found = True
                if _mapping_key_contains_registry_or_cycle(item, active_ids=active_ids):
                    cycle_found = True
        if is_sequence:
            sequence = cast(Sequence[object], value)
            for index in range(len(sequence)):
                if _mapping_key_contains_registry_or_cycle(sequence[index], active_ids=active_ids):
                    cycle_found = True
        if is_set:
            for item in cast(Any, value):
                if _mapping_key_contains_registry_or_cycle(item, active_ids=active_ids):
                    cycle_found = True
    finally:
        active_ids.remove(value_id)
    return cycle_found


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

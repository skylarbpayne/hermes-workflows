from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NoReturn


REVISION_ACTION_SERVICE_ID = "revision.action.validator"
REVISION_ACTION_CONTRACT_VERSION = 1
_ACTIONABLE_MESSAGE = "request_changes requires nonblank feedback or valid edited_output"
_MAPPING_ERROR_MESSAGE = "payload could not be read safely"
_EDITED_JSON_ERROR_MESSAGE = "edited_output must be a valid bounded JSON value"
_PAYLOAD_JSON_ERROR_MESSAGE = "normalized revision action exceeds JSON limits"
_ALLOWED_FIELDS = frozenset({"action", "feedback", "edited_output"})
_MISSING = object()
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 10_000
_MAX_JSON_BYTES = 1_000_000
_MAX_INTEGER_DIGITS = 4_096
_MAX_INTEGER_BITS = 13_607
_MAX_PAYLOAD_ENTRIES = 100


@dataclass(frozen=True)
class RevisionActionFieldError:
    field: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


class RevisionActionValidationError(ValueError):
    def __init__(self, message: str, field_errors: tuple[RevisionActionFieldError, ...]) -> None:
        if not field_errors:
            raise ValueError("field_errors must not be empty")
        super().__init__(message)
        self.field_errors = field_errors

    def to_dict(self) -> dict[str, object]:
        return {
            "code": "revision_action_invalid",
            "message": str(self),
            "field_errors": [error.to_dict() for error in self.field_errors],
        }


@dataclass(frozen=True)
class ValidatedRevisionActionV1:
    action: str
    normalized_payload: Mapping[str, object]
    normalized_payload_hash: str
    idempotency_key: str
    schema_version: int = 1


@dataclass(frozen=True)
class RevisionActionValidatorV1:
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != REVISION_ACTION_CONTRACT_VERSION:
            raise ValueError("schema_version must equal 1")

    def validate(self, payload: Mapping[str, object]) -> ValidatedRevisionActionV1:
        return validate_revision_action(payload)


def validate_revision_action(payload: Mapping[str, object]) -> ValidatedRevisionActionV1:
    if not isinstance(payload, Mapping):
        _raise_invalid(_field_error("payload", "type", "payload must be an object"))

    try:
        snapshot = _snapshot_mapping(
            payload,
            context="payload",
            max_entries=_MAX_PAYLOAD_ENTRIES,
        )
    except Exception:
        _raise_invalid(_field_error("payload", "mapping", _MAPPING_ERROR_MESSAGE))

    if any(field not in _ALLOWED_FIELDS for field in snapshot):
        _raise_invalid(
            _field_error(
                "payload",
                "unknown",
                "payload contains unknown revision action fields",
            )
        )

    action_value = snapshot.get("action", _MISSING)
    if not isinstance(action_value, str):
        _raise_invalid(_field_error("action", "type", "action must be a string"))
    action = _trusted_string(action_value).strip()
    if action not in {"approve", "request_changes"}:
        _raise_invalid(
            _field_error("action", "choice", "action must be approve or request_changes")
        )

    feedback_value = snapshot.get("feedback", _MISSING)
    edited_value = snapshot.get("edited_output", _MISSING)

    if action == "approve":
        forbidden = []
        if feedback_value is not _MISSING:
            forbidden.append(
                _field_error("feedback", "forbidden", "feedback is not accepted for approve")
            )
        if edited_value is not _MISSING:
            forbidden.append(
                _field_error("edited_output", "forbidden", "edited_output is not accepted for approve")
            )
        if forbidden:
            _raise_invalid(*forbidden)
        return _validated({"action": action})

    feedback: str | None = None
    feedback_blank = feedback_value is _MISSING
    if feedback_value is not _MISSING:
        if not isinstance(feedback_value, str):
            _raise_invalid(
                _field_error("feedback", "type", "feedback must be a string when provided")
            )
        feedback = _trusted_string(feedback_value).strip()
        try:
            _validate_json_string(feedback)
        except ValueError as exc:
            _raise_invalid(_field_error("feedback", "json", str(exc)))
        feedback_blank = not feedback

    edited_output: object = _MISSING
    edited_blank = edited_value is _MISSING
    if edited_value is not _MISSING:
        if edited_value is None:
            _raise_invalid(
                _field_error("edited_output", "type", "edited_output must be a non-null JSON value")
            )
        try:
            edited_output = _normalize_json_value(edited_value)
        except Exception:
            _raise_invalid(_field_error("edited_output", "json", _EDITED_JSON_ERROR_MESSAGE))
        edited_blank = isinstance(edited_output, str) and not edited_output.strip()

    if feedback_blank and edited_blank:
        raise RevisionActionValidationError(
            _ACTIONABLE_MESSAGE,
            (
                _field_error("feedback", "actionable_required", _ACTIONABLE_MESSAGE),
                _field_error("edited_output", "actionable_required", _ACTIONABLE_MESSAGE),
            ),
        )

    normalized: dict[str, object] = {"action": action}
    if not feedback_blank:
        normalized["feedback"] = feedback
    if not edited_blank:
        normalized["edited_output"] = edited_output
    return _validated(normalized)


def _validated(normalized: dict[str, object]) -> ValidatedRevisionActionV1:
    try:
        canonical = _canonical_json_bytes(normalized)
    except Exception:
        _raise_invalid(_field_error("payload", "json", _PAYLOAD_JSON_ERROR_MESSAGE))
    payload_hash = hashlib.sha256(canonical).hexdigest()
    return ValidatedRevisionActionV1(
        action=str(normalized["action"]),
        normalized_payload=_freeze_json_object(normalized),
        normalized_payload_hash=payload_hash,
        idempotency_key=f"revision-action:v1:{payload_hash}",
    )


def _normalize_json_value(
    value: object,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
    budget: list[int] | None = None,
    byte_budget: list[int] | None = None,
) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"edited_output exceeds maximum JSON depth of {_MAX_JSON_DEPTH}")
    if seen is None:
        seen = set()
    if budget is None:
        budget = [0]
    if byte_budget is None:
        byte_budget = [0]
    budget[0] += 1
    if budget[0] > _MAX_JSON_NODES:
        raise ValueError(f"edited_output exceeds maximum JSON size of {_MAX_JSON_NODES} values")

    if value is None:
        _consume_json_bytes(byte_budget, 4)
        return value
    if isinstance(value, bool):
        _consume_json_bytes(byte_budget, 4 if value else 5)
        return value
    if isinstance(value, str):
        normalized_string = _trusted_string(value)
        _consume_json_bytes(byte_budget, _json_string_size(normalized_string))
        return normalized_string
    if isinstance(value, int):
        normalized_integer = int.__int__(value)
        if abs(normalized_integer).bit_length() > _MAX_INTEGER_BITS:
            raise ValueError(
                f"edited_output JSON integers must contain at most {_MAX_INTEGER_DIGITS} digits"
            )
        try:
            digits = len(str(abs(normalized_integer)))
        except ValueError as exc:
            raise ValueError(
                f"edited_output JSON integers must contain at most {_MAX_INTEGER_DIGITS} digits"
            ) from exc
        if digits > _MAX_INTEGER_DIGITS:
            raise ValueError(
                f"edited_output JSON integers must contain at most {_MAX_INTEGER_DIGITS} digits"
            )
        _consume_json_bytes(byte_budget, digits + int(normalized_integer < 0))
        return normalized_integer
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("edited_output JSON numbers must be finite")
        normalized_float = float.__float__(value)
        _consume_json_bytes(byte_budget, len(repr(normalized_float)))
        return normalized_float
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError("edited_output JSON values must not contain cycles")
        seen.add(identity)
        try:
            _consume_json_bytes(byte_budget, 2)
            snapshot = _snapshot_mapping(
                value,
                context="edited_output JSON object",
                max_entries=_MAX_JSON_NODES,
            )
            normalized: dict[str, object] = {}
            for index, (key, item) in enumerate(snapshot.items()):
                if not isinstance(key, str):
                    raise TypeError("edited_output JSON object keys must be strings")
                if index:
                    _consume_json_bytes(byte_budget, 1)
                _consume_json_bytes(byte_budget, _json_string_size(key) + 1)
                normalized[key] = _normalize_json_value(
                    item,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                    byte_budget=byte_budget,
                )
            return normalized
        finally:
            seen.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in seen:
            raise ValueError("edited_output JSON values must not contain cycles")
        seen.add(identity)
        try:
            _consume_json_bytes(byte_budget, 2)
            normalized_list = []
            for index, item in enumerate(value):
                if index:
                    _consume_json_bytes(byte_budget, 1)
                normalized_list.append(
                    _normalize_json_value(
                        item,
                        depth=depth + 1,
                        seen=seen,
                        budget=budget,
                        byte_budget=byte_budget,
                    )
                )
            return normalized_list
        finally:
            seen.remove(identity)
    raise TypeError(f"edited_output value of type {type(value).__name__} is not JSON-compatible")


def _snapshot_mapping(
    value: Mapping[Any, Any],
    *,
    context: str,
    max_entries: int,
) -> dict[str, object]:
    iteration_keys: list[object] = []
    for key in value:
        iteration_keys.append(key)
        if len(iteration_keys) > max_entries:
            raise ValueError(f"{context} contains too many entries")

    item_entries: list[tuple[object, object]] = []
    for key, item in value.items():
        item_entries.append((key, item))
        if len(item_entries) > max_entries:
            raise ValueError(f"{context} contains too many entries")

    if not all(isinstance(key, str) for key in iteration_keys):
        raise TypeError(f"{context} keys must be strings")
    item_keys = [key for key, _ in item_entries]
    if not all(isinstance(key, str) for key in item_keys):
        raise TypeError(f"{context} keys must be strings")
    trusted_iteration_keys = [_trusted_string(key) for key in iteration_keys]
    trusted_item_keys = [_trusted_string(key) for key in item_keys]
    if len(trusted_iteration_keys) != len(set(trusted_iteration_keys)) or len(
        trusted_item_keys
    ) != len(set(trusted_item_keys)):
        raise ValueError(f"{context} must not contain duplicate keys")
    if set(trusted_iteration_keys) != set(trusted_item_keys):
        raise ValueError(f"{context} iteration and items must expose the same keys")
    return {key: item for key, (_, item) in zip(trusted_item_keys, item_entries)}


def _trusted_string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    return str.__str__(value)


def _validate_json_string(value: str) -> None:
    _json_string_size(value)


def _json_string_size(value: str) -> int:
    raw_bytes = 0
    canonical_bytes = 2
    for character in value:
        code_point = ord(character)
        if code_point <= 0x7F:
            raw_bytes += 1
            if code_point <= 0x1F:
                canonical_bytes += 2 if code_point in {8, 9, 10, 12, 13} else 6
            else:
                canonical_bytes += 2 if character in {'"', "\\"} else 1
        elif code_point <= 0x7FF:
            raw_bytes += 2
            canonical_bytes += 2
        elif 0xD800 <= code_point <= 0xDFFF:
            raise ValueError("JSON strings must contain valid Unicode scalar values")
        elif code_point <= 0xFFFF:
            raw_bytes += 3
            canonical_bytes += 3
        else:
            raw_bytes += 4
            canonical_bytes += 4
        if raw_bytes > _MAX_JSON_BYTES:
            raise ValueError(f"JSON strings must contain at most {_MAX_JSON_BYTES} UTF-8 bytes")
    return canonical_bytes


def _consume_json_bytes(budget: list[int], amount: int) -> None:
    budget[0] += amount
    if budget[0] > _MAX_JSON_BYTES:
        raise ValueError(f"canonical JSON must contain at most {_MAX_JSON_BYTES} UTF-8 bytes")


def _measure_json_value(value: object, budget: list[int]) -> None:
    if value is None:
        _consume_json_bytes(budget, 4)
    elif isinstance(value, bool):
        _consume_json_bytes(budget, 4 if value else 5)
    elif isinstance(value, str):
        _consume_json_bytes(budget, _json_string_size(value))
    elif isinstance(value, int):
        _consume_json_bytes(budget, len(str(value)))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        _consume_json_bytes(budget, len(repr(value)))
    elif isinstance(value, Mapping):
        _consume_json_bytes(budget, 2)
        for index, (key, item) in enumerate(value.items()):
            if index:
                _consume_json_bytes(budget, 1)
            _consume_json_bytes(budget, _json_string_size(key) + 1)
            _measure_json_value(item, budget)
    elif isinstance(value, list):
        _consume_json_bytes(budget, 2)
        for index, item in enumerate(value):
            if index:
                _consume_json_bytes(budget, 1)
            _measure_json_value(item, budget)
    else:
        raise TypeError("value is not JSON-compatible")


def _canonical_json_bytes(value: object) -> bytes:
    _measure_json_value(value, [0])
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(canonical) > _MAX_JSON_BYTES:
        raise ValueError(f"canonical JSON must contain at most {_MAX_JSON_BYTES} UTF-8 bytes")
    return canonical


def _freeze_json_object(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_json_object(value)
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int.__int__(value)
    if isinstance(value, float):
        return float.__float__(value)
    return value


def _field_error(field: str, code: str, message: str) -> RevisionActionFieldError:
    return RevisionActionFieldError(field=field, code=code, message=message)


def _raise_invalid(*field_errors: RevisionActionFieldError) -> NoReturn:
    raise RevisionActionValidationError("invalid revision action", tuple(field_errors))

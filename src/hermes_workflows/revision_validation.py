from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


REVISION_ACTION_SERVICE_ID = "revision.action.validator"
REVISION_ACTION_CONTRACT_VERSION = 1
_ACTIONABLE_MESSAGE = "request_changes requires nonblank feedback or valid edited_output"
_ALLOWED_FIELDS = frozenset({"action", "feedback", "edited_output"})
_MISSING = object()


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

    unknown = sorted(
        (field for field in payload if field not in _ALLOWED_FIELDS),
        key=lambda field: (type(field).__name__, repr(field)),
    )
    if unknown:
        _raise_invalid(
            *(
                _field_error(str(field), "unknown", f"unknown revision action field: {field}")
                for field in unknown
            )
        )

    action_value = payload.get("action", _MISSING)
    if not isinstance(action_value, str):
        _raise_invalid(_field_error("action", "type", "action must be a string"))
    action = str(action_value).strip()
    if action not in {"approve", "request_changes"}:
        _raise_invalid(
            _field_error("action", "choice", "action must be approve or request_changes")
        )

    feedback_value = payload.get("feedback", _MISSING)
    edited_value = payload.get("edited_output", _MISSING)

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
        feedback = str(feedback_value).strip()
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
        except (TypeError, ValueError) as exc:
            _raise_invalid(_field_error("edited_output", "json", str(exc)))
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
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ValidatedRevisionActionV1(
        action=str(normalized["action"]),
        normalized_payload=_freeze_json_object(normalized),
        normalized_payload_hash=payload_hash,
        idempotency_key=f"revision-action:v1:{payload_hash}",
    )


def _normalize_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("edited_output JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("edited_output JSON object keys must be strings")
            normalized[key] = _normalize_json_value(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    raise TypeError(f"edited_output value of type {type(value).__name__} is not JSON-compatible")


def _freeze_json_object(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_json_object(value)
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _field_error(field: str, code: str, message: str) -> RevisionActionFieldError:
    return RevisionActionFieldError(field=field, code=code, message=message)


def _raise_invalid(*field_errors: RevisionActionFieldError) -> None:
    raise RevisionActionValidationError("invalid revision action", tuple(field_errors))

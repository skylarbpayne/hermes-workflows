from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from hermes_workflows.operator_services import OperatorServicesV1
from hermes_workflows.revision_validation import (
    REVISION_ACTION_CONTRACT_VERSION,
    REVISION_ACTION_SERVICE_ID,
    RevisionActionValidationError,
    RevisionActionValidatorV1,
    validate_revision_action,
)


FIXTURE = Path(__file__).parent / "fixtures" / "revision_actions_v1.json"
ACTIONABLE_MESSAGE = "request_changes requires nonblank feedback or valid edited_output"


class HostileRevisionMapping(Mapping[str, object]):
    def __init__(self, action: str, field: str, value: object) -> None:
        self._data = {"action": action, field: value}

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def get(self, key: str, default: object = None) -> object:
        if key in {"feedback", "edited_output"}:
            return default
        return self._data.get(key, default)


class InconsistentItemsMapping(HostileRevisionMapping):
    def items(self):  # type: ignore[override]
        return [("action", self._data["action"])]


class DuplicateItemsMapping(HostileRevisionMapping):
    def items(self):  # type: ignore[override]
        return [("action", self._data["action"]), ("action", "request_changes")]


def _cyclic_list() -> list[object]:
    value: list[object] = []
    value.append(value)
    return value


def _nested_list(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


def test_fixture_covers_actionable_request_changes_contract():
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["schema_version"] == 1

    for case in fixture["cases"]:
        if case["valid"]:
            result = validate_revision_action(case["payload"])
            assert result.action in {"approve", "request_changes"}
            assert result.normalized_payload_hash == case["normalized_payload_hash"]
            assert result.idempotency_key == f"revision-action:v1:{result.normalized_payload_hash}"
        else:
            with pytest.raises(RevisionActionValidationError) as caught:
                validate_revision_action(case["payload"])
            assert str(caught.value) == ACTIONABLE_MESSAGE
            assert [error.field for error in caught.value.field_errors] == case["error_fields"]


def test_normalization_is_stable_across_whitespace_and_mapping_order():
    first = validate_revision_action(
        {
            "action": " request_changes ",
            "feedback": "  Tighten the opening.  ",
            "edited_output": {"z": [1, True], "a": "✓"},
        }
    )
    duplicate = validate_revision_action(
        {
            "edited_output": {"a": "✓", "z": [1, True]},
            "feedback": "Tighten the opening.",
            "action": "request_changes",
        }
    )

    assert first.normalized_payload == duplicate.normalized_payload
    assert first.normalized_payload_hash == duplicate.normalized_payload_hash
    assert first.idempotency_key == duplicate.idempotency_key


def test_validation_rejects_type_coercion_unknown_fields_and_invalid_json():
    bad_payloads = [
        {"action": True, "feedback": "fix"},
        {"action": "REQUEST_CHANGES", "feedback": "fix"},
        {"action": "request-changes", "feedback": "fix"},
        {"action": "request_changes", "feedback": 1},
        {"action": "request_changes", "edited_output": None},
        {"action": "request_changes", "edited_output": {1: "not-json"}},
        {"action": "request_changes", "edited_output": float("nan")},
        {"action": "request_changes", "feedback": "fix", "extra": "bypass"},
    ]

    for payload in bad_payloads:
        with pytest.raises(RevisionActionValidationError) as caught:
            validate_revision_action(payload)
        assert caught.value.field_errors

    with pytest.raises(RevisionActionValidationError) as caught:
        validate_revision_action([])  # type: ignore[arg-type]
    assert caught.value.field_errors[0].field == "payload"


def test_approve_rejects_revision_fields_instead_of_hiding_data():
    for field in ("feedback", "edited_output"):
        with pytest.raises(RevisionActionValidationError) as caught:
            validate_revision_action({"action": "approve", field: "unexpected"})
        assert caught.value.field_errors[0].field == field


def test_custom_mapping_get_cannot_hide_revision_fields():
    for field in ("feedback", "edited_output"):
        with pytest.raises(RevisionActionValidationError) as caught:
            validate_revision_action(HostileRevisionMapping("approve", field, "visible"))
        assert caught.value.field_errors[0].field == field

    with pytest.raises(RevisionActionValidationError, match=ACTIONABLE_MESSAGE):
        validate_revision_action(
            HostileRevisionMapping("request_changes", "feedback", "")
        )


def test_custom_mapping_views_must_agree_and_must_not_repeat_keys():
    for payload in (
        InconsistentItemsMapping("approve", "feedback", "visible"),
        DuplicateItemsMapping("approve", "feedback", "visible"),
    ):
        with pytest.raises(RevisionActionValidationError) as caught:
            validate_revision_action(payload)
        assert caught.value.field_errors[0].field == "payload"


@pytest.mark.parametrize(
    "edited_output",
    [
        pytest.param(_cyclic_list(), id="cycle"),
        pytest.param(_nested_list(65), id="excessive-depth"),
        pytest.param([0] * 10_001, id="excessive-size"),
        pytest.param("x" * 1_000_001, id="oversized-string"),
        pytest.param(10**5000, id="oversized-integer"),
        pytest.param("\ud800", id="lone-surrogate"),
        pytest.param({"\ud800": "value"}, id="lone-surrogate-key"),
    ],
)
def test_malformed_edited_output_fails_as_validation_error(edited_output: object):
    with pytest.raises(RevisionActionValidationError) as caught:
        validate_revision_action(
            {"action": "request_changes", "edited_output": edited_output}
        )
    assert caught.value.field_errors[0].field == "edited_output"


def test_direct_operator_service_path_uses_the_same_validator():
    validator = RevisionActionValidatorV1()
    registry = OperatorServicesV1(services={REVISION_ACTION_SERVICE_ID: validator})
    resolved = registry.resolve(REVISION_ACTION_SERVICE_ID, REVISION_ACTION_CONTRACT_VERSION)

    assert resolved is validator
    assert isinstance(resolved, RevisionActionValidatorV1)
    valid = resolved.validate({"action": "request_changes", "feedback": "Make it concrete"})
    assert valid.normalized_payload == {
        "action": "request_changes",
        "feedback": "Make it concrete",
    }
    with pytest.raises(RevisionActionValidationError, match=ACTIONABLE_MESSAGE):
        resolved.validate({"action": "request_changes", "feedback": "\u2003"})
    with pytest.raises(RevisionActionValidationError) as caught:
        resolved.validate({"action": "request_changes", "edited_output": _cyclic_list()})
    assert caught.value.field_errors[0].field == "edited_output"


def test_idempotency_hash_does_not_collapse_distinct_decisions():
    feedback = validate_revision_action({"action": "request_changes", "feedback": "A"})
    other_feedback = validate_revision_action({"action": "request_changes", "feedback": "B"})
    edit = validate_revision_action({"action": "request_changes", "edited_output": {"body": "A"}})
    approve = validate_revision_action({"action": "approve"})

    assert len({feedback.idempotency_key, other_feedback.idempotency_key, edit.idempotency_key, approve.idempotency_key}) == 4


def test_normalized_payload_is_an_immutable_snapshot_and_errors_are_structured():
    source = {"action": "request_changes", "edited_output": {"sections": ["one"]}}
    result = validate_revision_action(source)
    source["edited_output"]["sections"].append("mutated")  # type: ignore[index,union-attr]

    assert result.normalized_payload["edited_output"]["sections"] == ("one",)  # type: ignore[index]
    with pytest.raises(TypeError):
        result.normalized_payload["action"] = "approve"  # type: ignore[index]

    with pytest.raises(RevisionActionValidationError) as caught:
        validate_revision_action({"action": "request_changes"})
    assert caught.value.to_dict() == {
        "code": "revision_action_invalid",
        "message": ACTIONABLE_MESSAGE,
        "field_errors": [
            {"field": "feedback", "code": "actionable_required", "message": ACTIONABLE_MESSAGE},
            {"field": "edited_output", "code": "actionable_required", "message": ACTIONABLE_MESSAGE},
        ],
    }

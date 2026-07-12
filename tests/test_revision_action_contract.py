from __future__ import annotations

import json
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

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


class HostileWhitespace(str):
    def strip(self, chars=None):  # type: ignore[override]
        return "looks-actionable"


class LyingString(str):
    def __str__(self) -> str:
        return "looks-actionable"


class RaisingString(str):
    def __str__(self) -> str:
        raise RuntimeError("hostile __str__ must not run")


class LyingValidationError(RuntimeError):
    def __str__(self) -> str:
        return "attacker-controlled validation detail"


class RaisingValidationError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("hostile exception __str__ must not run")


class RaisingIterationMapping(Mapping[str, object]):
    def __init__(self, error_type: type[RuntimeError]) -> None:
        self._error_type = error_type

    def __iter__(self) -> Iterator[str]:
        raise self._error_type()

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> object:
        raise KeyError(key)


class RaisingIterationList(list[object]):
    def __init__(self, error_type: type[RuntimeError]) -> None:
        super().__init__(["revised"])
        self._error_type = error_type

    def __iter__(self) -> Iterator[object]:
        raise self._error_type()


class LyingAbsoluteInteger(int):
    def __abs__(self) -> int:
        return 0


class StatefulInteger(int):
    state: dict[str, str]

    def __new__(cls, value: int):
        instance = super().__new__(cls, value)
        instance.state = {"status": "original"}
        return instance


class StatefulFloat(float):
    state: dict[str, str]

    def __new__(cls, value: float):
        instance = super().__new__(cls, value)
        instance.state = {"status": "original"}
        return instance


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


def test_integer_subclass_cannot_bypass_the_digit_limit_with_hostile_abs():
    oversized = 10**4096
    expected_error = {
        "field": "edited_output",
        "code": "json",
        "message": "edited_output must be a valid bounded JSON value",
    }

    for value in (oversized, LyingAbsoluteInteger(oversized)):
        with pytest.raises(RevisionActionValidationError) as caught:
            validate_revision_action(
                {"action": "request_changes", "edited_output": value}
            )
        assert [error.to_dict() for error in caught.value.field_errors] == [expected_error]


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
    with pytest.raises(RevisionActionValidationError, match=ACTIONABLE_MESSAGE):
        resolved.validate(
            {"action": "request_changes", "edited_output": HostileWhitespace("\u2003")}
        )
    hostile_valid = resolved.validate(
        {"action": "request_changes", "edited_output": HostileWhitespace("revised")}
    )
    assert type(hostile_valid.normalized_payload["edited_output"]) is str
    with pytest.raises(RevisionActionValidationError) as caught:
        resolved.validate({"action": "request_changes", "edited_output": _cyclic_list()})
    assert caught.value.field_errors[0].field == "edited_output"


def test_string_subclass_overrides_cannot_change_actionability_or_escape_validation():
    for field in ("feedback", "edited_output"):
        with pytest.raises(RevisionActionValidationError, match=ACTIONABLE_MESSAGE):
            validate_revision_action(
                {"action": "request_changes", field: LyingString("\u2003")}
            )

    action = validate_revision_action(
        {"action": RaisingString("request_changes"), "feedback": RaisingString("Revise it")}
    )
    assert action.normalized_payload == {
        "action": "request_changes",
        "feedback": "Revise it",
    }
    assert type(action.action) is str
    assert type(action.normalized_payload["feedback"]) is str

    lying = validate_revision_action(
        {"action": LyingString("request_changes"), "feedback": LyingString("Revise it")}
    )
    assert lying.normalized_payload == action.normalized_payload
    assert lying.idempotency_key == action.idempotency_key


@pytest.mark.parametrize("error_type", [LyingValidationError, RaisingValidationError])
@pytest.mark.parametrize(
    ("payload", "expected_field", "expected_code", "expected_message"),
    [
        pytest.param(
            lambda error_type: RaisingIterationMapping(error_type),
            "payload",
            "mapping",
            "payload could not be read safely",
            id="top-level-custom-mapping",
        ),
        pytest.param(
            lambda error_type: {
                "action": "request_changes",
                "edited_output": RaisingIterationList(error_type),
            },
            "edited_output",
            "json",
            "edited_output must be a valid bounded JSON value",
            id="edited-list-subclass",
        ),
    ],
)
def test_hostile_exception_stringification_is_contained_and_non_leaking(
    error_type,
    payload,
    expected_field: str,
    expected_code: str,
    expected_message: str,
):
    with pytest.raises(RevisionActionValidationError) as caught:
        validate_revision_action(payload(error_type))

    assert [error.to_dict() for error in caught.value.field_errors] == [
        {
            "field": expected_field,
            "code": expected_code,
            "message": expected_message,
        }
    ]
    serialized_error = repr(caught.value.to_dict())
    assert "attacker-controlled" not in serialized_error
    assert "hostile exception" not in serialized_error


def test_combined_canonical_payload_limit_has_a_deterministic_non_leaking_error():
    with pytest.raises(RevisionActionValidationError) as caught:
        validate_revision_action(
            {
                "action": "request_changes",
                "feedback": "f" * 600_000,
                "edited_output": "e" * 600_000,
            }
        )

    assert [error.to_dict() for error in caught.value.field_errors] == [
        {
            "field": "payload",
            "code": "json",
            "message": "normalized revision action exceeds JSON limits",
        }
    ]


def test_normalized_json_object_keys_are_exact_builtin_strings():
    key = RaisingString("body")
    result = validate_revision_action(
        {"action": "request_changes", "edited_output": {key: "revised"}}
    )

    normalized_edit = result.normalized_payload["edited_output"]
    assert isinstance(normalized_edit, Mapping)
    normalized_key = next(iter(normalized_edit))
    assert normalized_key == "body"
    assert type(normalized_key) is str
    plain = validate_revision_action(
        {"action": "request_changes", "edited_output": {"body": "revised"}}
    )
    assert result.idempotency_key == plain.idempotency_key


@pytest.mark.parametrize(
    ("numeric_subclass", "plain_value", "builtin_type"),
    [
        pytest.param(StatefulInteger(7), 7, int, id="integer-subclass"),
        pytest.param(StatefulFloat(7.5), 7.5, float, id="float-subclass"),
    ],
)
def test_numeric_subclasses_are_detached_exact_builtin_snapshots_with_stable_hashes(
    numeric_subclass: object,
    plain_value: object,
    builtin_type: type,
):
    result = validate_revision_action(
        {"action": "request_changes", "edited_output": {"score": numeric_subclass}}
    )
    plain = validate_revision_action(
        {"action": "request_changes", "edited_output": {"score": plain_value}}
    )

    normalized_edit = result.normalized_payload["edited_output"]
    assert isinstance(normalized_edit, Mapping)
    normalized_number = normalized_edit["score"]
    assert type(normalized_number) is builtin_type
    assert normalized_number is not numeric_subclass
    assert not hasattr(normalized_number, "state")
    numeric_subclass.state["status"] = "mutated"  # type: ignore[attr-defined]
    assert normalized_number == plain_value
    assert result.normalized_payload_hash == plain.normalized_payload_hash
    assert result.idempotency_key == plain.idempotency_key


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

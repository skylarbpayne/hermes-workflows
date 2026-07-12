from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Optional, TypedDict, Union

import pytest

from hermes_workflows.operator_services import OperatorServicesV1
from hermes_workflows.projection_sections import ProjectionContributorV1
from hermes_workflows.revision import (
    MAX_DIFF_DESCRIPTOR_BYTES,
    REVISION_SERVICE_ID,
    RevisionConflictError,
    RevisionDiffV1,
    RevisionError,
    RevisionLedger,
    RevisionServiceV1,
    RevisionValueError,
    canonical_value_hash,
    resolve_revision_service,
)


@dataclass(frozen=True)
class Draft:
    title: str
    score: int


@dataclass(frozen=True)
class DriftedDraft:
    title: str
    score: int
    format: str = "markdown"


@dataclass(frozen=True)
class FloatDraft:
    score: float


@dataclass(frozen=True)
class NestedDraft:
    primary: Draft
    alternatives: list[Draft]
    archived: tuple[Draft, ...]
    by_name: dict[str, Draft]
    fallback: Optional[Draft]


@dataclass(frozen=True)
class AnnotatedDraft:
    child: Annotated[Draft, "revision child"]


@dataclass(frozen=True)
class NarrowDraft:
    title: str


@dataclass(frozen=True)
class ExpandedDraft:
    title: str
    score: int = 0


class TypedDraft(TypedDict):
    title: str
    score: int


@dataclass(frozen=True)
class LiteralDraft:
    choice: Literal[1, "one"]


class LiteralTypedDraft(TypedDict):
    choice: Literal[1, "one"]


@dataclass(frozen=True)
class BoolLiteralDraft:
    choice: Literal[True]


@dataclass(frozen=True)
class PostponedFallbackLiteralDraft:
    choice: Literal[1]
    note: str | None = None


@dataclass(frozen=True)
class PostponedPep604LiteralDraft:
    choice: Literal[1] | None


def _test_stable_id(prefix, payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


class BrokenMapping(Mapping):
    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        raise RuntimeError("SECRET_MAPPING_ITERATION")

    def __len__(self):
        return 1


class HostileInt(int):
    def __lt__(self, other):
        raise RuntimeError("SECRET_NUMERIC_COMPARATOR")


def test_valid_edit_is_schema_coerced_and_becomes_exact_next_base(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    original = ledger.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)

    edited = ledger.record_edit(
        "wf_revision",
        1,
        {"title": "Human edit", "score": "2"},
        value_type=Draft,
    )
    selected = ledger.select_next_base("wf_revision", 2, value_type=Draft)

    assert selected.value == Draft("Human edit", 2)
    assert selected.value_sha256 == edited.value_sha256
    assert selected.value_sha256 == canonical_value_hash({"title": "Human edit", "score": 2})
    assert selected.base_revision_id == edited.revision_id
    assert edited.parent_revision_id == original.revision_id
    assert edited.attempt_id == original.attempt_id
    assert selected.attempt_id != edited.attempt_id


def test_invalid_edit_is_rejected_without_mutating_lineage(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    original = ledger.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)

    with pytest.raises(RevisionValueError, match="score"):
        ledger.record_edit(
            "wf_revision",
            1,
            {"title": "Bad", "score": "not-an-int"},
            value_type=Draft,
        )

    assert ledger.revisions("wf_revision") == (original,)

    with pytest.raises(RevisionValueError, match="unknown revision fields"):
        ledger.record_edit(
            "wf_revision",
            1,
            {"title": "Bad", "score": 2, "secret_extra": True},
            value_type=Draft,
        )

    assert ledger.revisions("wf_revision") == (original,)


def test_invalid_dataclass_instance_is_rejected_without_mutating_lineage(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError, match="score"):
        ledger.record_output(
            "wf_dataclass_instance_revision",
            1,
            Draft("Bad", "not-an-int"),  # type: ignore[arg-type]
            value_type=Draft,
        )

    assert ledger.revisions("wf_dataclass_instance_revision") == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {"title": "Bad", "score": "not-an-int"},
            "invalid revision value for TypedDraft",
        ),
        (
            {"title": "Bad", "score": 2, "secret_extra": True},
            "invalid revision value: unknown revision fields",
        ),
        ({"title": "Missing score"}, "invalid revision value for TypedDraft"),
    ],
)
def test_invalid_typed_dict_is_rejected_without_mutating_lineage(
    tmp_path, value, expected
):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output("wf_typed_dict_revision", 1, value, value_type=TypedDraft)

    assert str(caught.value) == expected
    assert "secret_extra" not in str(caught.value)
    assert ledger.revisions("wf_typed_dict_revision") == ()


def test_valid_typed_dict_edit_restarts_into_exact_selected_base(tmp_path):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    ledger.record_output(
        "wf_typed_dict_revision",
        1,
        {"title": "Draft", "score": "1"},
        value_type=TypedDraft,
    )
    edited = ledger.record_edit(
        "wf_typed_dict_revision",
        1,
        {"title": "Human edit", "score": "2"},
        value_type=TypedDraft,
    )

    restarted = RevisionLedger(path)
    selected = restarted.select_next_base(
        "wf_typed_dict_revision", 2, value_type=TypedDraft
    )

    assert selected.value == {"title": "Human edit", "score": 2}
    assert selected.value_sha256 == edited.value_sha256
    assert selected.base_revision_id == edited.revision_id


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        ({"choice": True}, LiteralDraft),
        (LiteralDraft(True), LiteralDraft),  # type: ignore[arg-type]
        ({"choice": True}, LiteralTypedDraft),
    ],
)
def test_literal_bool_int_identity_collisions_are_rejected_without_persistence(
    tmp_path, value, value_type
):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError):
        ledger.record_output("wf_literal_revision", 1, value, value_type=value_type)

    assert ledger.revisions("wf_literal_revision") == ()
    assert not (tmp_path / "revisions.json").exists()


@pytest.mark.parametrize(
    ("value", "value_type", "expected"),
    [
        ({"choice": 1}, LiteralDraft, LiteralDraft(1)),
        (LiteralDraft("one"), LiteralDraft, LiteralDraft("one")),
        ({"choice": "one"}, LiteralTypedDraft, {"choice": "one"}),
    ],
)
def test_valid_literal_values_preserve_value_and_type(tmp_path, value, value_type, expected):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    record = ledger.record_output(
        "wf_valid_literal_revision", 1, value, value_type=value_type
    )
    actual_choice = (
        record.value["choice"]
        if isinstance(record.value, dict)
        else record.value.choice
    )
    expected_choice = expected["choice"] if isinstance(expected, dict) else expected.choice

    assert record.value == expected
    assert type(actual_choice) is type(expected_choice)


def test_literal_int_bool_identity_collision_is_rejected_in_reverse(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_bool_literal_revision",
            1,
            {"choice": 1},
            value_type=BoolLiteralDraft,
        )

    assert ledger.revisions("wf_bool_literal_revision") == ()


def test_postponed_literal_identity_survives_pep604_type_hint_fallback(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_postponed_literal_revision",
            1,
            {"choice": True, "note": None},
            value_type=PostponedFallbackLiteralDraft,
        )

    assert ledger.revisions("wf_postponed_literal_revision") == ()
    assert not (tmp_path / "revisions.json").exists()


def test_postponed_pep604_literal_union_preserves_identity_on_python39(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_postponed_pep604_literal_revision",
            1,
            {"choice": True},
            value_type=PostponedPep604LiteralDraft,
        )

    assert ledger.revisions("wf_postponed_pep604_literal_revision") == ()
    assert not (tmp_path / "revisions.json").exists()

    literal = ledger.record_output(
        "wf_valid_postponed_pep604_literal_revision",
        1,
        {"choice": 1},
        value_type=PostponedPep604LiteralDraft,
    )
    optional = ledger.record_output(
        "wf_valid_postponed_pep604_optional_revision",
        1,
        {"choice": None},
        value_type=PostponedPep604LiteralDraft,
    )

    assert literal.value == PostponedPep604LiteralDraft(1)
    assert type(literal.value.choice) is int
    assert optional.value == PostponedPep604LiteralDraft(None)


def test_persistence_failure_is_not_retained_as_an_idempotent_replay(
    tmp_path, monkeypatch
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    real_replace = os.replace
    replace_attempts = 0

    def fail_first_replace(source, destination):
        nonlocal replace_attempts
        replace_attempts += 1
        if replace_attempts == 1:
            raise OSError("SECRET_PERSIST_PATH:" + "x" * 10_000)
        return real_replace(source, destination)

    monkeypatch.setattr("hermes_workflows.revision.os.replace", fail_first_replace)

    with pytest.raises(RevisionError) as caught:
        ledger.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)

    assert str(caught.value) == "revision ledger persistence failed"
    assert "SECRET_PERSIST_PATH" not in str(caught.value)
    assert len(str(caught.value).encode("utf-8")) < 128

    assert ledger.revisions("wf_revision") == ()
    assert not path.exists()

    retried = ledger.record_output(
        "wf_revision", 1, Draft("Draft", 1), value_type=Draft
    )
    assert ledger.revisions("wf_revision") == (retried,)
    assert RevisionLedger(path).revisions("wf_revision") == (retried,)
    assert replace_attempts == 2


def test_stale_writer_cannot_overwrite_a_conflicting_durable_slot(tmp_path):
    path = tmp_path / "revisions.json"
    first = RevisionLedger(path)
    stale = RevisionLedger(path)

    durable = first.record_output(
        "wf_revision", 1, Draft("First writer", 1), value_type=Draft
    )

    with pytest.raises(RevisionConflictError, match="already has a different revision"):
        stale.record_output(
            "wf_revision", 1, Draft("Stale writer", 2), value_type=Draft
        )

    assert RevisionLedger(path).revisions("wf_revision") == (durable,)
    assert stale.revisions("wf_revision") == (durable,)


def test_dependent_operations_reload_lineage_under_lock(tmp_path):
    path = tmp_path / "revisions.json"
    first = RevisionLedger(path)
    second = RevisionLedger(path)

    output = first.record_output(
        "wf_revision", 1, Draft("First attempt", 1), value_type=Draft
    )
    selected = second.select_next_base("wf_revision", 2, value_type=Draft)
    edited = first.record_edit(
        "wf_revision", 2, Draft("Second attempt edit", 2), value_type=Draft
    )

    assert selected.parent_revision_id == output.revision_id
    assert edited.parent_revision_id == selected.revision_id
    assert RevisionLedger(path).revisions("wf_revision") == (output, selected, edited)


@pytest.mark.parametrize(
    "value",
    [
        {
            "primary": {"title": "Primary", "score": 1, "unknown": True},
            "alternatives": [],
            "archived": (),
            "by_name": {},
            "fallback": None,
        },
        {
            "primary": {"title": "Primary", "score": 1},
            "alternatives": [{"title": "Alternative", "score": 2, "unknown": True}],
            "archived": (),
            "by_name": {},
            "fallback": None,
        },
        {
            "primary": {"title": "Primary", "score": 1},
            "alternatives": [],
            "archived": ({"title": "Archived", "score": 3, "unknown": True},),
            "by_name": {},
            "fallback": None,
        },
        {
            "primary": {"title": "Primary", "score": 1},
            "alternatives": [],
            "archived": (),
            "by_name": {"named": {"title": "Named", "score": 3, "unknown": True}},
            "fallback": None,
        },
        {
            "primary": {"title": "Primary", "score": 1},
            "alternatives": [],
            "archived": (),
            "by_name": {},
            "fallback": {"title": "Fallback", "score": 4, "unknown": True},
        },
    ],
)
def test_nested_unknown_dataclass_fields_are_rejected_without_mutating_lineage(
    tmp_path, value
):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError, match="unknown revision fields"):
        ledger.record_output("wf_nested_revision", 1, value, value_type=NestedDraft)

    assert ledger.revisions("wf_nested_revision") == ()


def test_valid_nested_dataclass_fields_remain_schema_coerced(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    value = {
        "primary": {"title": "Primary", "score": "1"},
        "alternatives": [{"title": "Alternative", "score": "2"}],
        "archived": ({"title": "Archived", "score": "3"},),
        "by_name": {"named": {"title": "Named", "score": "4"}},
        "fallback": {"title": "Fallback", "score": "5"},
    }

    record = ledger.record_output(
        "wf_nested_revision", 1, value, value_type=NestedDraft
    )

    assert record.value == NestedDraft(
        primary=Draft("Primary", 1),
        alternatives=[Draft("Alternative", 2)],
        archived=(Draft("Archived", 3),),
        by_name={"named": Draft("Named", 4)},
        fallback=Draft("Fallback", 5),
    )


def test_annotated_nested_dataclass_is_coerced_and_rejects_unknown_fields(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    record = ledger.record_output(
        "wf_annotated_revision",
        1,
        {"child": {"title": "Annotated", "score": "2"}},
        value_type=AnnotatedDraft,
    )

    assert record.value == AnnotatedDraft(Draft("Annotated", 2))

    attacker_value = {
        "child": {"title": "Bad", "score": 3, "SECRET_ANNOTATED_FIELD": True}
    }
    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output(
            "wf_annotated_revision", 2, attacker_value, value_type=AnnotatedDraft
        )

    assert str(caught.value) == "invalid revision value: unknown revision fields"
    assert "SECRET" not in str(caught.value)
    assert ledger.revisions("wf_annotated_revision") == (record,)


def test_overlapping_dataclass_union_selects_only_compatible_branch(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    record = ledger.record_output(
        "wf_union_revision",
        1,
        {"title": "Expanded", "score": "4"},
        value_type=Union[NarrowDraft, ExpandedDraft],
    )

    assert record.value == ExpandedDraft("Expanded", 4)


@pytest.mark.parametrize(
    "value",
    [
        {"title": "Ambiguous"},
        {"title": "Invalid", "SECRET_UNION_FIELD": True},
    ],
)
def test_ambiguous_or_invalid_dataclass_unions_fail_deterministically(tmp_path, value):
    messages = []
    for index in range(2):
        ledger = RevisionLedger(tmp_path / f"revisions-{index}.json")
        with pytest.raises(RevisionValueError) as caught:
            ledger.record_output(
                "wf_union_revision",
                1,
                value,
                value_type=Union[NarrowDraft, ExpandedDraft],
            )
        messages.append(str(caught.value))
        assert ledger.revisions("wf_union_revision") == ()

    assert messages[0] == messages[1]
    assert len(messages[0].encode("utf-8")) <= 256
    assert "SECRET" not in messages[0]


def test_ambiguous_scalar_unions_fail_independently_of_argument_order(tmp_path):
    messages = []
    for index, value_type in enumerate((Union[int, str], Union[str, int])):
        ledger = RevisionLedger(tmp_path / f"scalar-union-{index}.json")
        with pytest.raises(RevisionValueError) as caught:
            ledger.record_output("wf_scalar_union", 1, "1", value_type=value_type)
        messages.append(str(caught.value))
        assert ledger.revisions("wf_scalar_union") == ()

    assert messages[0] == messages[1]


def test_hostile_revision_mappings_fail_with_bounded_nonleaking_errors(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    secret = "SECRET_" + "x" * 10_000

    for value in (
        {"title": "Bad", "score": 2, secret: True},
        BrokenMapping(),
    ):
        with pytest.raises(RevisionValueError) as caught:
            ledger.record_output("wf_revision", 1, value, value_type=Draft)

        message = str(caught.value)
        assert len(message.encode("utf-8")) <= 256
        assert "SECRET" not in message

    assert ledger.revisions("wf_revision") == ()


def test_revision_values_reject_keys_that_collide_in_canonical_json(tmp_path):
    colliding = {1: "first", "1": "second"}
    reduced = {"1": "second"}

    with pytest.raises(RevisionValueError) as caught:
        canonical_value_hash(colliding)
    assert str(caught.value) == "revision value contains duplicate canonical object keys"
    assert canonical_value_hash(reduced)

    ledger = RevisionLedger(tmp_path / "revisions.json")
    with pytest.raises(RevisionValueError, match="duplicate canonical object keys"):
        ledger.record_output("wf_revision", 1, colliding, value_type=object)
    assert ledger.revisions("wf_revision") == ()


def test_declared_mapping_rejects_keys_that_collide_during_coercion(tmp_path):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output(
            "wf_revision",
            1,
            {1: "first", "1": "second"},
            value_type=dict[str, str],
        )

    assert str(caught.value) == "revision value contains duplicate canonical object keys"
    assert ledger.revisions("wf_revision") == ()
    assert not path.exists()


def test_attempt_number_rejects_hostile_int_subclasses_without_comparison(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionError) as caught:
        ledger.record_output("wf_revision", HostileInt(1), Draft("Draft", 1), value_type=Draft)

    assert str(caught.value) == "attempt_number must be a positive integer"
    assert "SECRET" not in str(caught.value)
    assert ledger.revisions("wf_revision") == ()

    with pytest.raises(RevisionError) as caught_diff:
        RevisionDiffV1("0" * 64, "1" * 64, HostileInt(0))
    assert str(caught_diff.value) == "changed_leaf_count must be a nonnegative integer"
    assert "SECRET" not in str(caught_diff.value)


def test_without_edit_the_generated_output_is_the_next_base(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    output = ledger.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)

    selected = ledger.select_next_base("wf_revision", 2, value_type=Draft)

    assert selected.value == Draft("Draft", 1)
    assert selected.base_revision_id == output.revision_id
    assert selected.parent_revision_id == output.revision_id


def test_next_base_cannot_skip_a_later_attempt_output(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    ledger.record_output("wf_revision", 1, Draft("First", 1), value_type=Draft)
    ledger.select_next_base("wf_revision", 2, value_type=Draft)

    with pytest.raises(RevisionError, match="prior attempt output or edit"):
        ledger.select_next_base("wf_revision", 3, value_type=Draft)

    output_v2 = ledger.record_output(
        "wf_revision", 2, Draft("Second", 2), value_type=Draft
    )
    selected_v3 = ledger.select_next_base("wf_revision", 3, value_type=Draft)
    assert selected_v3.parent_revision_id == output_v2.revision_id
    assert selected_v3.base_revision_id == output_v2.revision_id


def test_restart_rejects_descendant_base_derived_from_an_unfinalized_base(tmp_path):
    path = tmp_path / "stale-descendant-base.json"
    ledger = RevisionLedger(path)
    ledger.record_output("wf_revision", 1, Draft("First", 1), value_type=Draft)
    selected_v2 = ledger.select_next_base("wf_revision", 2, value_type=Draft)
    payload = json.loads(path.read_text(encoding="utf-8"))

    attempt_id = _test_stable_id(
        "att", {"workflow_id": "wf_revision", "attempt_number": 3}
    )
    stale_v3 = {
        "schema_version": 1,
        "workflow_id": "wf_revision",
        "attempt_number": 3,
        "attempt_id": attempt_id,
        "kind": "base",
        "value_sha256": selected_v2.value_sha256,
        "parent_revision_id": selected_v2.revision_id,
        "base_revision_id": selected_v2.revision_id,
        "diff": None,
        "value": {"title": "First", "score": 1},
    }
    stale_v3["revision_id"] = _test_stable_id(
        "rev",
        {
            "workflow_id": "wf_revision",
            "attempt_id": attempt_id,
            "kind": "base",
            "value_sha256": selected_v2.value_sha256,
            "parent_revision_id": selected_v2.revision_id,
            "base_revision_id": selected_v2.revision_id,
        },
    )
    payload["revisions"].append(stale_v3)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RevisionError, match="prior attempt output or edit"):
        RevisionLedger(path)


def test_late_edit_is_rejected_after_descendant_base_selection(tmp_path):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    output = ledger.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)
    selected = ledger.select_next_base("wf_revision", 2, value_type=Draft)

    with pytest.raises(RevisionConflictError, match="descendant base"):
        ledger.record_edit("wf_revision", 1, Draft("Too late", 2), value_type=Draft)

    assert ledger.revisions("wf_revision") == (output, selected)
    restarted = RevisionLedger(path)
    assert restarted.revisions("wf_revision") == (output, selected)
    assert restarted.select_next_base("wf_revision", 2, value_type=Draft) == selected


def test_descendant_base_in_another_workflow_does_not_block_edit(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    ledger.record_output("wf_a", 1, Draft("A", 1), value_type=Draft)
    ledger.select_next_base("wf_a", 2, value_type=Draft)
    output_b = ledger.record_output("wf_b", 1, Draft("B", 1), value_type=Draft)

    edit_b = ledger.record_edit("wf_b", 1, Draft("B edited", 2), value_type=Draft)

    assert edit_b.parent_revision_id == output_b.revision_id
    assert RevisionLedger(ledger.path).revisions("wf_b") == (output_b, edit_b)


def test_preselection_edit_replay_remains_idempotent_after_descendant_base(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    output = ledger.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)
    edit = ledger.record_edit("wf_revision", 1, Draft("Edited", 2), value_type=Draft)
    selected = ledger.select_next_base("wf_revision", 2, value_type=Draft)

    assert ledger.record_edit("wf_revision", 1, Draft("Edited", 2), value_type=Draft) == edit
    assert ledger.revisions("wf_revision") == (output, edit, selected)


def test_restart_rejects_ledger_with_edit_after_descendant_base(tmp_path):
    stale_path = tmp_path / "stale.json"
    stale = RevisionLedger(stale_path)
    stale.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)
    stale.select_next_base("wf_revision", 2, value_type=Draft)

    edit_path = tmp_path / "edit.json"
    edit_source = RevisionLedger(edit_path)
    edit_source.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)
    edit_source.record_edit("wf_revision", 1, Draft("Too late", 2), value_type=Draft)

    stale_payload = json.loads(stale_path.read_text(encoding="utf-8"))
    edit_payload = json.loads(edit_path.read_text(encoding="utf-8"))
    stale_payload["revisions"].append(edit_payload["revisions"][-1])
    stale_path.write_text(json.dumps(stale_payload), encoding="utf-8")

    with pytest.raises(RevisionError, match="edit cannot follow descendant base selection"):
        RevisionLedger(stale_path)


def test_restart_rejects_descendant_base_that_skips_prior_edit(tmp_path):
    edited_path = tmp_path / "edited.json"
    edited = RevisionLedger(edited_path)
    edited.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)
    edited.record_edit("wf_revision", 1, Draft("Human edit", 2), value_type=Draft)

    generated_path = tmp_path / "generated.json"
    generated = RevisionLedger(generated_path)
    generated.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)
    generated.select_next_base("wf_revision", 2, value_type=Draft)

    edited_payload = json.loads(edited_path.read_text(encoding="utf-8"))
    generated_payload = json.loads(generated_path.read_text(encoding="utf-8"))
    edited_payload["revisions"].append(generated_payload["revisions"][-1])
    edited_path.write_text(json.dumps(edited_payload), encoding="utf-8")

    with pytest.raises(RevisionError, match="prior attempt's edited revision"):
        RevisionLedger(edited_path)


def test_stable_attempt_slots_reject_conflicting_output_or_edit(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    ledger.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)

    with pytest.raises(RevisionConflictError, match="output slot"):
        ledger.record_output("wf_revision", 1, Draft("Other", 2), value_type=Draft)

    ledger.record_edit("wf_revision", 1, Draft("Edited", 2), value_type=Draft)
    with pytest.raises(RevisionConflictError, match="edit slot"):
        ledger.record_edit("wf_revision", 1, Draft("Different edit", 3), value_type=Draft)


def test_attempt_parent_and_revision_ids_are_stable_across_restart(tmp_path):
    path = tmp_path / "revisions.json"
    first = RevisionLedger(path)
    output = first.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)
    edit = first.record_edit("wf_revision", 1, Draft("Edited", 2), value_type=Draft)

    restarted = RevisionLedger(path)
    selected = restarted.select_next_base("wf_revision", 2, value_type=Draft)

    assert restarted.revisions("wf_revision") == (output, edit, selected)
    assert restarted.select_next_base("wf_revision", 2, value_type=Draft) == selected
    assert selected.base_revision_id == edit.revision_id
    assert selected.value_sha256 == edit.value_sha256

    output_v2 = restarted.record_output(
        "wf_revision", 2, Draft("Generated from edit", 3), value_type=Draft
    )
    assert output_v2.parent_revision_id == selected.revision_id
    assert output_v2.base_revision_id == selected.revision_id


def test_schema_drift_cannot_change_exact_edited_revision_base(tmp_path):
    path = tmp_path / "revisions.json"
    first = RevisionLedger(path)
    first.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)
    edited = first.record_edit("wf_revision", 1, Draft("Edited", 2), value_type=Draft)

    drifted = RevisionLedger(path)
    with pytest.raises(RevisionValueError, match="exact revision value"):
        drifted.select_next_base("wf_revision", 2, value_type=DriftedDraft)
    assert drifted.revisions("wf_revision") == tuple(first.revisions("wf_revision"))

    restarted = RevisionLedger(path)
    selected = restarted.select_next_base("wf_revision", 2, value_type=Draft)
    assert selected.value_sha256 == edited.value_sha256
    assert selected.base_revision_id == edited.revision_id
    selected_drifted = RevisionLedger(path)
    with pytest.raises(RevisionValueError, match="exact revision value"):
        selected_drifted.select_next_base("wf_revision", 2, value_type=DriftedDraft)
    assert restarted.select_next_base("wf_revision", 2, value_type=Draft) == selected
    assert RevisionLedger(path).revisions("wf_revision") == (*first.revisions("wf_revision"), selected)


def test_revision_ledger_is_resolved_through_generic_operator_service_registry(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    registry = OperatorServicesV1(services={REVISION_SERVICE_ID: ledger})

    assert isinstance(ledger, RevisionServiceV1)
    assert resolve_revision_service(registry) is ledger


def test_diff_descriptor_is_bounded_deterministic_and_contains_no_values(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    before = Draft("private-before-" + "x" * 2000, 1)
    after = Draft("private-after-" + "y" * 2000, 2)
    ledger.record_output("wf_revision", 1, before, value_type=Draft)

    edited = ledger.record_edit("wf_revision", 1, after, value_type=Draft)
    descriptor = edited.diff
    assert descriptor is not None
    encoded = json.dumps(descriptor.to_dict(), sort_keys=True, separators=(",", ":"))

    assert len(encoded.encode("utf-8")) <= MAX_DIFF_DESCRIPTOR_BYTES
    assert "private-before" not in encoded
    assert "private-after" not in encoded
    assert "title" not in encoded
    assert descriptor.before_sha256 == canonical_value_hash(before)
    assert descriptor.after_sha256 == canonical_value_hash(after)
    assert descriptor.changed_leaf_count == 2

    with pytest.raises(RevisionError, match="nonnegative"):
        RevisionDiffV1(
            before_sha256=descriptor.before_sha256,
            after_sha256=descriptor.after_sha256,
            changed_leaf_count=-1,
        )


def test_restart_rejects_tampered_diff_count(tmp_path):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    ledger.record_output("wf_revision", 1, Draft("before", 1), value_type=Draft)
    ledger.record_edit("wf_revision", 1, Draft("after", 2), value_type=Draft)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["revisions"][1]["diff"]["changed_leaf_count"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RevisionError, match="changed-leaf count"):
        RevisionLedger(path)


@pytest.mark.parametrize(
    ("level", "invalid_version"),
    [
        ("ledger", True),
        ("ledger", 1.0),
        ("entry", True),
        ("entry", 1.0),
        ("diff", True),
        ("diff", 1.0),
        ("diff", 2),
    ],
)
def test_restart_requires_exact_builtin_schema_version_at_every_level(
    tmp_path, level, invalid_version
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    ledger.record_output("wf_revision", 1, Draft("before", 1), value_type=Draft)
    ledger.record_edit("wf_revision", 1, Draft("after", 2), value_type=Draft)
    payload = json.loads(path.read_text(encoding="utf-8"))

    if level == "ledger":
        payload["schema_version"] = invalid_version
    elif level == "entry":
        payload["revisions"][0]["schema_version"] = invalid_version
    else:
        payload["revisions"][1]["diff"]["schema_version"] = invalid_version
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RevisionError, match=f"{level} schema_version must be the integer 1"):
        RevisionLedger(path)


@pytest.mark.parametrize("workflow_id", ["", " ", "\t"])
def test_restart_normalizes_blank_persisted_workflow_id_to_revision_error(
    tmp_path, workflow_id
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    ledger.record_output("wf_revision", 1, Draft("before", 1), value_type=Draft)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["revisions"][0]["workflow_id"] = workflow_id
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RevisionError) as caught:
        RevisionLedger(path)

    assert str(caught.value) == "revision ledger contains invalid persisted revision data"


@pytest.mark.parametrize(
    "persisted_value",
    [
        float("nan"),
        {"nested": float("inf")},
        [float("-inf")],
    ],
)
def test_restart_normalizes_nonfinite_persisted_values_to_revision_error(
    tmp_path, persisted_value
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    ledger.record_output("wf_revision", 1, Draft("before", 1), value_type=Draft)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["revisions"][0]["value"] = persisted_value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RevisionError) as caught:
        RevisionLedger(path)

    assert str(caught.value) == "revision ledger contains invalid persisted revision data"


def test_restart_normalizes_huge_json_integer_parse_failure_to_revision_error(tmp_path):
    path = tmp_path / "revisions.json"
    huge_version = "9" * 5000
    path.write_text(
        '{"schema_version":' + huge_version + ',"revisions":[]}',
        encoding="utf-8",
    )

    with pytest.raises(RevisionError) as caught:
        RevisionLedger(path)

    assert str(caught.value) == "revision ledger must contain valid UTF-8 JSON"
    assert huge_version not in str(caught.value)


def test_restart_rejects_duplicate_persisted_json_object_keys(tmp_path):
    path = tmp_path / "revisions.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,"revisions":[]}',
        encoding="utf-8",
    )

    with pytest.raises(RevisionError) as caught:
        RevisionLedger(path)

    assert str(caught.value) == "revision ledger must contain valid UTF-8 JSON"


def test_restart_normalizes_excessively_deep_json_across_python_versions(tmp_path):
    path = tmp_path / "revisions.json"
    path.write_text(
        '{"schema_version":1,"revisions":' + "[" * 2_000 + "]" * 2_000 + "}",
        encoding="utf-8",
    )

    with pytest.raises(RevisionError) as caught:
        RevisionLedger(path)

    assert str(caught.value) == "revision ledger must contain valid UTF-8 JSON"


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        (float("nan"), float),
        ({"score": float("inf")}, FloatDraft),
    ],
)
def test_generated_output_rejects_nonfinite_values_with_bounded_revision_error(
    tmp_path, value, value_type
):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output("wf_revision", 1, value, value_type=value_type)

    assert str(caught.value) == "revision value must contain only finite JSON numbers"
    assert ledger.revisions("wf_revision") == ()


@pytest.mark.parametrize(
    ("initial", "edit", "value_type"),
    [
        (1.0, float("inf"), float),
        (FloatDraft(1.0), {"score": float("nan")}, FloatDraft),
    ],
)
def test_schema_coerced_edit_rejects_nonfinite_values_with_bounded_revision_error(
    tmp_path, initial, edit, value_type
):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    original = ledger.record_output("wf_revision", 1, initial, value_type=value_type)

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_edit("wf_revision", 1, edit, value_type=value_type)

    assert str(caught.value) == "revision value must contain only finite JSON numbers"
    assert ledger.revisions("wf_revision") == (original,)


def test_restart_rejects_duplicate_workflow_attempt_kind_slot(tmp_path):
    path = tmp_path / "revisions.json"
    first = RevisionLedger(path)
    first.record_output("wf_revision", 1, Draft("first", 1), value_type=Draft)

    second_path = tmp_path / "second.json"
    second = RevisionLedger(second_path)
    second.record_output("wf_revision", 1, Draft("second", 2), value_type=Draft)

    payload = json.loads(path.read_text(encoding="utf-8"))
    second_payload = json.loads(second_path.read_text(encoding="utf-8"))
    payload["revisions"].append(second_payload["revisions"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RevisionError, match="^duplicate revision slot$"):
        RevisionLedger(path)


def test_duplicate_slot_restart_error_is_bounded_deterministic_and_nonleaking(tmp_path):
    workflow_id = "SENSITIVE_" + "x" * 10_000
    messages = []

    for index in range(2):
        path = tmp_path / f"revisions-{index}.json"
        first = RevisionLedger(path)
        first.record_output(workflow_id, 1, Draft("first", 1), value_type=Draft)

        second_path = tmp_path / f"second-{index}.json"
        second = RevisionLedger(second_path)
        second.record_output(workflow_id, 1, Draft("second", 2), value_type=Draft)

        payload = json.loads(path.read_text(encoding="utf-8"))
        second_payload = json.loads(second_path.read_text(encoding="utf-8"))
        payload["revisions"].append(second_payload["revisions"][0])
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(RevisionError) as caught:
            RevisionLedger(path)
        messages.append(str(caught.value))

    assert messages == ["duplicate revision slot", "duplicate revision slot"]
    assert len(messages[0].encode("utf-8")) <= 256
    assert "SENSITIVE" not in messages[0]


def test_revision_projection_is_bounded_and_nonleaking(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    ledger.record_output("wf_revision", 1, Draft("secret original", 1), value_type=Draft)
    edit = ledger.record_edit("wf_revision", 1, Draft("secret edit", 2), value_type=Draft)

    assert isinstance(ledger, ProjectionContributorV1)
    sections = ledger.project("wf_revision")

    assert len(sections) == 1
    section = sections[0]
    assert section.section_id == "revision.summary"
    assert section.summary["latest_revision_id"] == edit.revision_id
    assert section.summary["latest_value_sha256"] == edit.value_sha256
    assert "secret" not in section.to_json()


def test_fixture_restart_round_trip_uses_edited_hash_as_v2_base(tmp_path):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "revision_v1.json").read_text(encoding="utf-8")
    )
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    ledger.record_output(fixture["workflow_id"], 1, fixture["output"], value_type=Draft)
    edited = ledger.record_edit(fixture["workflow_id"], 1, fixture["edit"], value_type=Draft)

    restarted = RevisionLedger(path)
    selected = restarted.select_next_base(fixture["workflow_id"], 2, value_type=Draft)

    assert selected.value_sha256 == edited.value_sha256
    assert selected.base_revision_id == edited.revision_id

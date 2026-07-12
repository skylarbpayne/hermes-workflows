from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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

    with pytest.raises(
        RevisionError,
        match="duplicate revision slot: workflow=wf_revision attempt=1 kind=output",
    ):
        RevisionLedger(path)


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

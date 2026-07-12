from __future__ import annotations

import json
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


def test_without_edit_the_generated_output_is_the_next_base(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")
    output = ledger.record_output("wf_revision", 1, Draft("Draft", 1), value_type=Draft)

    selected = ledger.select_next_base("wf_revision", 2, value_type=Draft)

    assert selected.value == Draft("Draft", 1)
    assert selected.base_revision_id == output.revision_id
    assert selected.parent_revision_id == output.revision_id


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

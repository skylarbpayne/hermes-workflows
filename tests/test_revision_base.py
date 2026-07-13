from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Optional, TypedDict, Union

try:
    from typing import NotRequired, Required
except ImportError:  # pragma: no cover - exercised by the Python 3.9 test job.
    from typing_extensions import NotRequired, Required

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


class WrappedTypedDraft(TypedDict):
    title: Required[str]
    score: NotRequired[int]


@dataclass(frozen=True)
class MappingKeyOrderDraft:
    score: int
    nested: dict[str, int]


class MappingKeyOrderTypedDraft(TypedDict):
    score: int
    nested: dict[str, int]


@dataclass(frozen=True)
class MisplacedRequiredDraft:
    score: Required[int]


@dataclass(frozen=True)
class MisplacedNotRequiredDraft:
    score: NotRequired[int]


@dataclass(frozen=True)
class SetDraft:
    tags: set[int]


@dataclass(frozen=True)
class MalformedListDraft:
    tags: object


MalformedListDraft.__annotations__["tags"] = list.__class_getitem__((int, str))


@dataclass(frozen=True)
class UnsupportedWrapperDraft:
    score: Final[int]


class UnsupportedRevisionClass:
    pass


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


@dataclass(frozen=True)
class PostponedNestedPep604LiteralDraft:
    choices: list[Literal[1] | None]


@dataclass(frozen=True)
class PostponedAnnotatedPep604LiteralDraft:
    choice: Annotated[Literal[1] | None, "revision choice"]


@dataclass(frozen=True)
class PostponedQuotedPep604LiteralDraft:
    choice: "Literal[1] | None"


@dataclass(frozen=True)
class PostponedNestedQuotedPep604LiteralDraft:
    choices: list["Literal[1] | None"]


class PostponedQuotedPep604LiteralTypedDraft(TypedDict):
    choice: "Literal[1] | None"


PostponedPep604LiteralAlias = "Literal[1] | None"


@dataclass(frozen=True)
class PostponedAliasPep604LiteralDraft:
    choice: object


PostponedAliasPep604LiteralDraft.__annotations__["choice"] = (
    "PostponedPep604LiteralAlias"
)


class PostponedPep604LiteralAliasHolder:
    Choice = "Literal[1] | None"


@dataclass(frozen=True)
class PostponedAttributeAliasPep604LiteralDraft:
    choice: object


PostponedAttributeAliasPep604LiteralDraft.__annotations__["choice"] = (
    "PostponedPep604LiteralAliasHolder.Choice"
)


PostponedNestedAnnotatedPep604LiteralAlias = Annotated[
    "Literal[1] | None", "revision choice"
]
PostponedNestedListPep604LiteralAlias = list["Literal[1] | None"]


@dataclass(frozen=True)
class PostponedNestedAnnotatedAliasPep604LiteralDraft:
    choice: object


PostponedNestedAnnotatedAliasPep604LiteralDraft.__annotations__["choice"] = (
    "PostponedNestedAnnotatedPep604LiteralAlias"
)


@dataclass(frozen=True)
class PostponedNestedListAliasPep604LiteralDraft:
    choices: object


PostponedNestedListAliasPep604LiteralDraft.__annotations__["choices"] = (
    "PostponedNestedListPep604LiteralAlias"
)


CyclicRevisionAlias = "CyclicRevisionAlias"


@dataclass(frozen=True)
class PostponedCyclicAliasDraft:
    choice: object


PostponedCyclicAliasDraft.__annotations__["choice"] = "CyclicRevisionAlias"


@dataclass(frozen=True)
class PostponedBitwiseLiteralDraft:
    choice: object
    note: str | None = None


PostponedBitwiseLiteralDraft.__annotations__["choice"] = "Literal[1 | 2]"


_side_effect_annotation_calls = 0


def _side_effect_literal_annotation():
    global _side_effect_annotation_calls
    _side_effect_annotation_calls += 1
    return Literal[1]


@dataclass(frozen=True)
class PostponedSideEffectAnnotationDraft:
    choice: object


PostponedSideEffectAnnotationDraft.__annotations__["choice"] = (
    "_side_effect_literal_annotation() | None"
)


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


class ConflictingDraftMapping(Mapping):
    def __init__(self):
        self._score_reads = 0

    def __getitem__(self, key):
        if key == "title":
            return "Draft"
        if key == "score":
            self._score_reads += 1
            return self._score_reads
        raise KeyError(key)

    def __iter__(self):
        return iter(("title", "score", "score"))

    def __len__(self):
        return 3


class DraftDict(dict):
    pass


class HostileInt(int):
    def __lt__(self, other):
        raise RuntimeError("SECRET_NUMERIC_COMPARATOR")


class StatefulMappingKey:
    def __init__(self):
        self.string_reads = 0

    def __hash__(self):
        return 1

    def __str__(self):
        self.string_reads += 1
        return f"SECRET_STATEFUL_KEY_{self.string_reads}"


class SecretStringKey(str):
    pass


class SecretIntegerKey(int):
    pass


class SecretFloatKey(float):
    pass


class StatefulStringFieldKey(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.string_reads = 0
        return instance

    def __str__(self):
        self.string_reads += 1
        return f"SECRET_STRING_FIELD_KEY_{self.string_reads}"


class StatefulFieldKey:
    def __init__(self, value):
        self.value = value
        self.string_reads = 0

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other):
        if isinstance(other, StatefulFieldKey):
            return self.value == other.value
        return self.value == other

    def __str__(self):
        self.string_reads += 1
        return f"SECRET_CUSTOM_FIELD_KEY_{self.string_reads}"


class CoercionProbe:
    def __init__(self):
        self.integer_reads = 0

    def __int__(self):
        self.integer_reads += 1
        return 1


class FailingPersistHandle:
    def __init__(self, handle, stage):
        self._handle = handle
        self._stage = stage

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._handle.close()

    def write(self, value):
        if self._stage == "write":
            self._handle.write(value[: max(1, len(value) // 2)])
            self._handle.flush()
            raise OSError("SECRET_WRITE_FAILURE")
        return self._handle.write(value)

    def flush(self):
        if self._stage == "flush":
            raise OSError("SECRET_FLUSH_FAILURE")
        return self._handle.flush()

    def fileno(self):
        return self._handle.fileno()


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


def test_not_required_typed_dict_value_is_strictly_coerced_before_persistence(tmp_path):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_wrapped_typed_dict_revision",
            1,
            {"title": "Draft", "score": "not-an-int"},
            value_type=WrappedTypedDraft,
        )

    assert ledger.revisions("wf_wrapped_typed_dict_revision") == ()
    assert not path.exists()


def test_required_and_not_required_typed_dict_keys_preserve_key_semantics(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    record = ledger.record_output(
        "wf_wrapped_typed_dict_revision",
        1,
        {"title": "Draft"},
        value_type=WrappedTypedDraft,
    )

    assert record.value == {"title": "Draft"}
    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_missing_required_typed_dict_revision",
            1,
            {"score": 1},
            value_type=WrappedTypedDraft,
        )


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        (1, Required[int]),
        (1, NotRequired[int]),
        ({"score": 1}, MisplacedRequiredDraft),
        ({"score": 1}, MisplacedNotRequiredDraft),
    ],
)
def test_required_and_not_required_outside_typed_dict_fail_closed_without_persistence(
    tmp_path, value, value_type
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_misplaced_presence_wrapper", 1, value, value_type=value_type
        )

    assert ledger.revisions("wf_misplaced_presence_wrapper") == ()
    assert not path.exists()


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        ({"tags": [1, 2]}, SetDraft),
        ({"tags": [1, 2]}, MalformedListDraft),
        ({"score": 1}, UnsupportedWrapperDraft),
    ],
)
def test_unsupported_or_malformed_generic_schema_fails_closed_without_persistence(
    tmp_path, value, value_type
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_unsupported_generic_revision", 1, value, value_type=value_type
        )

    assert ledger.revisions("wf_unsupported_generic_revision") == ()
    assert not path.exists()


@pytest.mark.parametrize(
    "value_type",
    [
        bytes,
        UnsupportedRevisionClass,
        Union[UnsupportedRevisionClass, int],
    ],
)
def test_unsupported_declared_schema_fails_closed_without_persistence(
    tmp_path, value_type
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_unsupported_declared_schema",
            1,
            {"unexpected": "mapping"},
            value_type=value_type,
        )

    assert ledger.revisions("wf_unsupported_declared_schema") == ()
    assert not path.exists()


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


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        ({"choices": [True]}, PostponedNestedPep604LiteralDraft),
        ({"choice": True}, PostponedAnnotatedPep604LiteralDraft),
        ({"choice": True}, PostponedQuotedPep604LiteralDraft),
        ({"choices": [True]}, PostponedNestedQuotedPep604LiteralDraft),
        ({"choice": True}, PostponedQuotedPep604LiteralTypedDraft),
        ({"choice": True}, PostponedAliasPep604LiteralDraft),
        ({"choice": True}, PostponedAttributeAliasPep604LiteralDraft),
        ({"choice": True}, PostponedNestedAnnotatedAliasPep604LiteralDraft),
        ({"choices": [True]}, PostponedNestedListAliasPep604LiteralDraft),
    ],
)
def test_nested_postponed_pep604_literal_unions_preserve_identity_on_python39(
    tmp_path, value, value_type
):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_nested_postponed_pep604_literal_revision",
            1,
            value,
            value_type=value_type,
        )

    assert ledger.revisions("wf_nested_postponed_pep604_literal_revision") == ()
    assert not (tmp_path / "revisions.json").exists()


def test_nested_alias_postponed_pep604_literal_unions_accept_exact_values(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    annotated = ledger.record_output(
        "wf_valid_nested_annotated_alias_revision",
        1,
        {"choice": 1},
        value_type=PostponedNestedAnnotatedAliasPep604LiteralDraft,
    )
    listed = ledger.record_output(
        "wf_valid_nested_list_alias_revision",
        1,
        {"choices": [1, None]},
        value_type=PostponedNestedListAliasPep604LiteralDraft,
    )

    assert annotated.value == PostponedNestedAnnotatedAliasPep604LiteralDraft(1)
    assert type(annotated.value.choice) is int
    assert listed.value == PostponedNestedListAliasPep604LiteralDraft([1, None])
    assert type(listed.value.choices[0]) is int


def test_unsupported_postponed_annotation_fails_closed_without_repeated_evaluation(
    tmp_path,
):
    global _side_effect_annotation_calls
    _side_effect_annotation_calls = 0
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_side_effect_annotation_revision",
            1,
            {"choice": True},
            value_type=PostponedSideEffectAnnotationDraft,
        )

    assert _side_effect_annotation_calls <= 3
    assert ledger.revisions("wf_side_effect_annotation_revision") == ()
    assert not (tmp_path / "revisions.json").exists()


def test_cyclic_postponed_alias_fails_closed_without_persistence(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_cyclic_annotation_revision",
            1,
            {"choice": True},
            value_type=PostponedCyclicAliasDraft,
        )

    assert ledger.revisions("wf_cyclic_annotation_revision") == ()
    assert not (tmp_path / "revisions.json").exists()


def test_postponed_literal_inner_bitwise_expression_is_not_rewritten_as_union(tmp_path):
    ledger = RevisionLedger(tmp_path / "revisions.json")

    with pytest.raises(RevisionValueError):
        ledger.record_output(
            "wf_bitwise_literal_revision",
            1,
            {"choice": True, "note": None},
            value_type=PostponedBitwiseLiteralDraft,
        )

    record = ledger.record_output(
        "wf_bitwise_literal_revision",
        1,
        {"choice": 3, "note": None},
        value_type=PostponedBitwiseLiteralDraft,
    )

    assert record.value == PostponedBitwiseLiteralDraft(3)
    assert type(record.value.choice) is int


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


def test_persistence_tempfile_is_private_at_creation(tmp_path, monkeypatch):
    path = tmp_path / "revisions.json"
    observed = []
    real_mkstemp = tempfile.mkstemp

    def observe_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        observed.append(
            {
                "mode": stat.S_IMODE(os.stat(name).st_mode),
                "directory": Path(name).parent,
            }
        )
        return fd, name

    monkeypatch.setattr("hermes_workflows.revision.tempfile.mkstemp", observe_mkstemp)

    RevisionLedger(path).record_output(
        "wf_private_tempfile", 1, Draft("private", 1), value_type=Draft
    )

    assert observed == [{"mode": 0o600, "directory": tmp_path}]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_persistence_tempfile_collision_does_not_overwrite_existing_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "revisions.json"
    colliding = tmp_path / f".{path.name}.collision.tmp"
    colliding.write_text("collision sentinel", encoding="utf-8")
    candidates = iter(("collision", "unique"))
    monkeypatch.setattr(
        "hermes_workflows.revision.tempfile._get_candidate_names",
        lambda: candidates,
    )

    RevisionLedger(path).record_output(
        "wf_tempfile_collision", 1, Draft("private", 1), value_type=Draft
    )

    assert colliding.read_text(encoding="utf-8") == "collision sentinel"
    assert path.exists()
    assert not (tmp_path / f".{path.name}.unique.tmp").exists()


def test_persistence_fsyncs_file_before_replace_and_parent_directory_after(
    tmp_path, monkeypatch
):
    path = tmp_path / "revisions.json"
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def observe_fsync(descriptor):
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(f"fsync:{kind}")
        return real_fsync(descriptor)

    def observe_replace(source, destination):
        events.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr("hermes_workflows.revision.os.fsync", observe_fsync)
    monkeypatch.setattr("hermes_workflows.revision.os.replace", observe_replace)

    RevisionLedger(path).record_output(
        "wf_fsync_order", 1, Draft("durable", 1), value_type=Draft
    )

    assert events == ["fsync:file", "replace", "fsync:directory"]


def test_parent_directory_fsync_failure_retry_reloads_uncertain_commit(
    tmp_path, monkeypatch
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    original = ledger.record_output(
        "wf_directory_fsync", 1, Draft("original", 1), value_type=Draft
    )
    real_fsync = os.fsync
    real_replace = os.replace
    replace_attempts = 0

    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("SECRET_DIRECTORY_FSYNC_FAILURE")
        return real_fsync(descriptor)

    def observe_replace(source, destination):
        nonlocal replace_attempts
        replace_attempts += 1
        return real_replace(source, destination)

    monkeypatch.setattr("hermes_workflows.revision.os.fsync", fail_directory_fsync)
    monkeypatch.setattr("hermes_workflows.revision.os.replace", observe_replace)

    with pytest.raises(RevisionError) as caught:
        ledger.record_edit(
            "wf_directory_fsync", 1, Draft("edited", 2), value_type=Draft
        )

    assert str(caught.value) == "revision ledger persistence failed"
    assert "SECRET_DIRECTORY_FSYNC_FAILURE" not in str(caught.value)
    assert ledger.revisions("wf_directory_fsync") == (original,)
    uncertain_records = RevisionLedger(path).revisions("wf_directory_fsync")
    uncertain_snapshot = [
        record.to_dict(include_value=True) for record in uncertain_records
    ]
    assert [record.kind for record in uncertain_records] == ["output", "edit"]
    assert uncertain_records[0] == original
    assert replace_attempts == 1

    retry_events = []

    def observe_retry_fsync(descriptor):
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        retry_events.append(f"fsync:{kind}")
        return real_fsync(descriptor)

    monkeypatch.setattr("hermes_workflows.revision.os.fsync", observe_retry_fsync)
    retried = ledger.record_edit(
        "wf_directory_fsync", 1, Draft("edited", 2), value_type=Draft
    )

    assert [
        record.to_dict(include_value=True)
        for record in ledger.revisions("wf_directory_fsync")
    ] == uncertain_snapshot
    assert retried == uncertain_records[1]
    assert [
        record.to_dict(include_value=True)
        for record in RevisionLedger(path).revisions("wf_directory_fsync")
    ] == uncertain_snapshot
    assert retry_events == ["fsync:directory"]
    assert replace_attempts == 1


def test_uncertain_commit_replay_retries_directory_fsync_until_success(
    tmp_path, monkeypatch
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    original = ledger.record_output(
        "wf_directory_fsync_retry", 1, Draft("original", 1), value_type=Draft
    )
    real_fsync = os.fsync
    real_replace = os.replace
    replace_attempts = 0

    def observe_replace(source, destination):
        nonlocal replace_attempts
        replace_attempts += 1
        return real_replace(source, destination)

    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("SECRET_DIRECTORY_FSYNC_FAILURE")
        return real_fsync(descriptor)

    monkeypatch.setattr("hermes_workflows.revision.os.replace", observe_replace)
    monkeypatch.setattr("hermes_workflows.revision.os.fsync", fail_directory_fsync)

    with pytest.raises(RevisionError, match="^revision ledger persistence failed$"):
        ledger.record_edit(
            "wf_directory_fsync_retry", 1, Draft("edited", 2), value_type=Draft
        )

    uncertain_records = RevisionLedger(path).revisions("wf_directory_fsync_retry")
    uncertain_snapshot = [
        record.to_dict(include_value=True) for record in uncertain_records
    ]
    assert [record.kind for record in uncertain_records] == ["output", "edit"]
    assert uncertain_records[0] == original
    assert replace_attempts == 1

    replay_events = []

    def fail_replay_directory_fsync(descriptor):
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        replay_events.append(f"fsync:{kind}")
        raise OSError("SECRET_REPLAY_DIRECTORY_FSYNC_FAILURE")

    monkeypatch.setattr(
        "hermes_workflows.revision.os.fsync", fail_replay_directory_fsync
    )
    with pytest.raises(RevisionError) as caught:
        ledger.record_edit(
            "wf_directory_fsync_retry", 1, Draft("edited", 2), value_type=Draft
        )

    assert str(caught.value) == "revision ledger persistence failed"
    assert "SECRET_REPLAY_DIRECTORY_FSYNC_FAILURE" not in str(caught.value)
    assert replay_events == ["fsync:directory"]
    assert replace_attempts == 1
    assert [
        record.to_dict(include_value=True)
        for record in RevisionLedger(path).revisions("wf_directory_fsync_retry")
    ] == uncertain_snapshot

    successful_replay_events = []

    def observe_successful_replay_fsync(descriptor):
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        successful_replay_events.append(f"fsync:{kind}")
        return real_fsync(descriptor)

    monkeypatch.setattr(
        "hermes_workflows.revision.os.fsync", observe_successful_replay_fsync
    )
    retried = ledger.record_edit(
        "wf_directory_fsync_retry", 1, Draft("edited", 2), value_type=Draft
    )

    assert retried == uncertain_records[1]
    assert successful_replay_events == ["fsync:directory"]
    assert replace_attempts == 1
    assert [
        record.to_dict(include_value=True)
        for record in RevisionLedger(path).revisions("wf_directory_fsync_retry")
    ] == uncertain_snapshot


@pytest.mark.parametrize("stage", ("write", "flush", "fsync", "replace"))
def test_persistence_stage_failure_removes_tempfile_and_plaintext(
    tmp_path, monkeypatch, stage
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)
    original = ledger.record_output(
        "wf_persistence_failure", 1, Draft("durable", 1), value_type=Draft
    )
    durable_bytes = path.read_bytes()
    secret = "SECRET_PLAINTEXT_" + "x" * 4096

    if stage in {"write", "flush"}:
        real_fdopen = os.fdopen

        def failing_fdopen(*args, **kwargs):
            return FailingPersistHandle(real_fdopen(*args, **kwargs), stage)

        monkeypatch.setattr("hermes_workflows.revision.os.fdopen", failing_fdopen)
    elif stage == "fsync":
        monkeypatch.setattr(
            "hermes_workflows.revision.os.fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("SECRET_FSYNC_FAILURE")),
        )
    else:
        monkeypatch.setattr(
            "hermes_workflows.revision.os.replace",
            lambda _source, _destination: (_ for _ in ()).throw(
                OSError("SECRET_REPLACE_FAILURE")
            ),
        )

    with pytest.raises(RevisionError) as caught:
        ledger.record_edit(
            "wf_persistence_failure",
            1,
            Draft(secret, 2),
            value_type=Draft,
        )

    assert str(caught.value) == "revision ledger persistence failed"
    assert secret not in str(caught.value)
    assert path.read_bytes() == durable_bytes
    assert ledger.revisions("wf_persistence_failure") == (original,)
    assert RevisionLedger(path).revisions("wf_persistence_failure") == (original,)
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []
    for candidate in tmp_path.iterdir():
        if candidate.is_file():
            assert secret not in candidate.read_text(encoding="utf-8", errors="ignore")


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


@pytest.mark.parametrize("value_type", (Draft, TypedDraft))
@pytest.mark.parametrize(
    "value",
    (ConflictingDraftMapping(), DraftDict(title="Draft", score=1)),
    ids=("custom-mapping", "dict-subclass"),
)
def test_revision_object_inputs_require_concrete_dicts_before_materialization(
    tmp_path, value_type, value
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output(
            "wf_revision_custom_mapping",
            1,
            value,
            value_type=value_type,
        )

    message = str(caught.value)
    assert message.startswith("invalid revision value for ")
    assert len(message.encode("utf-8")) <= 256
    assert ledger.revisions("wf_revision_custom_mapping") == ()
    assert not path.exists()


@pytest.mark.parametrize(
    ("value_type", "original", "invalid_value"),
    (
        (
            Draft,
            Draft("Draft", 1),
            lambda key, score: {key: "Edited", "score": score},
        ),
        (
            NestedDraft,
            NestedDraft(Draft("Draft", 1), [], (), {}, None),
            lambda key, score: {
                "primary": {key: "Edited", "score": score},
                "alternatives": [],
                "archived": (),
                "by_name": {},
                "fallback": None,
            },
        ),
        (
            TypedDraft,
            {"title": "Draft", "score": 1},
            lambda key, score: {key: "Edited", "score": score},
        ),
    ),
    ids=("dataclass", "nested-dataclass", "typed-dict"),
)
@pytest.mark.parametrize(
    "key_factory",
    (StatefulStringFieldKey, StatefulFieldKey),
    ids=("str-subclass", "custom-key"),
)
def test_schema_dict_keys_are_rejected_before_field_coercion_or_lineage_mutation(
    tmp_path, value_type, original, invalid_value, key_factory
):
    path = tmp_path / "revisions.json"
    workflow_id = "wf_revision_schema_key"
    ledger = RevisionLedger(path)
    first = ledger.record_output(workflow_id, 1, original, value_type=value_type)
    persisted = path.read_bytes()
    lineage = ledger.revisions(workflow_id)
    key = key_factory("title")
    score = CoercionProbe()

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_edit(
            workflow_id,
            1,
            invalid_value(key, score),
            value_type=value_type,
        )

    assert str(caught.value) == (
        "revision JSON object keys must be exact built-in strings"
    )
    assert "SECRET" not in str(caught.value)
    assert key.string_reads == 0
    assert score.integer_reads == 0
    assert path.read_bytes() == persisted
    assert ledger.revisions(workflow_id) == lineage == (first,)
    assert RevisionLedger(path).revisions(workflow_id) == lineage


def test_revision_values_reject_non_string_keys_before_canonical_json(tmp_path):
    non_string = {1: "first", "1": "second"}
    reduced = {"1": "second"}

    with pytest.raises(RevisionValueError) as caught:
        canonical_value_hash(non_string)
    assert str(caught.value) == (
        "revision JSON object keys must be exact built-in strings"
    )
    assert canonical_value_hash(reduced)

    ledger = RevisionLedger(tmp_path / "revisions.json")
    with pytest.raises(RevisionValueError, match="exact built-in strings"):
        ledger.record_output("wf_revision", 1, non_string, value_type=object)
    assert ledger.revisions("wf_revision") == ()


def test_declared_mapping_rejects_keys_that_collide_during_coercion(tmp_path):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output(
            "wf_revision",
            1,
            {"1": "first", "01": "second"},
            value_type=dict[int, str],
        )

    assert str(caught.value) == "revision value contains duplicate canonical object keys"
    assert ledger.revisions("wf_revision") == ()
    assert not path.exists()


def test_declared_mapping_rejects_stateful_key_before_stringification_or_persistence(
    tmp_path,
):
    path = tmp_path / "revisions.json"
    key = StatefulMappingKey()
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError) as hash_error:
        canonical_value_hash({key: 1})
    assert str(hash_error.value) == (
        "revision JSON object keys must be exact built-in strings"
    )
    assert key.string_reads == 0

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output(
            "wf_revision_stateful_key",
            1,
            {key: 1},
            value_type=dict[object, int],
        )

    message = str(caught.value)
    assert message == "revision JSON object keys must be exact built-in strings"
    assert "SECRET" not in message
    assert key.string_reads == 0
    assert ledger.revisions("wf_revision_stateful_key") == ()
    assert not path.exists()


def test_non_string_mapping_key_is_rejected_before_restart_can_change_its_type(
    tmp_path,
):
    path = tmp_path / "revisions.json"
    workflow_id = "wf_revision_non_string_key_restart"
    ledger = RevisionLedger(path)

    try:
        recorded = ledger.record_output(
            workflow_id,
            1,
            {7: {"nested": 1}},
            value_type=dict[object, dict[str, int]],
        )
    except RevisionValueError as caught:
        assert str(caught) == (
            "revision JSON object keys must be exact built-in strings"
        )
        assert ledger.revisions(workflow_id) == ()
        assert not path.exists()
        return

    restarted = RevisionLedger(path).revisions(workflow_id)[0]
    assert recorded.value == {7: {"nested": 1}}
    assert type(next(iter(recorded.value))) is int
    assert restarted.value == {"7": {"nested": 1}}
    assert type(next(iter(restarted.value))) is str
    pytest.fail(
        "accepted exact built-in int key; restart changed key from int 7 to str '7'"
    )


@pytest.mark.parametrize(
    "key",
    (SecretStringKey("secret"), SecretIntegerKey(1), SecretFloatKey(1.5)),
    ids=("str-subclass", "int-subclass", "float-subclass"),
)
def test_declared_mapping_rejects_scalar_subclass_keys_without_persistence(
    tmp_path, key
):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output(
            "wf_revision_scalar_subclass_key",
            1,
            {key: 1},
            value_type=dict[object, int],
        )

    message = str(caught.value)
    assert message == "revision JSON object keys must be exact built-in strings"
    assert "secret" not in message.lower()
    assert ledger.revisions("wf_revision_scalar_subclass_key") == ()
    assert not path.exists()


@pytest.mark.parametrize("key", (float("nan"), float("inf"), float("-inf")))
def test_declared_mapping_rejects_nonfinite_float_keys_without_persistence(tmp_path, key):
    path = tmp_path / "revisions.json"
    ledger = RevisionLedger(path)

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_output(
            "wf_revision_nonfinite_key",
            1,
            {key: 1},
            value_type=dict[object, int],
        )

    assert str(caught.value) == "revision JSON object keys must be exact built-in strings"
    assert ledger.revisions("wf_revision_nonfinite_key") == ()
    assert not path.exists()


@pytest.mark.parametrize(
    "key",
    (
        7,
        1.5,
        True,
        None,
        float("nan"),
        SecretStringKey("secret"),
        SecretIntegerKey(1),
        SecretFloatKey(1.5),
        StatefulMappingKey(),
    ),
    ids=(
        "int",
        "float",
        "bool",
        "null",
        "nonfinite-float",
        "str-subclass",
        "int-subclass",
        "float-subclass",
        "custom-stateful",
    ),
)
def test_nested_non_string_mapping_keys_leave_durable_and_in_memory_state_unchanged(
    tmp_path, key
):
    path = tmp_path / "revisions.json"
    workflow_id = "wf_revision_rejected_nested_key"
    ledger = RevisionLedger(path)
    first = ledger.record_output(
        workflow_id,
        1,
        {"nested": {"stable": 1}},
        value_type=dict[str, dict[object, int]],
    )
    persisted_bytes = path.read_bytes()
    lineage = [
        record.to_dict(include_value=True) for record in ledger.revisions(workflow_id)
    ]

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_edit(
            workflow_id,
            1,
            {"nested": {key: 2}},
            value_type=dict[str, dict[object, int]],
        )

    message = str(caught.value)
    assert message == "revision JSON object keys must be exact built-in strings"
    assert "SECRET" not in message
    if isinstance(key, StatefulMappingKey):
        assert key.string_reads == 0
    assert path.read_bytes() == persisted_bytes
    assert [
        record.to_dict(include_value=True) for record in ledger.revisions(workflow_id)
    ] == lineage == [first.to_dict(include_value=True)]
    assert [
        record.to_dict(include_value=True)
        for record in RevisionLedger(path).revisions(workflow_id)
    ] == lineage


@pytest.mark.parametrize(
    ("value_type", "invalid_value"),
    (
        (
            MappingKeyOrderDraft,
            lambda score: {"score": score, "nested": {7: 2}},
        ),
        (
            MappingKeyOrderDraft,
            lambda score: MappingKeyOrderDraft(score, {7: 2}),  # type: ignore[arg-type]
        ),
        (
            MappingKeyOrderTypedDraft,
            lambda score: {"score": score, "nested": {7: 2}},
        ),
    ),
    ids=("dataclass-dict", "dataclass-instance", "typed-dict"),
)
def test_nested_mapping_keys_are_validated_before_dataclass_or_typed_dict_coercion(
    tmp_path, value_type, invalid_value
):
    path = tmp_path / "revisions.json"
    workflow_id = "wf_revision_nested_key_order"
    ledger = RevisionLedger(path)
    first = ledger.record_output(
        workflow_id,
        1,
        {"score": 1, "nested": {"stable": 1}},
        value_type=value_type,
    )
    persisted_bytes = path.read_bytes()
    lineage = [
        record.to_dict(include_value=True) for record in ledger.revisions(workflow_id)
    ]
    score = CoercionProbe()

    with pytest.raises(RevisionValueError) as caught:
        ledger.record_edit(
            workflow_id,
            1,
            invalid_value(score),
            value_type=value_type,
        )

    assert str(caught.value) == (
        "revision JSON object keys must be exact built-in strings"
    )
    assert score.integer_reads == 0
    assert path.read_bytes() == persisted_bytes
    assert [
        record.to_dict(include_value=True) for record in ledger.revisions(workflow_id)
    ] == lineage == [first.to_dict(include_value=True)]


def test_exact_string_mapping_keys_survive_edit_restart_and_base_selection(tmp_path):
    path = tmp_path / "revisions.json"
    workflow_id = "wf_revision_exact_string_key"
    ledger = RevisionLedger(path)
    ledger.record_output(
        workflow_id,
        1,
        {"nested": {"stable": "1"}},
        value_type=dict[str, dict[str, int]],
    )
    edited = ledger.record_edit(
        workflow_id,
        1,
        {"nested": {"stable": "2"}},
        value_type=dict[str, dict[str, int]],
    )

    restarted = RevisionLedger(path)
    selected = restarted.select_next_base(
        workflow_id,
        2,
        value_type=dict[str, dict[str, int]],
    )

    assert selected.value == {"nested": {"stable": 2}}
    assert type(next(iter(selected.value))) is str
    assert type(next(iter(selected.value["nested"]))) is str
    assert selected.value_sha256 == edited.value_sha256
    assert selected.base_revision_id == edited.revision_id
    assert selected.parent_revision_id == edited.revision_id


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

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from .input_parsing import coerce_workflow_input
from .operator_services import OperatorServiceRegistry
from .projection_sections import ProjectionSectionV1, validate_workflow_id
from .types import JsonValue, to_json_value


SCHEMA_VERSION = 1
MAX_DIFF_DESCRIPTOR_BYTES = 512
REVISION_SERVICE_ID = "revision.service"


class RevisionError(ValueError):
    """Base error for invalid or conflicting revision operations."""


class RevisionValueError(RevisionError):
    """A revision value does not satisfy its declared schema."""


class RevisionConflictError(RevisionError):
    """A stable revision identity was reused with different content."""


@runtime_checkable
class RevisionServiceV1(Protocol):
    def record_output(
        self, workflow_id: str, attempt_number: int, value: object, *, value_type: Any
    ) -> "RevisionRecordV1": ...

    def record_edit(
        self, workflow_id: str, attempt_number: int, value: object, *, value_type: Any
    ) -> "RevisionRecordV1": ...

    def select_next_base(
        self, workflow_id: str, attempt_number: int, *, value_type: Any
    ) -> "RevisionRecordV1": ...


@dataclass(frozen=True)
class RevisionDiffV1:
    before_sha256: str
    after_sha256: str
    changed_leaf_count: int

    def __post_init__(self) -> None:
        _validate_hash(self.before_sha256)
        _validate_hash(self.after_sha256)
        _validate_nonnegative_int(self.changed_leaf_count)
        _validate_diff_descriptor(self)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": SCHEMA_VERSION,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "changed_leaf_count": self.changed_leaf_count,
        }


@dataclass(frozen=True)
class RevisionRecordV1:
    workflow_id: str
    attempt_number: int
    attempt_id: str
    revision_id: str
    kind: Literal["output", "edit", "base"]
    value_sha256: str
    parent_revision_id: str | None
    base_revision_id: str | None
    diff: RevisionDiffV1 | None
    value: Any = field(default=None, compare=False, repr=False)

    def to_dict(self, *, include_value: bool = False) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "attempt_number": self.attempt_number,
            "attempt_id": self.attempt_id,
            "revision_id": self.revision_id,
            "kind": self.kind,
            "value_sha256": self.value_sha256,
            "parent_revision_id": self.parent_revision_id,
            "base_revision_id": self.base_revision_id,
            "diff": self.diff.to_dict() if self.diff is not None else None,
        }
        if include_value:
            result["value"] = to_json_value(self.value)
        return result


class RevisionLedger:
    """Small durable ledger for typed revision values and their exact lineage.

    The full values are persisted for restart recovery. Operator-facing diff and
    projection descriptors expose only stable hashes and counts, never field
    names or values.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._records = self._load()

    def record_output(
        self,
        workflow_id: str,
        attempt_number: int,
        value: object,
        *,
        value_type: Any,
    ) -> RevisionRecordV1:
        workflow_id = validate_workflow_id(workflow_id)
        attempt_number = _validate_attempt_number(attempt_number)
        normalized = _coerce_value(value, value_type)
        parent = self._latest(workflow_id, attempt_number, kinds=("base",))
        if attempt_number > 1 and parent is None:
            raise RevisionError("a selected base must exist before a later-attempt output")
        record = _make_record(
            workflow_id=workflow_id,
            attempt_number=attempt_number,
            kind="output",
            value=normalized,
            parent_revision_id=parent.revision_id if parent is not None else None,
            base_revision_id=parent.revision_id if parent is not None else None,
            diff=None,
        )
        return self._append_or_existing(record)

    def record_edit(
        self,
        workflow_id: str,
        attempt_number: int,
        value: object,
        *,
        value_type: Any,
    ) -> RevisionRecordV1:
        workflow_id = validate_workflow_id(workflow_id)
        attempt_number = _validate_attempt_number(attempt_number)
        existing_edit = self._latest(workflow_id, attempt_number, kinds=("edit",))
        descendant_base = self._latest(workflow_id, attempt_number + 1, kinds=("base",))
        if descendant_base is not None and existing_edit is None:
            raise RevisionConflictError(
                f"edit for attempt {attempt_number} cannot be recorded after descendant base selection"
            )
        parent = self._latest(workflow_id, attempt_number, kinds=("output", "base"))
        if parent is None:
            raise RevisionError("an output or selected base must exist before an edit")
        normalized = _coerce_value(value, value_type)
        diff = RevisionDiffV1(
            before_sha256=parent.value_sha256,
            after_sha256=canonical_value_hash(normalized),
            changed_leaf_count=_changed_leaf_count(parent.value, normalized),
        )
        _validate_diff_descriptor(diff)
        record = _make_record(
            workflow_id=workflow_id,
            attempt_number=attempt_number,
            kind="edit",
            value=normalized,
            parent_revision_id=parent.revision_id,
            base_revision_id=parent.base_revision_id,
            diff=diff,
        )
        return self._append_or_existing(record)

    def select_next_base(
        self,
        workflow_id: str,
        attempt_number: int,
        *,
        value_type: Any,
    ) -> RevisionRecordV1:
        workflow_id = validate_workflow_id(workflow_id)
        attempt_number = _validate_attempt_number(attempt_number)
        existing = self._latest(workflow_id, attempt_number, kinds=("base",))
        if existing is not None:
            return replace(
                existing,
                value=_coerce_exact_value(
                    existing.value,
                    value_type,
                    expected_sha256=existing.value_sha256,
                ),
            )

        previous_attempt = attempt_number - 1
        if previous_attempt < 1:
            raise RevisionError("the first attempt has no prior revision base")
        chosen = self._latest(workflow_id, previous_attempt, kinds=("edit",))
        if chosen is None:
            chosen = self._latest(workflow_id, previous_attempt, kinds=("output", "base"))
        if chosen is None:
            raise RevisionError("no prior output is available as the next revision base")

        typed_value = _coerce_exact_value(
            chosen.value,
            value_type,
            expected_sha256=chosen.value_sha256,
        )
        record = _make_record(
            workflow_id=workflow_id,
            attempt_number=attempt_number,
            kind="base",
            value=typed_value,
            parent_revision_id=chosen.revision_id,
            base_revision_id=chosen.revision_id,
            diff=None,
        )
        return self._append_or_existing(record)

    def revisions(self, workflow_id: str) -> tuple[RevisionRecordV1, ...]:
        workflow_id = validate_workflow_id(workflow_id)
        return tuple(record for record in self._records if record.workflow_id == workflow_id)

    def project(self, workflow_id: str) -> tuple[ProjectionSectionV1, ...]:
        records = self.revisions(workflow_id)
        if not records:
            return ()
        latest = records[-1]
        edits = sum(record.kind == "edit" for record in records)
        return (
            ProjectionSectionV1(
                section_id="revision.summary",
                summary={
                    "revision_count": len(records),
                    "edit_count": edits,
                    "latest_attempt_id": latest.attempt_id,
                    "latest_revision_id": latest.revision_id,
                    "latest_value_sha256": latest.value_sha256,
                    "latest_kind": latest.kind,
                },
            ),
        )

    def _latest(
        self,
        workflow_id: str,
        attempt_number: int,
        *,
        kinds: tuple[str, ...],
    ) -> RevisionRecordV1 | None:
        matches = [
            record
            for record in self._records
            if record.workflow_id == workflow_id
            and record.attempt_number == attempt_number
            and record.kind in kinds
        ]
        return matches[-1] if matches else None

    def _append_or_existing(self, record: RevisionRecordV1) -> RevisionRecordV1:
        for existing in self._records:
            same_slot = (
                existing.workflow_id == record.workflow_id
                and existing.attempt_number == record.attempt_number
                and existing.kind == record.kind
            )
            if same_slot and existing.revision_id != record.revision_id:
                raise RevisionConflictError(
                    f"{record.kind} slot for attempt {record.attempt_number} already has a different revision"
                )
            if existing.revision_id != record.revision_id:
                continue
            if existing.to_dict(include_value=True) != record.to_dict(include_value=True):
                raise RevisionConflictError(
                    f"revision_id {record.revision_id} already exists with different content"
                )
            return replace(existing, value=record.value)
        self._records.append(record)
        self._persist()
        return record

    def _load(self) -> list[RevisionRecordV1]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RevisionError("revision ledger must contain valid UTF-8 JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "revisions"}:
            raise RevisionError("revision ledger has an invalid top-level shape")
        if payload["schema_version"] != SCHEMA_VERSION or not isinstance(payload["revisions"], list):
            raise RevisionError("revision ledger schema_version must equal 1")
        records = [_record_from_dict(item) for item in payload["revisions"]]
        _validate_lineage(records)
        return records

    def _persist(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "revisions": [record.to_dict(include_value=True) for record in self._records],
        }
        encoded = _canonical_json(payload) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()


def canonical_value_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(to_json_value(value)).encode("utf-8")).hexdigest()


def resolve_revision_service(registry: OperatorServiceRegistry) -> RevisionServiceV1:
    service = registry.resolve(REVISION_SERVICE_ID, SCHEMA_VERSION)
    if service is None:
        raise RevisionError(f"operator service {REVISION_SERVICE_ID!r} is not registered")
    if not isinstance(service, RevisionServiceV1):
        raise RevisionError(f"operator service {REVISION_SERVICE_ID!r} does not implement contract v1")
    return service


def _coerce_value(value: object, value_type: Any) -> Any:
    try:
        declared_fields = getattr(value_type, "__dataclass_fields__", None)
        if declared_fields is not None and isinstance(value, Mapping):
            unknown = set(value) - set(declared_fields)
            if unknown:
                raise TypeError(f"unknown revision fields: {sorted(unknown)}")
        coerced = coerce_workflow_input(value, value_type)
        to_json_value(coerced)
        return coerced
    except (TypeError, ValueError) as exc:
        field_names = getattr(value_type, "__dataclass_fields__", {})
        fields_text = ", ".join(field_names) if field_names else getattr(value_type, "__name__", str(value_type))
        raise RevisionValueError(f"invalid revision value for {fields_text}: {exc}") from exc


def _coerce_exact_value(value: object, value_type: Any, *, expected_sha256: str) -> Any:
    coerced = _coerce_value(value, value_type)
    if canonical_value_hash(coerced) != expected_sha256:
        raise RevisionValueError("declared schema must preserve the exact revision value")
    return coerced


def _make_record(
    *,
    workflow_id: str,
    attempt_number: int,
    kind: Literal["output", "edit", "base"],
    value: object,
    parent_revision_id: str | None,
    base_revision_id: str | None,
    diff: RevisionDiffV1 | None,
) -> RevisionRecordV1:
    value_sha256 = canonical_value_hash(value)
    attempt_id = _stable_id("att", {"workflow_id": workflow_id, "attempt_number": attempt_number})
    revision_id = _stable_id(
        "rev",
        {
            "workflow_id": workflow_id,
            "attempt_id": attempt_id,
            "kind": kind,
            "value_sha256": value_sha256,
            "parent_revision_id": parent_revision_id,
            "base_revision_id": base_revision_id,
        },
    )
    return RevisionRecordV1(
        workflow_id=workflow_id,
        attempt_number=attempt_number,
        attempt_id=attempt_id,
        revision_id=revision_id,
        kind=kind,
        value_sha256=value_sha256,
        parent_revision_id=parent_revision_id,
        base_revision_id=base_revision_id,
        diff=diff,
        value=value,
    )


def _record_from_dict(value: object) -> RevisionRecordV1:
    if not isinstance(value, Mapping):
        raise RevisionError("revision entries must be JSON objects")
    expected = {
        "schema_version",
        "workflow_id",
        "attempt_number",
        "attempt_id",
        "revision_id",
        "kind",
        "value_sha256",
        "parent_revision_id",
        "base_revision_id",
        "diff",
        "value",
    }
    if set(value) != expected or value["schema_version"] != SCHEMA_VERSION:
        raise RevisionError("revision entry has unknown, missing, or invalid fields")
    kind = value["kind"]
    if kind not in ("output", "edit", "base"):
        raise RevisionError("revision kind must be output, edit, or base")
    diff_value = value["diff"]
    diff = None
    if diff_value is not None:
        if not isinstance(diff_value, Mapping) or set(diff_value) != {
            "schema_version",
            "before_sha256",
            "after_sha256",
            "changed_leaf_count",
        }:
            raise RevisionError("revision diff has an invalid shape")
        diff = RevisionDiffV1(
            before_sha256=_validate_hash(diff_value["before_sha256"]),
            after_sha256=_validate_hash(diff_value["after_sha256"]),
            changed_leaf_count=_validate_nonnegative_int(diff_value["changed_leaf_count"]),
        )
        _validate_diff_descriptor(diff)
    record = RevisionRecordV1(
        workflow_id=validate_workflow_id(value["workflow_id"]),
        attempt_number=_validate_attempt_number(value["attempt_number"]),
        attempt_id=_validate_stable_id(value["attempt_id"], "att"),
        revision_id=_validate_stable_id(value["revision_id"], "rev"),
        kind=kind,
        value_sha256=_validate_hash(value["value_sha256"]),
        parent_revision_id=_validate_optional_stable_id(value["parent_revision_id"], "rev"),
        base_revision_id=_validate_optional_stable_id(value["base_revision_id"], "rev"),
        diff=diff,
        value=to_json_value(value["value"]),
    )
    expected_record = _make_record(
        workflow_id=record.workflow_id,
        attempt_number=record.attempt_number,
        kind=record.kind,
        value=record.value,
        parent_revision_id=record.parent_revision_id,
        base_revision_id=record.base_revision_id,
        diff=record.diff,
    )
    if record.attempt_id != expected_record.attempt_id or record.revision_id != expected_record.revision_id:
        raise RevisionError("revision stable identity does not match its canonical content")
    if record.value_sha256 != canonical_value_hash(record.value):
        raise RevisionError("revision value hash does not match its canonical value")
    return record


def _validate_lineage(records: list[RevisionRecordV1]) -> None:
    seen: dict[str, RevisionRecordV1] = {}
    for record in records:
        if record.revision_id in seen:
            raise RevisionError(f"duplicate revision_id: {record.revision_id}")
        if record.parent_revision_id is not None:
            parent = seen.get(record.parent_revision_id)
            if parent is None or parent.workflow_id != record.workflow_id:
                raise RevisionError("revision parent must be an earlier revision in the same workflow")
        if record.base_revision_id is not None:
            base = seen.get(record.base_revision_id)
            if base is None or base.workflow_id != record.workflow_id:
                raise RevisionError("revision base must be an earlier revision in the same workflow")
        if record.diff is not None:
            parent = seen.get(record.parent_revision_id or "")
            if parent is None or record.diff.before_sha256 != parent.value_sha256:
                raise RevisionError("revision diff before hash must match its parent")
            if record.diff.after_sha256 != record.value_sha256:
                raise RevisionError("revision diff after hash must match its value")
        if record.kind == "output":
            if record.diff is not None:
                raise RevisionError("output revisions cannot carry a diff")
            if record.attempt_number == 1:
                if record.parent_revision_id is not None or record.base_revision_id is not None:
                    raise RevisionError("first-attempt output cannot have a parent or base")
            else:
                parent = seen.get(record.parent_revision_id or "")
                if (
                    parent is None
                    or parent.kind != "base"
                    or parent.attempt_number != record.attempt_number
                    or record.base_revision_id != parent.revision_id
                ):
                    raise RevisionError("later-attempt output must descend from that attempt's selected base")
        elif record.kind == "edit":
            if any(
                descendant.kind == "base"
                and descendant.attempt_number == record.attempt_number + 1
                for descendant in seen.values()
            ):
                raise RevisionError("edit cannot follow descendant base selection")
            parent = seen.get(record.parent_revision_id or "")
            if (
                parent is None
                or parent.attempt_number != record.attempt_number
                or parent.kind not in ("output", "base")
                or record.base_revision_id != parent.base_revision_id
                or record.diff is None
            ):
                raise RevisionError("edit must descend from an output or base in the same attempt")
            expected_changed = _changed_leaf_count(parent.value, record.value)
            if record.diff.changed_leaf_count != expected_changed:
                raise RevisionError("revision diff changed-leaf count does not match durable values")
        else:
            parent = seen.get(record.parent_revision_id or "")
            prior_edits = [
                earlier
                for earlier in seen.values()
                if earlier.workflow_id == record.workflow_id
                and earlier.attempt_number == record.attempt_number - 1
                and earlier.kind == "edit"
            ]
            if prior_edits and parent != prior_edits[-1]:
                raise RevisionError(
                    "selected base must preserve the prior attempt's edited revision"
                )
            if (
                parent is None
                or parent.attempt_number != record.attempt_number - 1
                or record.base_revision_id != parent.revision_id
                or record.value_sha256 != parent.value_sha256
                or record.diff is not None
            ):
                raise RevisionError("selected base must exactly preserve the prior attempt's chosen value")
        seen[record.revision_id] = record


def _changed_leaf_count(before: object, after: object) -> int:
    if before is _MISSING or after is _MISSING:
        return 1
    before_json = to_json_value(before)
    after_json = to_json_value(after)
    if before_json == after_json:
        return 0
    if isinstance(before_json, dict) and isinstance(after_json, dict):
        keys = set(before_json) | set(after_json)
        return sum(
            _changed_leaf_count(before_json.get(key, _MISSING), after_json.get(key, _MISSING))
            for key in keys
        )
    if isinstance(before_json, list) and isinstance(after_json, list):
        length = max(len(before_json), len(after_json))
        return sum(
            _changed_leaf_count(
                before_json[index] if index < len(before_json) else _MISSING,
                after_json[index] if index < len(after_json) else _MISSING,
            )
            for index in range(length)
        )
    return 1


class _Missing:
    pass


_MISSING = _Missing()


def _stable_id(prefix: str, payload: object) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _canonical_json(value: object) -> str:
    return json.dumps(
        to_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _validate_attempt_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RevisionError("attempt_number must be a positive integer")
    return value


def _validate_nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RevisionError("changed_leaf_count must be a nonnegative integer")
    return value


def _validate_hash(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise RevisionError("revision hashes must be lowercase SHA-256 hex")
    return value


def _validate_stable_id(value: object, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(f"{prefix}_")
        or len(value) != len(prefix) + 33
        or any(char not in "0123456789abcdef" for char in value[len(prefix) + 1 :])
    ):
        raise RevisionError(f"revision identity must be a stable {prefix}_ identifier")
    return value


def _validate_optional_stable_id(value: object, prefix: str) -> str | None:
    if value is None:
        return None
    return _validate_stable_id(value, prefix)


def _validate_diff_descriptor(diff: RevisionDiffV1) -> None:
    encoded = _canonical_json(diff.to_dict()).encode("utf-8")
    if len(encoded) > MAX_DIFF_DESCRIPTOR_BYTES:
        raise RevisionError(
            f"revision diff descriptor must be <= {MAX_DIFF_DESCRIPTOR_BYTES} UTF-8 bytes"
        )

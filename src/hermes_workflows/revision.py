from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Callable,
    Literal,
    Protocol,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from .input_parsing import coerce_workflow_input
from .operator_services import OperatorServiceRegistry
from .projection_sections import ProjectionSectionV1, validate_workflow_id
from .types import JsonValue, to_json_value


SCHEMA_VERSION = 1
MAX_DIFF_DESCRIPTOR_BYTES = 512
REVISION_SERVICE_ID = "revision.service"
_MAX_PERSISTED_INTEGER_DIGITS = 4300
_MAX_PERSISTED_JSON_DEPTH = 100


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

        def build_record() -> RevisionRecordV1:
            parent = self._latest(workflow_id, attempt_number, kinds=("base",))
            if attempt_number > 1 and parent is None:
                raise RevisionError(
                    "a selected base must exist before a later-attempt output"
                )
            return _make_record(
                workflow_id=workflow_id,
                attempt_number=attempt_number,
                kind="output",
                value=normalized,
                parent_revision_id=parent.revision_id if parent is not None else None,
                base_revision_id=parent.revision_id if parent is not None else None,
                diff=None,
            )

        return self._append_or_existing(build_record)

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
        normalized = _coerce_value(value, value_type)

        def build_record() -> RevisionRecordV1:
            existing_edit = self._latest(workflow_id, attempt_number, kinds=("edit",))
            descendant_base = self._latest(
                workflow_id, attempt_number + 1, kinds=("base",)
            )
            if descendant_base is not None and existing_edit is None:
                raise RevisionConflictError(
                    f"edit for attempt {attempt_number} cannot be recorded after descendant base selection"
                )
            parent = self._latest(
                workflow_id, attempt_number, kinds=("output", "base")
            )
            if parent is None:
                raise RevisionError("an output or selected base must exist before an edit")
            diff = RevisionDiffV1(
                before_sha256=parent.value_sha256,
                after_sha256=canonical_value_hash(normalized),
                changed_leaf_count=_changed_leaf_count(parent.value, normalized),
            )
            _validate_diff_descriptor(diff)
            return _make_record(
                workflow_id=workflow_id,
                attempt_number=attempt_number,
                kind="edit",
                value=normalized,
                parent_revision_id=parent.revision_id,
                base_revision_id=parent.base_revision_id,
                diff=diff,
            )

        return self._append_or_existing(build_record)

    def select_next_base(
        self,
        workflow_id: str,
        attempt_number: int,
        *,
        value_type: Any,
    ) -> RevisionRecordV1:
        workflow_id = validate_workflow_id(workflow_id)
        attempt_number = _validate_attempt_number(attempt_number)

        def build_record() -> RevisionRecordV1:
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
                chosen = self._latest(
                    workflow_id, previous_attempt, kinds=("output",)
                )
            if chosen is None:
                raise RevisionError(
                    "a prior attempt output or edit must exist before selecting the next base"
                )

            typed_value = _coerce_exact_value(
                chosen.value,
                value_type,
                expected_sha256=chosen.value_sha256,
            )
            return _make_record(
                workflow_id=workflow_id,
                attempt_number=attempt_number,
                kind="base",
                value=typed_value,
                parent_revision_id=chosen.revision_id,
                base_revision_id=chosen.revision_id,
                diff=None,
            )

        return self._append_or_existing(build_record)

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

    def _append_or_existing(
        self, build_record: Callable[[], RevisionRecordV1]
    ) -> RevisionRecordV1:
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock:
                os.fchmod(lock.fileno(), 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                durable_records = self._load()
                self._records = durable_records
                records_after = durable_records
                record = build_record()
                for existing in durable_records:
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
                    if existing.to_dict(include_value=True) != record.to_dict(
                        include_value=True
                    ):
                        raise RevisionConflictError(
                            f"revision_id {record.revision_id} already exists with different content"
                        )
                    result = replace(existing, value=record.value)
                    break
                else:
                    records_after = [*durable_records, record]
                    _validate_lineage(records_after)
                    self._persist(records_after)
                    result = record
        except RevisionError:
            raise
        except OSError as exc:
            raise RevisionError("revision ledger persistence failed") from exc

        self._records = records_after
        return result

    def _load(self) -> list[RevisionRecordV1]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(
                self.path.read_text(encoding="utf-8"),
                parse_int=_parse_persisted_integer,
                object_pairs_hook=_reject_duplicate_json_object_keys,
            )
            _validate_persisted_json_depth(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise RevisionError("revision ledger must contain valid UTF-8 JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "revisions"}:
            raise RevisionError("revision ledger has an invalid top-level shape")
        _validate_schema_version(payload["schema_version"], level="ledger")
        if not isinstance(payload["revisions"], list):
            raise RevisionError("revision ledger revisions must be a JSON array")
        try:
            records = [_record_from_dict(item) for item in payload["revisions"]]
            _validate_lineage(records)
        except RevisionValueError as exc:
            raise RevisionError(
                "revision ledger contains invalid persisted revision data"
            ) from exc
        except RevisionError:
            raise
        except (TypeError, ValueError) as exc:
            raise RevisionError(
                "revision ledger contains invalid persisted revision data"
            ) from exc
        return records

    def _persist(self, records: list[RevisionRecordV1]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "revisions": [record.to_dict(include_value=True) for record in records],
        }
        encoded = _canonical_json(payload) + "\n"
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise RevisionError("revision ledger persistence failed") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def canonical_value_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def resolve_revision_service(registry: OperatorServiceRegistry) -> RevisionServiceV1:
    service = registry.resolve(REVISION_SERVICE_ID, SCHEMA_VERSION)
    if service is None:
        raise RevisionError(f"operator service {REVISION_SERVICE_ID!r} is not registered")
    if not isinstance(service, RevisionServiceV1):
        raise RevisionError(f"operator service {REVISION_SERVICE_ID!r} does not implement contract v1")
    return service


def _coerce_value(value: object, value_type: Any) -> Any:
    try:
        coerced = _coerce_revision_value(value, value_type)
        json_value = _revision_json_value(coerced)
    except RevisionValueError:
        raise
    except Exception as exc:
        raise RevisionValueError(
            f"invalid revision value for {_revision_value_type_label(value_type)}"
        ) from exc
    _validate_finite_json_numbers(json_value)
    return coerced


def _coerce_revision_value(value: object, value_type: Any) -> Any:
    origin = get_origin(value_type)
    args = get_args(value_type)
    union_type = getattr(types, "UnionType", None)

    if origin is Annotated:
        return _coerce_revision_value(value, args[0])

    if origin is Union or (union_type is not None and origin is union_type):
        if value is None and type(None) in args:
            return None
        matches: list[tuple[Any, Any]] = []
        validation_error: RevisionValueError | None = None
        for option in args:
            if option is type(None):
                continue
            try:
                matches.append((option, _coerce_revision_value(value, option)))
            except RevisionValueError as exc:
                if validation_error is None:
                    validation_error = exc
            except Exception:
                continue
        if not matches:
            if validation_error is not None:
                raise validation_error
            raise TypeError("revision value does not match any declared union branch")
        if len(matches) > 1:
            raise TypeError("revision value matches multiple declared union branches")
        return matches[0][1]

    _reject_unknown_dataclass_fields(value, value_type)

    if is_dataclass(value_type) and isinstance(value_type, type) and isinstance(value, Mapping):
        type_hints = _safe_revision_type_hints(value_type)
        prepared = dict(value)
        for item in fields(value_type):
            if item.name in prepared:
                prepared[item.name] = _coerce_revision_value(
                    prepared[item.name], type_hints.get(item.name, item.type)
                )
        return coerce_workflow_input(prepared, value_type)

    if origin in (list, Sequence) and isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        item_type = args[0] if args else Any
        prepared_items = [
            _coerce_revision_value(item, item_type)
            for item in cast(Sequence[object], value)
        ]
        return coerce_workflow_input(prepared_items, value_type)

    if origin is tuple and isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        sequence = cast(Sequence[object], value)
        if len(args) == 2 and args[1] is Ellipsis:
            item_types = (args[0],) * len(sequence)
        else:
            item_types = args
        prepared_items = tuple(
            _coerce_revision_value(item, item_types[index] if index < len(item_types) else Any)
            for index, item in enumerate(sequence)
        )
        return coerce_workflow_input(prepared_items, value_type)

    if origin in (dict, Mapping) and isinstance(value, Mapping):
        key_type = args[0] if args else Any
        item_type = args[1] if len(args) > 1 else Any
        prepared_mapping = {}
        for key, item in value.items():
            prepared_key = _coerce_revision_value(key, key_type)
            if prepared_key in prepared_mapping:
                raise RevisionValueError(
                    "revision value contains duplicate canonical object keys"
                )
            prepared_mapping[prepared_key] = _coerce_revision_value(item, item_type)
        return coerce_workflow_input(prepared_mapping, value_type)

    return coerce_workflow_input(value, value_type)


def _safe_revision_type_hints(value_type: type[Any]) -> dict[str, Any]:
    try:
        return get_type_hints(value_type, include_extras=True)
    except Exception:
        return dict(getattr(value_type, "__annotations__", {}) or {})


def _reject_unknown_dataclass_fields(value: object, value_type: Any) -> None:
    origin = get_origin(value_type)
    args = get_args(value_type)
    union_type = getattr(types, "UnionType", None)

    if origin is Annotated:
        _reject_unknown_dataclass_fields(value, args[0])
        return

    if origin is Union or (union_type is not None and origin is union_type):
        if value is None and type(None) in args:
            return
        validation_error: RevisionValueError | None = None
        for option in args:
            if option is type(None):
                continue
            try:
                _reject_unknown_dataclass_fields(value, option)
                _coerce_revision_value(value, option)
            except RevisionValueError as exc:
                if validation_error is None:
                    validation_error = exc
            except Exception:
                continue
            return
        if validation_error is not None:
            raise validation_error
        return

    if is_dataclass(value_type) and isinstance(value_type, type):
        declared = {item.name: item for item in fields(value_type)}
        if isinstance(value, Mapping):
            if any(key not in declared for key in value):
                raise RevisionValueError("invalid revision value: unknown revision fields")
            source = value
        elif isinstance(value, value_type):
            source = {name: getattr(value, name) for name in declared}
        else:
            return
        try:
            type_hints = get_type_hints(value_type, include_extras=True)
        except Exception:
            type_hints = dict(getattr(value_type, "__annotations__", {}) or {})
        for name, item in declared.items():
            if name in source:
                _reject_unknown_dataclass_fields(source[name], type_hints.get(name, item.type))
        return

    if origin in (list, Sequence):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            item_type = args[0] if args else Any
            for item in value:
                _reject_unknown_dataclass_fields(item, item_type)
        return

    if origin is tuple:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if len(args) == 2 and args[1] is Ellipsis:
                item_types = (args[0],) * len(value)
            else:
                item_types = args
            for index, item in enumerate(value):
                if index < len(item_types):
                    _reject_unknown_dataclass_fields(item, item_types[index])
        return

    if origin in (dict, Mapping) and isinstance(value, Mapping):
        key_type = args[0] if args else Any
        item_type = args[1] if len(args) > 1 else Any
        for key, item in value.items():
            _reject_unknown_dataclass_fields(key, key_type)
            _reject_unknown_dataclass_fields(item, item_type)


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
    if set(value) != expected:
        raise RevisionError("revision entry has unknown, missing, or invalid fields")
    _validate_schema_version(value["schema_version"], level="entry")
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
        _validate_schema_version(diff_value["schema_version"], level="diff")
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
    seen_slots: set[tuple[str, int, str]] = set()
    for record in records:
        slot = (record.workflow_id, record.attempt_number, record.kind)
        if slot in seen_slots:
            raise RevisionError("duplicate revision slot")
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
                or parent.kind not in ("output", "edit")
                or record.base_revision_id != parent.revision_id
                or record.value_sha256 != parent.value_sha256
                or record.diff is not None
            ):
                raise RevisionError(
                    "selected base must exactly preserve a prior attempt output or edit"
                )
        seen[record.revision_id] = record
        seen_slots.add(slot)


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
    try:
        return json.dumps(
            _revision_json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except RevisionValueError:
        raise
    except Exception as exc:
        raise RevisionValueError("revision value must be deterministic JSON") from exc


def _revision_json_value(value: object) -> JsonValue:
    try:
        return _normalize_revision_json_value(value, depth=0, active_ids=set())
    except RevisionValueError:
        raise
    except Exception as exc:
        raise RevisionValueError("revision value must be deterministic JSON") from exc


def _normalize_revision_json_value(
    value: object,
    *,
    depth: int,
    active_ids: set[int],
) -> JsonValue:
    if depth > _MAX_PERSISTED_JSON_DEPTH:
        raise RevisionValueError("revision value exceeds the supported JSON depth limit")
    is_dataclass_value = is_dataclass(value) and not isinstance(value, type)
    is_mapping = isinstance(value, Mapping)
    is_sequence = isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )
    if not (is_dataclass_value or is_mapping or is_sequence):
        normalized = to_json_value(value)
        if normalized is value:
            return normalized
        return _normalize_revision_json_value(
            normalized,
            depth=depth,
            active_ids=active_ids,
        )

    value_id = id(value)
    if value_id in active_ids:
        raise RevisionValueError("revision value must be acyclic JSON")
    active_ids.add(value_id)
    try:
        if is_dataclass_value:
            return {
                item.name: _normalize_revision_json_value(
                    getattr(value, item.name),
                    depth=depth + 1,
                    active_ids=active_ids,
                )
                for item in fields(cast(Any, value))
            }
        if is_mapping:
            result: dict[str, JsonValue] = {}
            for key, item in cast(Mapping[object, object], value).items():
                canonical_key = str(key)
                if canonical_key in result:
                    raise RevisionValueError(
                        "revision value contains duplicate canonical object keys"
                    )
                result[canonical_key] = _normalize_revision_json_value(
                    item,
                    depth=depth + 1,
                    active_ids=active_ids,
                )
            return result
        return [
            _normalize_revision_json_value(
                item,
                depth=depth + 1,
                active_ids=active_ids,
            )
            for item in cast(Sequence[object], value)
        ]
    finally:
        active_ids.remove(value_id)


def _revision_value_type_label(value_type: Any) -> str:
    try:
        field_names = getattr(value_type, "__dataclass_fields__", {})
        if field_names:
            label = ", ".join(str(name) for name in field_names)
        else:
            label = str(getattr(value_type, "__name__", "declared schema"))
    except Exception:
        return "declared schema"
    encoded = label.encode("utf-8")[:160]
    return encoded.decode("utf-8", errors="ignore") or "declared schema"


def _parse_persisted_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > _MAX_PERSISTED_INTEGER_DIGITS:
        raise ValueError("persisted JSON integer exceeds the supported digit limit")
    return int(value)


def _reject_duplicate_json_object_keys(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("persisted JSON contains duplicate object keys")
        result[key] = value
    return result


def _validate_persisted_json_depth(value: JsonValue) -> None:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_PERSISTED_JSON_DEPTH:
            raise ValueError("persisted JSON exceeds the supported depth limit")
        if isinstance(current, dict):
            pending.extend((nested, depth + 1) for nested in current.values())
        elif isinstance(current, list):
            pending.extend((nested, depth + 1) for nested in current)


def _validate_finite_json_numbers(value: JsonValue) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RevisionValueError("revision value must contain only finite JSON numbers")
    if isinstance(value, dict):
        for nested in value.values():
            _validate_finite_json_numbers(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_finite_json_numbers(nested)


def _validate_attempt_number(value: object) -> int:
    if type(value) is not int or value < 1:
        raise RevisionError("attempt_number must be a positive integer")
    return value


def _validate_schema_version(value: object, *, level: str) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise RevisionError(f"{level} schema_version must be the integer 1")
    return value


def _validate_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
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

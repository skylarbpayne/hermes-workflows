from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

try:
    from typing import NotRequired, Required
except ImportError:  # pragma: no cover - exercised by the Python 3.9 probe.
    from typing_extensions import NotRequired, Required


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hermes_workflows.revision import RevisionError, RevisionLedger  # noqa: E402


@dataclass(frozen=True)
class Draft:
    title: str
    score: int


class TypedDraft(TypedDict):
    title: str
    score: int


class WrappedTypedDraft(TypedDict):
    title: Required[str]
    score: NotRequired[int]


@dataclass(frozen=True)
class MisplacedRequiredDraft:
    score: Required[int]


@dataclass(frozen=True)
class MisplacedNotRequiredDraft:
    score: NotRequired[int]


def _fixture() -> tuple[Path, dict[str, object]]:
    path = REPO_ROOT / "tests" / "fixtures" / "revision_v1.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path) -> int:
    _, fixture = _fixture()
    ledger = RevisionLedger(path)
    output = ledger.record_output(
        str(fixture["workflow_id"]), 1, fixture["output"], value_type=Draft
    )
    edit = ledger.record_edit(
        str(fixture["workflow_id"]), 1, fixture["edit"], value_type=Draft
    )
    print(
        json.dumps(
            {
                "output_revision_id": output.revision_id,
                "edited_revision_id": edit.revision_id,
                "edited_v1_hash": edit.value_sha256,
                "diff": edit.diff.to_dict() if edit.diff is not None else None,
            },
            sort_keys=True,
        )
    )
    return 0


def _select(path: Path) -> int:
    _, fixture = _fixture()
    ledger = RevisionLedger(path)
    selected = ledger.select_next_base(str(fixture["workflow_id"]), 2, value_type=Draft)
    print(
        json.dumps(
            {
                "v2_attempt_id": selected.attempt_id,
                "v2_base_revision_id": selected.base_revision_id,
                "v2_base_hash": selected.value_sha256,
                "lineage": [record.to_dict() for record in ledger.revisions(str(fixture["workflow_id"]))],
            },
            sort_keys=True,
        )
    )
    return 0


def _verify_adversarial_skip_is_rejected(directory: Path) -> str:
    edited_path = directory / "edited.json"
    edited = RevisionLedger(edited_path)
    edited.record_output("wf_revision_adversarial", 1, Draft("Draft", 1), value_type=Draft)
    edited.record_edit("wf_revision_adversarial", 1, Draft("Human edit", 2), value_type=Draft)

    generated_path = directory / "generated.json"
    generated = RevisionLedger(generated_path)
    generated.record_output("wf_revision_adversarial", 1, Draft("Draft", 1), value_type=Draft)
    generated.select_next_base("wf_revision_adversarial", 2, value_type=Draft)

    edited_payload = json.loads(edited_path.read_text(encoding="utf-8"))
    generated_payload = json.loads(generated_path.read_text(encoding="utf-8"))
    edited_payload["revisions"].append(generated_payload["revisions"][-1])
    edited_path.write_text(json.dumps(edited_payload), encoding="utf-8")

    try:
        RevisionLedger(edited_path)
    except RevisionError as exc:
        message = str(exc)
        if "prior attempt's edited revision" not in message:
            raise RuntimeError(f"unexpected adversarial rejection: {message}") from exc
        return message
    raise RuntimeError("restart accepted a descendant base that skipped a prior edit")


def _verify_invalid_schema_versions_are_rejected(directory: Path) -> list[str]:
    cases = (("ledger", True), ("entry", True), ("diff", 2))
    rejections = []
    for level, invalid_version in cases:
        path = directory / f"invalid-{level}-schema.json"
        ledger = RevisionLedger(path)
        ledger.record_output("wf_revision_schema", 1, Draft("before", 1), value_type=Draft)
        ledger.record_edit("wf_revision_schema", 1, Draft("after", 2), value_type=Draft)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if level == "ledger":
            payload["schema_version"] = invalid_version
        elif level == "entry":
            payload["revisions"][0]["schema_version"] = invalid_version
        else:
            payload["revisions"][1]["diff"]["schema_version"] = invalid_version
        path.write_text(json.dumps(payload), encoding="utf-8")

        try:
            RevisionLedger(path)
        except RevisionError as exc:
            message = str(exc)
            expected = f"{level} schema_version must be the integer 1"
            if message != expected:
                raise RuntimeError(f"unexpected schema rejection: {message}") from exc
            rejections.append(message)
            continue
        raise RuntimeError(f"restart accepted invalid {level} schema_version")
    return rejections


def _verify_duplicate_slot_is_rejected(directory: Path) -> str:
    path = directory / "duplicate-slot.json"
    workflow_id = "SENSITIVE_" + "x" * 10_000
    ledger = RevisionLedger(path)
    ledger.record_output(workflow_id, 1, Draft("first", 1), value_type=Draft)

    conflicting_path = directory / "duplicate-slot-conflict.json"
    conflicting = RevisionLedger(conflicting_path)
    conflicting.record_output(workflow_id, 1, Draft("second", 2), value_type=Draft)

    payload = json.loads(path.read_text(encoding="utf-8"))
    conflicting_payload = json.loads(conflicting_path.read_text(encoding="utf-8"))
    payload["revisions"].append(conflicting_payload["revisions"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        RevisionLedger(path)
    except RevisionError as exc:
        message = str(exc)
        expected = "duplicate revision slot"
        if message != expected:
            raise RuntimeError(f"unexpected duplicate-slot rejection: {message}") from exc
        if len(message.encode("utf-8")) > 256 or "SENSITIVE" in message:
            raise RuntimeError("duplicate-slot rejection was unbounded or leaked workflow data")
        return message
    raise RuntimeError("restart accepted a duplicate workflow/attempt/kind slot")


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _verify_unfinalized_base_is_rejected(directory: Path) -> dict[str, str]:
    path = directory / "unfinalized-base.json"
    ledger = RevisionLedger(path)
    ledger.record_output("wf_revision_order", 1, Draft("First", 1), value_type=Draft)
    selected_v2 = ledger.select_next_base("wf_revision_order", 2, value_type=Draft)
    expected = "a prior attempt output or edit must exist before selecting the next base"
    try:
        ledger.select_next_base("wf_revision_order", 3, value_type=Draft)
    except RevisionError as exc:
        if str(exc) != expected:
            raise RuntimeError(f"unexpected output-order rejection: {exc}") from exc
        public_rejection = str(exc)
    else:
        raise RuntimeError("public selection propagated an unfinalized base")

    payload = json.loads(path.read_text(encoding="utf-8"))
    attempt_id = _stable_id(
        "att", {"workflow_id": "wf_revision_order", "attempt_number": 3}
    )
    stale_v3 = {
        "schema_version": 1,
        "workflow_id": "wf_revision_order",
        "attempt_number": 3,
        "attempt_id": attempt_id,
        "kind": "base",
        "value_sha256": selected_v2.value_sha256,
        "parent_revision_id": selected_v2.revision_id,
        "base_revision_id": selected_v2.revision_id,
        "diff": None,
        "value": {"title": "First", "score": 1},
    }
    stale_v3["revision_id"] = _stable_id(
        "rev",
        {
            "workflow_id": "wf_revision_order",
            "attempt_id": attempt_id,
            "kind": "base",
            "value_sha256": selected_v2.value_sha256,
            "parent_revision_id": selected_v2.revision_id,
            "base_revision_id": selected_v2.revision_id,
        },
    )
    payload["revisions"].append(stale_v3)
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        RevisionLedger(path)
    except RevisionError as exc:
        restart_expected = "selected base must exactly preserve a prior attempt output or edit"
        if str(exc) != restart_expected:
            raise RuntimeError(f"unexpected stale-base restart rejection: {exc}") from exc
        return {"public": public_rejection, "restart": str(exc)}
    raise RuntimeError("restart accepted a descendant base derived from an unfinalized base")


def _verify_typed_schema_validation(directory: Path) -> dict[str, object]:
    dataclass_path = directory / "invalid-dataclass-instance.json"
    dataclass_ledger = RevisionLedger(dataclass_path)
    try:
        dataclass_ledger.record_output(
            "wf_revision_invalid_dataclass",
            1,
            Draft("Bad", "not-an-int"),  # type: ignore[arg-type]
            value_type=Draft,
        )
    except RevisionError as exc:
        dataclass_rejection = str(exc)
    else:
        raise RuntimeError("typed revision accepted an invalid dataclass instance")
    if dataclass_ledger.revisions("wf_revision_invalid_dataclass") or dataclass_path.exists():
        raise RuntimeError("invalid dataclass instance appended revision lineage")

    typed_dict_path = directory / "invalid-typed-dict.json"
    typed_dict_ledger = RevisionLedger(typed_dict_path)
    typed_dict_rejections = []
    for value in (
        {"title": "Bad", "score": "not-an-int"},
        {"title": "Bad", "score": 2, "secret_extra": True},
    ):
        try:
            typed_dict_ledger.record_output(
                "wf_revision_invalid_typed_dict", 1, value, value_type=TypedDraft
            )
        except RevisionError as exc:
            typed_dict_rejections.append(str(exc))
            continue
        raise RuntimeError("typed revision accepted an invalid TypedDict value")
    if typed_dict_ledger.revisions("wf_revision_invalid_typed_dict") or typed_dict_path.exists():
        raise RuntimeError("invalid TypedDict value appended revision lineage")

    misplaced_path = directory / "misplaced-presence-wrapper.json"
    misplaced_ledger = RevisionLedger(misplaced_path)
    misplaced_rejections = []
    for value, value_type in (
        (1, Required[int]),
        (1, NotRequired[int]),
        ({"score": 1}, MisplacedRequiredDraft),
        ({"score": 1}, MisplacedNotRequiredDraft),
    ):
        try:
            misplaced_ledger.record_output(
                "wf_revision_misplaced_presence_wrapper",
                1,
                value,
                value_type=value_type,
            )
        except RevisionError as exc:
            misplaced_rejections.append(str(exc))
            continue
        raise RuntimeError("typed revision accepted a misplaced presence wrapper")
    if (
        misplaced_ledger.revisions("wf_revision_misplaced_presence_wrapper")
        or misplaced_path.exists()
    ):
        raise RuntimeError("misplaced presence wrapper appended revision lineage")

    valid_path = directory / "valid-typed-dict.json"
    valid = RevisionLedger(valid_path)
    valid.record_output(
        "wf_revision_valid_typed_dict",
        1,
        {"title": "Draft", "score": "1"},
        value_type=TypedDraft,
    )
    edited = valid.record_edit(
        "wf_revision_valid_typed_dict",
        1,
        {"title": "Human edit", "score": "2"},
        value_type=TypedDraft,
    )
    restarted = RevisionLedger(valid_path)
    selected = restarted.select_next_base(
        "wf_revision_valid_typed_dict", 2, value_type=TypedDraft
    )
    if selected.value != {"title": "Human edit", "score": 2}:
        raise RuntimeError("valid TypedDict did not restart with coerced values")
    if selected.value_sha256 != edited.value_sha256:
        raise RuntimeError("valid TypedDict restart did not preserve the exact edited hash")

    wrapped = valid.record_output(
        "wf_revision_valid_wrapped_typed_dict",
        1,
        {"title": "Draft"},
        value_type=WrappedTypedDraft,
    )
    if wrapped.value != {"title": "Draft"}:
        raise RuntimeError("valid TypedDict presence wrappers lost key semantics")

    return {
        "dataclass_rejection": dataclass_rejection,
        "typed_dict_rejections": typed_dict_rejections,
        "misplaced_presence_wrapper_rejections": misplaced_rejections,
        "typed_dict_edited_hash": edited.value_sha256,
        "typed_dict_base_hash": selected.value_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("write", "select"))
    parser.add_argument("--ledger")
    args = parser.parse_args(argv)
    if args.phase is not None:
        if args.ledger is None:
            parser.error("--phase requires --ledger")
        return _write(Path(args.ledger)) if args.phase == "write" else _select(Path(args.ledger))

    fixture_path, _ = _fixture()
    with tempfile.TemporaryDirectory(prefix="hw05-revision-") as temporary:
        ledger_path = Path(temporary) / "revisions.json"
        command = [sys.executable, str(Path(__file__).resolve()), "--ledger", str(ledger_path)]
        written = json.loads(
            subprocess.run(
                [*command, "--phase", "write"], check=True, capture_output=True, text=True
            ).stdout
        )
        restarted = json.loads(
            subprocess.run(
                [*command, "--phase", "select"], check=True, capture_output=True, text=True
            ).stdout
        )
        adversarial_rejection = _verify_adversarial_skip_is_rejected(Path(temporary))
        schema_rejections = _verify_invalid_schema_versions_are_rejected(Path(temporary))
        duplicate_slot_rejection = _verify_duplicate_slot_is_rejected(Path(temporary))
        unfinalized_base_rejections = _verify_unfinalized_base_is_rejected(Path(temporary))
        typed_schema_validation = _verify_typed_schema_validation(Path(temporary))

    if restarted["v2_base_hash"] != written["edited_v1_hash"]:
        raise RuntimeError("v2 base hash did not preserve the edited-v1 hash")
    if restarted["v2_base_revision_id"] != written["edited_revision_id"]:
        raise RuntimeError("v2 base revision did not point to the edited-v1 revision")

    result = {
        "schema_version": 1,
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "edited_v1_hash": written["edited_v1_hash"],
        "v2_base_hash": restarted["v2_base_hash"],
        "v2_base_revision_id": restarted["v2_base_revision_id"],
        "v2_attempt_id": restarted["v2_attempt_id"],
        "diff": written["diff"],
        "lineage": restarted["lineage"],
        "restart_processes": 2,
        "adversarial_rejection": adversarial_rejection,
        "schema_rejections": schema_rejections,
        "duplicate_slot_rejection": duplicate_slot_rejection,
        "unfinalized_base_rejections": unfinalized_base_rejections,
        "typed_schema_validation": typed_schema_validation,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

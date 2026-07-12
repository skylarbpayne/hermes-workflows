from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hermes_workflows.revision import RevisionLedger  # noqa: E402


@dataclass(frozen=True)
class Draft:
    title: str
    score: int


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
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

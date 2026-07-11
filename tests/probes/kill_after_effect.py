from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hermes_workflows.effects import (  # noqa: E402
    EffectCoordinator,
    EffectPolicy,
    SQLiteEffectStore,
    operation_identity,
)


INPUT_VALUE = {"message": "crash-safe"}
WORKFLOW_ID = "wf-crash-probe"
EFFECT_KEY = "emit"
ADAPTER_ID = "probe.file.v1"
CRASH_WINDOWS = (
    "before_adapter_call",
    "during_adapter_call",
    "after_effect_before_adapter_receipt",
    "after_receipt_commit_before_command_completion",
)


class CrashProbeAdapter:
    adapter_id = ADAPTER_ID

    def __init__(self, path: Path, crash_window: str | None):
        self.path = path
        self.crash_window = crash_window
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_effects (
                    operation_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adapter_receipts (
                    operation_id TEXT PRIMARY KEY,
                    adapter_receipt_id TEXT NOT NULL
                );
                """
            )

    def lookup_receipt(self, operation_id: str):
        with sqlite3.connect(self.path) as conn:
            external = conn.execute(
                "SELECT input_hash FROM external_effects WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if external is None:
                return None
            receipt_id = f"probe-{operation_id[-16:]}"
            conn.execute(
                "INSERT OR IGNORE INTO adapter_receipts(operation_id, adapter_receipt_id) VALUES (?, ?)",
                (operation_id, receipt_id),
            )
            conn.commit()
        return {"adapter_receipt_id": receipt_id, "operation_id": operation_id}

    def perform(self, operation_id: str, input_value):
        if self.crash_window == "during_adapter_call":
            os._exit(86)
        input_hash = hashlib.sha256(
            json.dumps(input_value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt_id = f"probe-{operation_id[-16:]}"
        with sqlite3.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO external_effects(operation_id, input_hash) VALUES (?, ?)",
                (operation_id, input_hash),
            )
            conn.commit()
        if self.crash_window == "after_effect_before_adapter_receipt":
            os._exit(86)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO adapter_receipts(operation_id, adapter_receipt_id) VALUES (?, ?)",
                (operation_id, receipt_id),
            )
            conn.commit()
        return {"adapter_receipt_id": receipt_id, "operation_id": operation_id}


def worker(effect_db: Path, adapter_db: Path, crash_window: str | None) -> int:
    store = SQLiteEffectStore(effect_db)
    identity = operation_identity(
        workflow_id=WORKFLOW_ID,
        effect_key=EFFECT_KEY,
        adapter_id=ADAPTER_ID,
        input_value=INPUT_VALUE,
    )
    record = store.get(identity.operation_id)
    if record.state == "completed":
        return 0
    claim = store.claim(identity.operation_id, ttl_seconds=0.05)
    if crash_window == "before_adapter_call":
        os._exit(86)
    completed = EffectCoordinator(store).execute_claimed(
        record,
        claim,
        CrashProbeAdapter(adapter_db, crash_window),
        INPUT_VALUE,
        sensitive_receipt=True,
    )
    if completed.state != "completed":
        raise RuntimeError("effect did not complete")
    if crash_window == "after_receipt_commit_before_command_completion":
        os._exit(86)
    return 0


def run_scenario(root: Path, crash_window: str) -> dict[str, object]:
    scenario = root / crash_window
    scenario.mkdir(parents=True)
    effect_db = scenario / "effects.sqlite"
    adapter_db = scenario / "adapter.sqlite"
    store = SQLiteEffectStore(effect_db)
    identity = operation_identity(
        workflow_id=WORKFLOW_ID,
        effect_key=EFFECT_KEY,
        adapter_id=ADAPTER_ID,
        input_value=INPUT_VALUE,
    )
    store.ensure_intent(identity, EffectPolicy.IDEMPOTENT, INPUT_VALUE)
    base = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        str(effect_db),
        str(adapter_db),
    ]
    first = subprocess.run([*base, "--crash-window", crash_window], check=False)
    if first.returncode != 86:
        raise RuntimeError(f"first worker did not crash at {crash_window}: {first.returncode}")
    time.sleep(0.08)
    second = subprocess.run(base, check=False)
    if second.returncode != 0:
        raise RuntimeError(f"recovery worker failed at {crash_window}: {second.returncode}")

    record = store.get(identity.operation_id)
    with sqlite3.connect(adapter_db) as conn:
        external_count = conn.execute("SELECT COUNT(*) FROM external_effects").fetchone()[0]
        adapter_receipt_count = conn.execute("SELECT COUNT(*) FROM adapter_receipts").fetchone()[0]
    with sqlite3.connect(effect_db) as conn:
        local_receipt_count = conn.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()[0]
        fencing_row = conn.execute(
            "SELECT state, attempts, claim_token FROM effect_intents WHERE operation_id = ?",
            (identity.operation_id,),
        ).fetchone()
    result = {
        "window": crash_window,
        "operation_id": identity.operation_id,
        "input_hash": identity.input_hash,
        "attempts": 2,
        "claim_attempts": record.attempts,
        "external_effect_count": external_count,
        "adapter_receipt_count": adapter_receipt_count,
        "completed_receipt_count": local_receipt_count,
        "state": record.state,
        "fencing_row": [
            fencing_row[0],
            fencing_row[1],
            hashlib.sha256(fencing_row[2].encode()).hexdigest(),
        ],
    }
    if external_count != 1 or adapter_receipt_count != 1 or local_receipt_count != 1:
        raise RuntimeError(f"duplicate or missing effect receipt: {result}")
    if record.state != "completed" or result["attempts"] < 2:
        raise RuntimeError(f"recovery did not converge: {result}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("effect_db", nargs="?")
    parser.add_argument("adapter_db", nargs="?")
    parser.add_argument("--crash-window", choices=CRASH_WINDOWS)
    args = parser.parse_args(argv)
    if args.worker:
        if args.effect_db is None or args.adapter_db is None:
            parser.error("worker requires effect_db and adapter_db")
        return worker(Path(args.effect_db), Path(args.adapter_db), args.crash_window)

    with tempfile.TemporaryDirectory(prefix="hw01-crash-") as temporary:
        results = [run_scenario(Path(temporary), window) for window in CRASH_WINDOWS]
    print(json.dumps({"schema_version": 1, "scenarios": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

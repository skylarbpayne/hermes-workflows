from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hermes_workflows.sqlite_policy import (  # noqa: E402
    JsonlSQLiteDiagnosticSink,
    SQLitePolicyV1,
    apply_writable_pragmas,
    doctor_sqlite_storage,
    lease_retry_plan,
    run_with_lock_retry,
)


LEASE_SECONDS = 1.0
RENEWAL_INTERVAL_SECONDS = 0.1
LOCK_HOLD_SECONDS = 0.25


def run_probe(root: Path) -> dict[str, object]:
    database = root / "workflow.sqlite"
    diagnostics_path = root / "sqlite-diagnostics.jsonl"
    policy = SQLitePolicyV1(
        busy_timeout_ms=20,
        retry_initial_delay_ms=20,
        retry_max_delay_ms=60,
        retry_max_attempts=12,
        retry_jitter_ratio=0.0,
        lease_safety_margin_ms=50,
    )
    with sqlite3.connect(database, isolation_level=None) as setup:
        pragma_report = apply_writable_pragmas(
            setup,
            policy,
            allow_journal_mode_change=True,
        )
        setup.execute("CREATE TABLE effects (operation_id TEXT PRIMARY KEY)")

    blocker = sqlite3.connect(database, isolation_level=None)
    apply_writable_pragmas(blocker, policy)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO effects VALUES ('blocker')")

    diagnostics = JsonlSQLiteDiagnosticSink(diagnostics_path)
    worker_result: dict[str, object] = {}
    worker_error: list[BaseException] = []

    def insert_once() -> str:
        with sqlite3.connect(database, isolation_level=None) as writer:
            apply_writable_pragmas(writer, policy)
            writer.execute("INSERT INTO effects VALUES ('target-operation')")
        return "written"

    def worker() -> None:
        try:
            started = time.monotonic()
            worker_result["result"] = run_with_lock_retry(
                insert_once,
                policy=policy,
                lease_seconds=LEASE_SECONDS,
                renewal_interval_seconds=RENEWAL_INTERVAL_SECONDS,
                operation_name="renewal_contention_probe",
                diagnostic_sink=diagnostics,
                random_value=lambda: 0.5,
            )
            worker_result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        except BaseException as error:
            worker_error.append(error)

    thread = threading.Thread(target=worker, name="sqlite-contention-probe")
    thread.start()
    time.sleep(LOCK_HOLD_SECONDS)
    blocker.commit()
    blocker.close()
    thread.join(timeout=2.0)
    if thread.is_alive():
        raise RuntimeError("contention worker did not finish within the lease bound")
    if worker_error:
        raise worker_error[0]

    records = [json.loads(line) for line in diagnostics_path.read_text().splitlines()]
    events = [str(record["event"]) for record in records]
    with sqlite3.connect(database) as connection:
        target_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM effects WHERE operation_id = 'target-operation'"
            ).fetchone()[0]
        )
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if target_count != 1:
        raise RuntimeError(f"target operation was silently lost or duplicated: count={target_count}")
    if "sqlite.lock_retry" not in events or events[-1] != "sqlite.lock_recovered":
        raise RuntimeError(f"contention did not produce durable retry/recovery diagnostics: {events}")
    if LOCK_HOLD_SECONDS <= RENEWAL_INTERVAL_SECONDS:
        raise RuntimeError("probe lock must outlive one renewal interval")

    fixture = REPO_ROOT / "tests" / "fixtures" / "sqlite_policy_v1.json"
    unsupported = doctor_sqlite_storage(
        database,
        policy=policy,
        filesystem_type="nfs",
    )
    return {
        "schema_version": 1,
        "pragma_trace": pragma_report.to_dict(),
        "observed_journal_mode": journal_mode,
        "lease_bound": lease_retry_plan(
            policy,
            lease_seconds=LEASE_SECONDS,
            renewal_interval_seconds=RENEWAL_INTERVAL_SECONDS,
        ).to_dict(),
        "lock_hold_seconds": LOCK_HOLD_SECONDS,
        "renewal_interval_seconds": RENEWAL_INTERVAL_SECONDS,
        "lock_outlived_renewal_interval": LOCK_HOLD_SECONDS > RENEWAL_INTERVAL_SECONDS,
        "worker_result": worker_result,
        "diagnostic_events": events,
        "diagnostic_records": records,
        "target_operation_count": target_count,
        "unsupported_storage": {
            "filesystem_type": unsupported.filesystem_type,
            "supported": unsupported.supported,
            "failures": list(unsupported.failures),
            "wal_probe_performed": unsupported.wal_probe_performed,
        },
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hw13-sqlite-contention-") as temporary:
        result = run_probe(Path(temporary))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

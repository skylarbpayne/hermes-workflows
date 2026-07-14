from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import hermes_workflows.sqlite_policy as sqlite_policy
from hermes_workflows.sqlite_policy import (
    JsonlSQLiteDiagnosticSink,
    JournalModeChangeRequired,
    LeaseUnsafePolicy,
    SQLiteLockExhausted,
    SQLitePolicyV1,
    UnsupportedSQLiteStorage,
    WalCompatibilityError,
    apply_writable_pragmas,
    classify_sqlite_lock,
    doctor_sqlite_storage,
    lease_retry_plan,
    require_supported_sqlite_storage,
    run_with_lock_retry,
    stop_checkpoint_start_plan,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sqlite_policy_v1.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text())


def _policy() -> SQLitePolicyV1:
    return SQLitePolicyV1.from_dict(_fixture()["policy"])


def test_versioned_policy_and_lease_plan_match_frozen_fixture():
    fixture = _fixture()
    policy = _policy()

    assert policy.to_dict() == fixture["policy"]
    plan = lease_retry_plan(
        policy,
        lease_seconds=fixture["lease_case"]["lease_seconds"],
        renewal_interval_seconds=fixture["lease_case"]["renewal_interval_seconds"],
    )

    assert plan.to_dict() == fixture["lease_case"]


def test_writable_pragmas_are_explicit_queryable_and_idempotent(tmp_path):
    db = tmp_path / "workflow.sqlite"
    policy = _policy()

    with sqlite3.connect(db, isolation_level=None) as connection:
        report = apply_writable_pragmas(
            connection,
            policy,
            allow_journal_mode_change=True,
        )
        assert report.to_dict() == _fixture()["pragmas"]
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == policy.busy_timeout_ms

    with sqlite3.connect(db, isolation_level=None) as connection:
        assert apply_writable_pragmas(connection, policy).to_dict() == _fixture()["pragmas"]


def test_journal_mode_change_is_never_implicit(tmp_path):
    db = tmp_path / "workflow.sqlite"
    policy = _policy()
    with sqlite3.connect(db, isolation_level=None) as connection:
        before = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
        assert before == "delete"
        with pytest.raises(JournalModeChangeRequired) as raised:
            apply_writable_pragmas(connection, policy)
        after = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()

    assert after == before
    assert raised.value.current_mode == "delete"
    assert raised.value.requested_mode == "wal"
    assert raised.value.plan == stop_checkpoint_start_plan(db)


def test_wal_compatibility_fails_closed_when_sqlite_refuses_wal():
    policy = _policy()
    with sqlite3.connect(":memory:", isolation_level=None) as connection:
        with pytest.raises(WalCompatibilityError, match="returned journal_mode=memory"):
            apply_writable_pragmas(connection, policy, allow_journal_mode_change=True)


def test_known_lock_classification_is_narrow_and_fixture_backed():
    fixture = _fixture()
    for case in fixture["known_lock_cases"]:
        assert classify_sqlite_lock(sqlite3.OperationalError(case["message"])) == case["classification"]
    for message in fixture["non_lock_messages"]:
        assert classify_sqlite_lock(sqlite3.OperationalError(message)) is None
    assert classify_sqlite_lock(sqlite3.IntegrityError("database is locked")) is None


def test_lease_plan_truncates_attempts_before_the_safety_window():
    policy = _policy()

    plan = lease_retry_plan(policy, lease_seconds=0.5, renewal_interval_seconds=0.2)

    assert plan.budget_ms == 250.0
    assert plan.max_attempts == 3
    assert plan.max_elapsed_ms == 225.0
    assert plan.worst_case_delays_ms == (25.0, 50.0)


def test_lease_plan_rejects_a_busy_timeout_that_cannot_fit():
    policy = SQLitePolicyV1(
        busy_timeout_ms=500,
        retry_initial_delay_ms=20,
        retry_max_delay_ms=80,
        retry_max_attempts=2,
        retry_jitter_ratio=0.0,
        lease_safety_margin_ms=50,
    )

    with pytest.raises(LeaseUnsafePolicy, match="busy timeout"):
        lease_retry_plan(policy, lease_seconds=0.5, renewal_interval_seconds=0.1)


def test_known_lock_retries_with_bounded_jitter_and_durable_recovery_diagnostic(tmp_path):
    policy = _policy()
    outcomes: list[object] = [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database table is locked"),
        "written",
    ]
    sleeps: list[float] = []
    diagnostics = JsonlSQLiteDiagnosticSink(tmp_path / "sqlite-diagnostics.jsonl")

    def operation():
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    result = run_with_lock_retry(
        operation,
        policy=policy,
        lease_seconds=1.0,
        renewal_interval_seconds=0.2,
        operation_name="renew_command_lease",
        diagnostic_sink=diagnostics,
        sleep=sleeps.append,
        random_value=lambda: 1.0,
    )

    assert result == "written"
    assert sleeps == [0.025, 0.05]
    records = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
    assert [record["event"] for record in records] == [
        "sqlite.lock_retry",
        "sqlite.lock_retry",
        "sqlite.lock_recovered",
    ]
    assert records[-1]["operation"] == "renew_command_lease"
    assert records[-1]["lock_count"] == 2
    assert all(record["lease_budget_ms"] == 750.0 for record in records)


def test_lock_exhaustion_is_explicit_and_durable(tmp_path):
    policy = SQLitePolicyV1(
        busy_timeout_ms=10,
        retry_initial_delay_ms=10,
        retry_max_delay_ms=10,
        retry_max_attempts=2,
        retry_jitter_ratio=0.0,
        lease_safety_margin_ms=10,
    )
    diagnostics = JsonlSQLiteDiagnosticSink(tmp_path / "sqlite-diagnostics.jsonl")

    def locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(SQLiteLockExhausted) as raised:
        run_with_lock_retry(
            locked,
            policy=policy,
            lease_seconds=0.2,
            renewal_interval_seconds=0.05,
            operation_name="claim_command",
            diagnostic_sink=diagnostics,
            sleep=lambda _: None,
            random_value=lambda: 0.5,
        )

    assert raised.value.attempts == 2
    records = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
    assert [record["event"] for record in records] == [
        "sqlite.lock_retry",
        "sqlite.lock_exhausted",
    ]
    assert records[-1]["attempt"] == 2


@pytest.mark.parametrize(
    ("diagnostic_elapsed_seconds", "sleep_elapsed_seconds", "expected_sleeps"),
    ((0.2, 0.0, 0), (0.0, 0.2, 1)),
)
def test_retry_never_starts_after_diagnostics_or_sleep_exhaust_the_lease_budget(
    monkeypatch,
    diagnostic_elapsed_seconds,
    sleep_elapsed_seconds,
    expected_sleeps,
):
    policy = SQLitePolicyV1(
        busy_timeout_ms=10,
        retry_initial_delay_ms=10,
        retry_max_delay_ms=10,
        retry_max_attempts=2,
        retry_jitter_ratio=0.0,
        lease_safety_margin_ms=10,
    )
    now = 0.0
    operation_calls: list[float] = []
    sleeps: list[float] = []
    diagnostics: list[dict[str, object]] = []

    def monotonic():
        return now

    def operation():
        operation_calls.append(now)
        if len(operation_calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return "written"

    def emit(record):
        nonlocal now
        diagnostics.append(dict(record))
        now += diagnostic_elapsed_seconds

    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += sleep_elapsed_seconds

    monkeypatch.setattr(sqlite_policy.time, "monotonic", monotonic)

    with pytest.raises(SQLiteLockExhausted) as raised:
        run_with_lock_retry(
            operation,
            policy=policy,
            lease_seconds=0.2,
            renewal_interval_seconds=0.05,
            operation_name="claim_command",
            diagnostic_sink=emit,
            sleep=sleep,
            random_value=lambda: 0.5,
        )

    assert raised.value.attempts == 1
    assert operation_calls == [0.0]
    assert len(sleeps) == expected_sleeps
    assert [record["event"] for record in diagnostics] == [
        "sqlite.lock_retry",
        "sqlite.lock_exhausted",
    ]
    assert diagnostics[-1]["attempt"] == 1


def test_non_lock_database_errors_are_never_retried_or_swallowed():
    calls = 0

    def broken():
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        run_with_lock_retry(
            broken,
            policy=_policy(),
            lease_seconds=1.0,
            renewal_interval_seconds=0.2,
            diagnostic_sink=lambda _: pytest.fail("non-lock error must not emit a diagnostic"),
            sleep=lambda _: pytest.fail("non-lock error must not sleep"),
        )
    assert calls == 1


def test_two_connections_recover_after_contention_without_duplicate_write(tmp_path):
    db = tmp_path / "workflow.sqlite"
    policy = SQLitePolicyV1(
        busy_timeout_ms=10,
        retry_initial_delay_ms=10,
        retry_max_delay_ms=20,
        retry_max_attempts=10,
        retry_jitter_ratio=0.0,
        lease_safety_margin_ms=20,
    )
    with sqlite3.connect(db, isolation_level=None) as setup:
        apply_writable_pragmas(setup, policy, allow_journal_mode_change=True)
        setup.execute("CREATE TABLE effects (operation_id TEXT PRIMARY KEY)")

    blocker = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    apply_writable_pragmas(blocker, policy)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO effects VALUES ('blocker')")
    released = threading.Event()

    def release_lock():
        time.sleep(0.08)
        blocker.commit()
        released.set()

    thread = threading.Thread(target=release_lock)
    thread.start()
    diagnostics: list[dict[str, object]] = []
    try:
        def insert_once():
            with sqlite3.connect(db, isolation_level=None) as writer:
                apply_writable_pragmas(writer, policy)
                writer.execute("INSERT INTO effects VALUES ('target')")

        run_with_lock_retry(
            insert_once,
            policy=policy,
            lease_seconds=0.5,
            renewal_interval_seconds=0.05,
            operation_name="effect_receipt",
            diagnostic_sink=lambda record: diagnostics.append(dict(record)),
        )
    finally:
        thread.join(timeout=1.0)
        blocker.close()

    assert released.is_set()
    assert diagnostics[-1]["event"] == "sqlite.lock_recovered"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM effects WHERE operation_id = 'target'"
        ).fetchone()[0] == 1


def test_unsupported_storage_fails_doctor_before_any_wal_probe(tmp_path):
    report = doctor_sqlite_storage(
        tmp_path / "workflow.sqlite",
        policy=_policy(),
        filesystem_type="nfs",
    )

    assert report.supported is False
    assert report.failures == ("unsupported_filesystem:nfs",)
    assert report.wal_probe_performed is False
    with pytest.raises(UnsupportedSQLiteStorage, match="unsupported_filesystem:nfs"):
        require_supported_sqlite_storage(report)
    assert sorted(_fixture()["unsupported_filesystems"]) == sorted(report.unsupported_filesystems)


def test_doctor_probes_a_sibling_database_without_switching_the_target(tmp_path):
    db = tmp_path / "workflow.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE existing (value TEXT)")
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"

    report = doctor_sqlite_storage(
        db,
        policy=_policy(),
        filesystem_type="apfs",
    )

    assert report.supported is True
    assert report.wal_probe_performed is True
    assert report.wal_compatible is True
    assert report.current_journal_mode == "delete"
    assert report.journal_mode_change_required is True
    assert report.plan == stop_checkpoint_start_plan(db)
    require_supported_sqlite_storage(report)
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["workflow.sqlite"]


def test_doctor_fails_an_unreadable_target_even_when_sibling_wal_probe_passes(tmp_path):
    db = tmp_path / "workflow.sqlite"
    db.write_text("not a SQLite database")

    report = doctor_sqlite_storage(db, policy=_policy(), filesystem_type="apfs")

    assert report.supported is False
    assert report.failures == ("target_database_unreadable",)
    assert report.wal_probe_performed is True
    assert report.wal_compatible is True
    with pytest.raises(UnsupportedSQLiteStorage, match="target_database_unreadable"):
        require_supported_sqlite_storage(report)


def test_stop_checkpoint_start_plan_is_explicit_and_non_mutating(tmp_path):
    db = tmp_path / "workflow.sqlite"

    plan = stop_checkpoint_start_plan(db)

    assert [step["phase"] for step in plan] == ["stop", "checkpoint", "switch", "start", "verify"]
    assert plan[0]["requirement"] == "stop every process that can write the database"
    assert "PRAGMA wal_checkpoint(TRUNCATE)" in plan[1]["action"]
    assert plan[2]["action"] == "PRAGMA journal_mode=WAL"
    assert plan[3]["requirement"] == "start exactly one writer first, then the remaining services"
    assert plan[4]["requirement"] == "verify WAL, foreign_keys, busy_timeout, and zero lock diagnostics"
    assert not db.exists()

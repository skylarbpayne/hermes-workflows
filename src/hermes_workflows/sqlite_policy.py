from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, Tuple, Union
from urllib.parse import quote


SQLITE_POLICY_SCHEMA_VERSION = 1
SQLITE_POLICY_SERVICE_ID = "sqlite.policy"
SQLITE_POLICY_CONTRACT_VERSION = 1
_SQLITE_BUSY_CODE = 5
_SQLITE_LOCKED_CODE = 6

UNSUPPORTED_FILESYSTEMS = frozenset(
    {
        "9p",
        "afpfs",
        "cifs",
        "davfs",
        "fuse.sshfs",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smbfs",
        "sshfs",
    }
)


class SQLitePolicyError(RuntimeError):
    """Base class for explicit SQLite policy failures."""


class LeaseUnsafePolicy(SQLitePolicyError):
    """The configured SQLite wait cannot fit inside the active lease window."""


class WalCompatibilityError(SQLitePolicyError):
    """SQLite or the backing storage refused the required WAL mode."""


class JournalModeChangeRequired(SQLitePolicyError):
    def __init__(self, current_mode: str, requested_mode: str, database: Union[str, Path]):
        self.current_mode = current_mode
        self.requested_mode = requested_mode
        self.plan = stop_checkpoint_start_plan(database)
        super().__init__(
            f"journal mode change requires an offline stop/checkpoint/switch/start plan: "
            f"{current_mode} -> {requested_mode}"
        )


class SQLiteLockExhausted(SQLitePolicyError):
    def __init__(self, operation: str, attempts: int, budget_ms: float):
        self.operation = operation
        self.attempts = attempts
        self.budget_ms = budget_ms
        super().__init__(
            f"known SQLite lock exhausted bounded retry for {operation} "
            f"after {attempts} attempts within {budget_ms:g}ms lease budget"
        )


class UnsupportedSQLiteStorage(SQLitePolicyError):
    def __init__(self, failures: Tuple[str, ...]):
        self.failures = failures
        super().__init__("SQLite storage doctor failed: " + ", ".join(failures))


class SQLiteDiagnosticSink(Protocol):
    def emit(self, record: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class SQLitePolicyV1:
    schema_version: int = SQLITE_POLICY_SCHEMA_VERSION
    journal_mode: str = "wal"
    foreign_keys: bool = True
    busy_timeout_ms: int = 250
    retry_initial_delay_ms: int = 25
    retry_multiplier: float = 2.0
    retry_max_delay_ms: int = 250
    retry_max_attempts: int = 8
    retry_jitter_ratio: float = 0.2
    lease_safety_margin_ms: int = 100

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SQLITE_POLICY_SCHEMA_VERSION:
            raise ValueError("schema_version must equal 1")
        if not isinstance(self.journal_mode, str) or self.journal_mode.lower() != "wal":
            raise ValueError("journal_mode must equal 'wal'")
        object.__setattr__(self, "journal_mode", self.journal_mode.lower())
        if type(self.foreign_keys) is not bool or not self.foreign_keys:
            raise ValueError("foreign_keys must be true")
        for name in (
            "busy_timeout_ms",
            "retry_initial_delay_ms",
            "retry_max_delay_ms",
            "retry_max_attempts",
            "lease_safety_margin_ms",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be an integer > 0")
        if type(self.retry_multiplier) not in (int, float) or self.retry_multiplier < 1.0:
            raise ValueError("retry_multiplier must be a number >= 1")
        object.__setattr__(self, "retry_multiplier", float(self.retry_multiplier))
        if type(self.retry_jitter_ratio) not in (int, float):
            raise ValueError("retry_jitter_ratio must be a number")
        if not 0.0 <= float(self.retry_jitter_ratio) <= 1.0:
            raise ValueError("retry_jitter_ratio must be between 0 and 1")
        object.__setattr__(self, "retry_jitter_ratio", float(self.retry_jitter_ratio))
        if self.retry_initial_delay_ms > self.retry_max_delay_ms:
            raise ValueError("retry_initial_delay_ms must not exceed retry_max_delay_ms")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "journal_mode": self.journal_mode,
            "foreign_keys": self.foreign_keys,
            "busy_timeout_ms": self.busy_timeout_ms,
            "retry_initial_delay_ms": self.retry_initial_delay_ms,
            "retry_multiplier": self.retry_multiplier,
            "retry_max_delay_ms": self.retry_max_delay_ms,
            "retry_max_attempts": self.retry_max_attempts,
            "retry_jitter_ratio": self.retry_jitter_ratio,
            "lease_safety_margin_ms": self.lease_safety_margin_ms,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SQLitePolicyV1":
        if not isinstance(value, Mapping):
            raise TypeError("SQLite policy must be a mapping")
        expected = {
            "schema_version",
            "journal_mode",
            "foreign_keys",
            "busy_timeout_ms",
            "retry_initial_delay_ms",
            "retry_multiplier",
            "retry_max_delay_ms",
            "retry_max_attempts",
            "retry_jitter_ratio",
            "lease_safety_margin_ms",
        }
        if set(value) != expected:
            missing = sorted(expected - set(value))
            extra = sorted(set(value) - expected)
            raise ValueError(f"SQLite policy fields mismatch: missing={missing}, extra={extra}")
        return cls(**dict(value))


DEFAULT_SQLITE_POLICY = SQLitePolicyV1()


@dataclass(frozen=True)
class SQLiteRetryPlanV1:
    lease_seconds: float
    renewal_interval_seconds: float
    budget_ms: float
    max_attempts: int
    max_elapsed_ms: float
    delays_ms: Tuple[float, ...]
    worst_case_delays_ms: Tuple[float, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_seconds": self.lease_seconds,
            "renewal_interval_seconds": self.renewal_interval_seconds,
            "budget_ms": self.budget_ms,
            "max_attempts": self.max_attempts,
            "max_elapsed_ms": self.max_elapsed_ms,
            "delays_ms": list(self.delays_ms),
            "worst_case_delays_ms": list(self.worst_case_delays_ms),
        }


@dataclass(frozen=True)
class SQLitePragmaReportV1:
    journal_mode: str
    foreign_keys: int
    busy_timeout_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "journal_mode": self.journal_mode,
            "foreign_keys": self.foreign_keys,
            "busy_timeout_ms": self.busy_timeout_ms,
        }


@dataclass(frozen=True)
class SQLiteStorageDoctorReportV1:
    database_path: str
    filesystem_type: str
    supported: bool
    failures: Tuple[str, ...]
    unsupported_filesystems: Tuple[str, ...]
    wal_probe_performed: bool
    wal_compatible: bool
    current_journal_mode: Optional[str]
    journal_mode_change_required: bool
    plan: Tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SQLITE_POLICY_SCHEMA_VERSION,
            "database_path": self.database_path,
            "filesystem_type": self.filesystem_type,
            "supported": self.supported,
            "failures": list(self.failures),
            "unsupported_filesystems": list(self.unsupported_filesystems),
            "wal_probe_performed": self.wal_probe_performed,
            "wal_compatible": self.wal_compatible,
            "current_journal_mode": self.current_journal_mode,
            "journal_mode_change_required": self.journal_mode_change_required,
            "plan": list(self.plan),
        }


class JsonlSQLiteDiagnosticSink:
    """Append and fsync each bounded lock diagnostic as one canonical JSON line."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._lock = threading.Lock()

    def emit(self, record: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(record), sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())


def lease_retry_plan(
    policy: SQLitePolicyV1,
    *,
    lease_seconds: float,
    renewal_interval_seconds: float,
) -> SQLiteRetryPlanV1:
    if not isinstance(policy, SQLitePolicyV1):
        raise TypeError("policy must be SQLitePolicyV1")
    if type(lease_seconds) not in (int, float) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a number > 0")
    if type(renewal_interval_seconds) not in (int, float) or renewal_interval_seconds <= 0:
        raise ValueError("renewal_interval_seconds must be a number > 0")
    lease_seconds = float(lease_seconds)
    renewal_interval_seconds = float(renewal_interval_seconds)
    if renewal_interval_seconds >= lease_seconds:
        raise LeaseUnsafePolicy("renewal interval must be shorter than the lease")

    budget_ms = _rounded(
        (lease_seconds - renewal_interval_seconds) * 1000.0 - policy.lease_safety_margin_ms
    )
    if budget_ms <= 0:
        raise LeaseUnsafePolicy("lease safety margin leaves no SQLite retry budget")
    if policy.busy_timeout_ms > budget_ms:
        raise LeaseUnsafePolicy(
            f"busy timeout {policy.busy_timeout_ms}ms exceeds lease-safe budget {budget_ms:g}ms"
        )

    delays: list[float] = []
    worst_delays: list[float] = []
    attempts = 0
    worst_elapsed = 0.0
    for configured_attempt in range(1, policy.retry_max_attempts + 1):
        candidate = worst_elapsed + policy.busy_timeout_ms
        if candidate > budget_ms:
            break
        worst_elapsed = candidate
        attempts += 1
        if configured_attempt == policy.retry_max_attempts:
            continue
        delay = min(
            policy.retry_initial_delay_ms * (policy.retry_multiplier ** (configured_attempt - 1)),
            policy.retry_max_delay_ms,
        )
        worst_delay = delay * (1.0 + policy.retry_jitter_ratio)
        next_attempt_bound = worst_elapsed + worst_delay + policy.busy_timeout_ms
        if next_attempt_bound > budget_ms:
            break
        delays.append(_rounded(delay))
        worst_delays.append(_rounded(worst_delay))
        worst_elapsed += worst_delay

    if attempts < 1:
        raise LeaseUnsafePolicy("busy timeout cannot fit one attempt inside the lease-safe budget")
    return SQLiteRetryPlanV1(
        lease_seconds=lease_seconds,
        renewal_interval_seconds=renewal_interval_seconds,
        budget_ms=budget_ms,
        max_attempts=attempts,
        max_elapsed_ms=_rounded(worst_elapsed),
        delays_ms=tuple(delays),
        worst_case_delays_ms=tuple(worst_delays),
    )


def classify_sqlite_lock(error: BaseException) -> Optional[str]:
    """Classify only SQLite BUSY/LOCKED failures; every other DB error is non-retryable."""

    if not isinstance(error, sqlite3.OperationalError):
        return None
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in (_SQLITE_BUSY_CODE, _SQLITE_LOCKED_CODE):
        return "locked"
    message = str(error).strip().lower()
    known_prefixes = (
        "database is locked",
        "database table is locked",
        "database schema is locked",
    )
    if message.startswith(known_prefixes):
        return "locked"
    return None


def apply_writable_pragmas(
    connection: sqlite3.Connection,
    policy: SQLitePolicyV1 = DEFAULT_SQLITE_POLICY,
    *,
    allow_journal_mode_change: bool = False,
    database: Optional[Union[str, Path]] = None,
) -> SQLitePragmaReportV1:
    """Apply the canonical writable policy without silently changing journal mode."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    if not isinstance(policy, SQLitePolicyV1):
        raise TypeError("policy must be SQLitePolicyV1")
    if connection.in_transaction:
        raise SQLitePolicyError("writable PRAGMAs must be applied outside a transaction")

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {policy.busy_timeout_ms}")
    current_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    target = _connection_database(connection, database)
    if current_mode != policy.journal_mode:
        if not allow_journal_mode_change:
            raise JournalModeChangeRequired(current_mode, policy.journal_mode, target)
        returned_mode = str(
            connection.execute(f"PRAGMA journal_mode = {policy.journal_mode.upper()}").fetchone()[0]
        ).lower()
        if returned_mode != policy.journal_mode:
            raise WalCompatibilityError(
                f"SQLite returned journal_mode={returned_mode}; required {policy.journal_mode}"
            )

    report = SQLitePragmaReportV1(
        journal_mode=str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
        foreign_keys=int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
        busy_timeout_ms=int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
    )
    if report.journal_mode != policy.journal_mode:
        raise WalCompatibilityError(
            f"SQLite returned journal_mode={report.journal_mode}; required {policy.journal_mode}"
        )
    if report.foreign_keys != 1:
        raise SQLitePolicyError("SQLite refused PRAGMA foreign_keys=ON")
    if report.busy_timeout_ms != policy.busy_timeout_ms:
        raise SQLitePolicyError(
            f"SQLite busy_timeout mismatch: {report.busy_timeout_ms} != {policy.busy_timeout_ms}"
        )
    return report


def run_with_lock_retry(
    operation: Callable[[], Any],
    *,
    policy: SQLitePolicyV1 = DEFAULT_SQLITE_POLICY,
    lease_seconds: float,
    renewal_interval_seconds: float,
    operation_name: str = "sqlite_operation",
    diagnostic_sink: Union[SQLiteDiagnosticSink, Callable[[Mapping[str, Any]], None]],
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
) -> Any:
    """Retry known lock failures only, bounded by the active lease safety window."""

    if not callable(operation):
        raise TypeError("operation must be callable")
    if not isinstance(operation_name, str) or not operation_name.strip():
        raise ValueError("operation_name must be a nonblank string")
    plan = lease_retry_plan(
        policy,
        lease_seconds=lease_seconds,
        renewal_interval_seconds=renewal_interval_seconds,
    )
    started = time.monotonic()
    lock_count = 0
    last_classification = "locked"
    for attempt in range(1, plan.max_attempts + 1):
        if lock_count:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if elapsed_ms + policy.busy_timeout_ms > plan.budget_ms:
                _emit_diagnostic(
                    diagnostic_sink,
                    _diagnostic_record(
                        event="sqlite.lock_exhausted",
                        operation=operation_name,
                        attempt=attempt - 1,
                        max_attempts=plan.max_attempts,
                        lock_count=lock_count,
                        classification=last_classification,
                        elapsed_ms=elapsed_ms,
                        delay_ms=None,
                        policy=policy,
                        plan=plan,
                    ),
                )
                raise SQLiteLockExhausted(operation_name, attempt - 1, plan.budget_ms)
        try:
            result = operation()
        except sqlite3.OperationalError as error:
            classification = classify_sqlite_lock(error)
            if classification is None:
                raise
            lock_count += 1
            last_classification = classification
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if attempt >= plan.max_attempts:
                _emit_diagnostic(
                    diagnostic_sink,
                    _diagnostic_record(
                        event="sqlite.lock_exhausted",
                        operation=operation_name,
                        attempt=attempt,
                        max_attempts=plan.max_attempts,
                        lock_count=lock_count,
                        classification=classification,
                        elapsed_ms=elapsed_ms,
                        delay_ms=None,
                        policy=policy,
                        plan=plan,
                    ),
                )
                raise SQLiteLockExhausted(operation_name, attempt, plan.budget_ms) from error

            base_delay_ms = plan.delays_ms[attempt - 1]
            jitter_sample = float(random_value())
            if not 0.0 <= jitter_sample <= 1.0:
                raise ValueError("random_value must return a number between 0 and 1")
            jitter_factor = 1.0 - policy.retry_jitter_ratio + 2.0 * policy.retry_jitter_ratio * jitter_sample
            delay_ms = base_delay_ms * jitter_factor
            if elapsed_ms + delay_ms + policy.busy_timeout_ms > plan.budget_ms:
                _emit_diagnostic(
                    diagnostic_sink,
                    _diagnostic_record(
                        event="sqlite.lock_exhausted",
                        operation=operation_name,
                        attempt=attempt,
                        max_attempts=plan.max_attempts,
                        lock_count=lock_count,
                        classification=classification,
                        elapsed_ms=elapsed_ms,
                        delay_ms=None,
                        policy=policy,
                        plan=plan,
                    ),
                )
                raise SQLiteLockExhausted(operation_name, attempt, plan.budget_ms) from error
            _emit_diagnostic(
                diagnostic_sink,
                _diagnostic_record(
                    event="sqlite.lock_retry",
                    operation=operation_name,
                    attempt=attempt,
                    max_attempts=plan.max_attempts,
                    lock_count=lock_count,
                    classification=classification,
                    elapsed_ms=elapsed_ms,
                    delay_ms=delay_ms,
                    policy=policy,
                    plan=plan,
                ),
            )
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if elapsed_ms + delay_ms + policy.busy_timeout_ms > plan.budget_ms:
                _emit_diagnostic(
                    diagnostic_sink,
                    _diagnostic_record(
                        event="sqlite.lock_exhausted",
                        operation=operation_name,
                        attempt=attempt,
                        max_attempts=plan.max_attempts,
                        lock_count=lock_count,
                        classification=classification,
                        elapsed_ms=elapsed_ms,
                        delay_ms=None,
                        policy=policy,
                        plan=plan,
                    ),
                )
                raise SQLiteLockExhausted(operation_name, attempt, plan.budget_ms) from error
            sleep(delay_ms / 1000.0)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if elapsed_ms + policy.busy_timeout_ms > plan.budget_ms:
                _emit_diagnostic(
                    diagnostic_sink,
                    _diagnostic_record(
                        event="sqlite.lock_exhausted",
                        operation=operation_name,
                        attempt=attempt,
                        max_attempts=plan.max_attempts,
                        lock_count=lock_count,
                        classification=classification,
                        elapsed_ms=elapsed_ms,
                        delay_ms=None,
                        policy=policy,
                        plan=plan,
                    ),
                )
                raise SQLiteLockExhausted(operation_name, attempt, plan.budget_ms) from error
            continue
        if lock_count:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if elapsed_ms + policy.busy_timeout_ms > plan.budget_ms:
                _emit_diagnostic(
                    diagnostic_sink,
                    _diagnostic_record(
                        event="sqlite.lock_exhausted",
                        operation=operation_name,
                        attempt=attempt,
                        max_attempts=plan.max_attempts,
                        lock_count=lock_count,
                        classification=last_classification,
                        elapsed_ms=elapsed_ms,
                        delay_ms=None,
                        policy=policy,
                        plan=plan,
                    ),
                )
                raise SQLiteLockExhausted(operation_name, attempt, plan.budget_ms)
            _emit_diagnostic(
                diagnostic_sink,
                _diagnostic_record(
                    event="sqlite.lock_recovered",
                    operation=operation_name,
                    attempt=attempt,
                    max_attempts=plan.max_attempts,
                    lock_count=lock_count,
                    classification="locked",
                    elapsed_ms=elapsed_ms,
                    delay_ms=None,
                    policy=policy,
                    plan=plan,
                ),
            )
        return result
    raise AssertionError("bounded SQLite retry loop terminated without result or error")


def detect_filesystem_type(path: Union[str, Path]) -> str:
    """Return the host filesystem name for doctor output, or 'unknown' if unavailable."""

    target = Path(path)
    if not target.exists():
        target = target.parent
    commands = (
        ("stat", "-f", "%T", str(target)),
        ("stat", "-f", "-c", "%T", str(target)),
    )
    for command in commands:
        try:
            output = subprocess.check_output(command, stderr=subprocess.DEVNULL, text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            continue
        if output and "%T" not in output:
            return output.lower()
    return "unknown"


def wal_compatibility_probe(
    directory: Union[str, Path],
    policy: SQLitePolicyV1 = DEFAULT_SQLITE_POLICY,
) -> SQLitePragmaReportV1:
    """Probe WAL on a disposable sibling DB; never mutate the target workflow DB."""

    root = Path(directory)
    if not root.is_dir():
        raise SQLitePolicyError(f"SQLite database directory does not exist: {root}")
    descriptor, raw_path = tempfile.mkstemp(prefix=".hermes-wal-probe-", suffix=".sqlite", dir=root)
    os.close(descriptor)
    probe = Path(raw_path)
    try:
        with sqlite3.connect(probe, isolation_level=None) as connection:
            report = apply_writable_pragmas(
                connection,
                policy,
                allow_journal_mode_change=True,
                database=probe,
            )
            connection.execute("CREATE TABLE probe (value INTEGER NOT NULL)")
            connection.execute("INSERT INTO probe VALUES (1)")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise WalCompatibilityError(f"WAL checkpoint remained busy: {checkpoint}")
            return report
    finally:
        for candidate in (probe, Path(str(probe) + "-wal"), Path(str(probe) + "-shm")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def doctor_sqlite_storage(
    database: Union[str, Path],
    *,
    policy: SQLitePolicyV1 = DEFAULT_SQLITE_POLICY,
    filesystem_type: Optional[str] = None,
) -> SQLiteStorageDoctorReportV1:
    """Read/probe SQLite storage compatibility without changing the target journal mode."""

    database_path = Path(database).expanduser().resolve(strict=False)
    directory = database_path.parent
    fs_type = (filesystem_type or detect_filesystem_type(directory)).strip().lower()
    failures: list[str] = []
    current_mode = _read_journal_mode(database_path)
    probe_performed = False
    wal_compatible = False

    if database_path.exists() and not database_path.is_file():
        failures.append("target_database_not_regular_file")
    elif database_path.is_file() and current_mode is None:
        failures.append("target_database_unreadable")
    if not directory.is_dir():
        failures.append("database_directory_missing")
    elif fs_type in UNSUPPORTED_FILESYSTEMS:
        failures.append(f"unsupported_filesystem:{fs_type}")
    else:
        probe_performed = True
        try:
            wal_compatibility_probe(directory, policy)
        except (sqlite3.Error, SQLitePolicyError, OSError) as error:
            failures.append(f"wal_probe_failed:{type(error).__name__}")
        else:
            wal_compatible = True

    change_required = current_mode is not None and current_mode != policy.journal_mode
    plan = stop_checkpoint_start_plan(database_path) if change_required else ()
    return SQLiteStorageDoctorReportV1(
        database_path=str(database_path),
        filesystem_type=fs_type,
        supported=not failures and wal_compatible,
        failures=tuple(failures),
        unsupported_filesystems=tuple(sorted(UNSUPPORTED_FILESYSTEMS)),
        wal_probe_performed=probe_performed,
        wal_compatible=wal_compatible,
        current_journal_mode=current_mode,
        journal_mode_change_required=change_required,
        plan=plan,
    )


def require_supported_sqlite_storage(
    report: SQLiteStorageDoctorReportV1,
) -> SQLiteStorageDoctorReportV1:
    if not isinstance(report, SQLiteStorageDoctorReportV1):
        raise TypeError("report must be SQLiteStorageDoctorReportV1")
    if not report.supported:
        raise UnsupportedSQLiteStorage(report.failures)
    return report


def stop_checkpoint_start_plan(database: Union[str, Path]) -> Tuple[dict[str, str], ...]:
    target = str(Path(database).expanduser().resolve(strict=False))
    return (
        {
            "phase": "stop",
            "database": target,
            "requirement": "stop every process that can write the database",
            "action": "confirm no active writer connections remain",
        },
        {
            "phase": "checkpoint",
            "database": target,
            "requirement": "run from one dedicated offline connection and require busy=0",
            "action": "PRAGMA wal_checkpoint(TRUNCATE)",
        },
        {
            "phase": "switch",
            "database": target,
            "requirement": "change journal mode only while all services are stopped",
            "action": "PRAGMA journal_mode=WAL",
        },
        {
            "phase": "start",
            "database": target,
            "requirement": "start exactly one writer first, then the remaining services",
            "action": "reopen every writable connection through SQLitePolicyV1",
        },
        {
            "phase": "verify",
            "database": target,
            "requirement": "verify WAL, foreign_keys, busy_timeout, and zero lock diagnostics",
            "action": "run storage doctor and contention probe before restoring traffic",
        },
    )


def _read_journal_mode(database: Path) -> Optional[str]:
    if not database.is_file():
        return None
    encoded = quote(str(database), safe="/")
    try:
        with sqlite3.connect(f"file:{encoded}?mode=ro", uri=True) as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    except sqlite3.Error:
        return None


def _connection_database(
    connection: sqlite3.Connection,
    explicit: Optional[Union[str, Path]],
) -> Union[str, Path]:
    if explicit is not None:
        return explicit
    row = connection.execute("PRAGMA database_list").fetchone()
    if row is not None and len(row) >= 3 and row[2]:
        return str(row[2])
    return ":memory:"


def _emit_diagnostic(
    sink: Optional[Union[SQLiteDiagnosticSink, Callable[[Mapping[str, Any]], None]]],
    record: Mapping[str, Any],
) -> None:
    if sink is None:
        return
    emit = getattr(sink, "emit", None)
    if callable(emit):
        emit(record)
        return
    if callable(sink):
        sink(record)
        return
    raise TypeError("diagnostic_sink must be callable or provide emit(record)")


def _diagnostic_record(
    *,
    event: str,
    operation: str,
    attempt: int,
    max_attempts: int,
    lock_count: int,
    classification: str,
    elapsed_ms: float,
    delay_ms: Optional[float],
    policy: SQLitePolicyV1,
    plan: SQLiteRetryPlanV1,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": SQLITE_POLICY_SCHEMA_VERSION,
        "event": event,
        "recorded_at": time.time(),
        "operation": operation,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "lock_count": lock_count,
        "classification": classification,
        "elapsed_ms": _rounded(elapsed_ms),
        "busy_timeout_ms": policy.busy_timeout_ms,
        "lease_seconds": plan.lease_seconds,
        "renewal_interval_seconds": plan.renewal_interval_seconds,
        "lease_budget_ms": plan.budget_ms,
    }
    if delay_ms is not None:
        record["delay_ms"] = _rounded(delay_ms)
    return record


def _rounded(value: float) -> float:
    return round(float(value), 3)

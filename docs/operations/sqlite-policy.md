---
layout: page
title: SQLite contention policy
---

# SQLite contention policy

Hermes Workflows supports SQLite on a local filesystem with WAL. The policy is explicit in `hermes_workflows.sqlite_policy.SQLitePolicyV1`; integration code must not create an independent timeout, PRAGMA, lock classifier, or retry loop.

## Contract

Every writable connection must apply and verify:

- `PRAGMA journal_mode=WAL`
- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout=<policy.busy_timeout_ms>`

`apply_writable_pragmas()` is idempotent after WAL is active. It refuses to switch an existing database from another journal mode unless the caller passes the explicit offline migration gate. A refusal is `JournalModeChangeRequired`, not an invitation to change the mode while workers are running.

`SQLitePolicyV1.to_dict()` and `lease_retry_plan(...).to_dict()` are the queryable policy/status payloads. The version-1 compatibility fixture is `tests/fixtures/sqlite_policy_v1.json`.

## Lock classification and retry

Only `sqlite3.OperationalError` values identified as SQLite `BUSY`/`LOCKED`, or SQLite's known `database ... is locked` messages, are retryable. Integrity errors, full disks, missing tables, I/O errors, malformed SQL, and every unknown database error escape unchanged. Do not wrap broad database failures in this retry helper.

`run_with_lock_retry()` emits one durable diagnostic per retry or exhaustion decision through a supplied sink. `JsonlSQLiteDiagnosticSink` appends canonical JSON and calls `fsync` for every record. Integrations may provide a database/event sink, but they must preserve the version-1 fields:

- event: `sqlite.lock_retry` or `sqlite.lock_exhausted`
- operation and attempt/max-attempt counts
- lock count and classification
- busy timeout, lease, renewal interval, and lease-safe budget
- elapsed time and the selected delay when retrying

Exhaustion raises `SQLiteLockExhausted`; it is never reported as success and never silently discarded. Version 1 does not emit `sqlite.lock_recovered`: a prior durable `sqlite.lock_retry` record plus the operation's normal result is the recovery trace.

## Lease-safe bound

Let:

- `L` be the remaining lease duration
- `R` be the renewal interval
- `M` be the configured safety margin
- `B` be the SQLite busy timeout for each attempt
- `D_i` be each exponential delay at maximum positive jitter

The retry budget is:

`budget_ms = (L - R) * 1000 - M`

The plan admits only the largest prefix of attempts for which:

`attempts * B + sum(D_i) <= budget_ms`

HW-13 v1 is an admission-bounded retry policy, not a completion or publication deadline. The usable lease budget controls whether a retry may begin. Before every retry after the first, `run_with_lock_retry()` durably emits `sqlite.lock_retry`, applies only bounded jitter and sleep, re-reads monotonic time immediately before invoking the operation, and invokes it only when `elapsed_ms + busy_timeout_ms <= budget_ms`. If admission fails, it durably emits `sqlite.lock_exhausted` and raises without invoking another operation.

If even one busy timeout cannot fit, `lease_retry_plan()` raises `LeaseUnsafePolicy`. Runtime elapsed time is re-read after each durable diagnostic and sleep before another operation can start, so slow diagnostics or a slow host fail explicitly instead of starting another SQLite unit outside the admission window. Jitter is bounded by `retry_jitter_ratio`; it cannot expand beyond the calculated worst case.

The operation callable must be one bounded SQLite unit using the configured busy timeout, and it must own any domain idempotency needed for retry. Once an admitted operation returns normally, that result is authoritative and is returned immediately without a subsequent diagnostic callback, elapsed-time failure, or exception translation. Version 1 does not promise wall-clock completion or publication within the lease budget. Recovered-diagnostic and publication-deadline semantics require a separately specified, transaction-aware successor.

The contention probe holds `BEGIN IMMEDIATE` longer than one renewal interval, then verifies at least one durable retry record, normal success, and exactly one target write:

```console
python tests/probes/sqlite_lock_contention.py
```

## Storage doctor

`doctor_sqlite_storage()` is read-only with respect to the target database. It reads the current journal mode, identifies the host filesystem, and runs WAL/write/checkpoint verification against a disposable sibling database. The sibling database and sidecars are removed after the probe.

Known network/distributed filesystems (`nfs`, `nfs4`, `cifs`, `smbfs`, `afpfs`, `sshfs`, `fuse.sshfs`, `9p`, `davfs`, `glusterfs`, and `lustre`) fail before the WAL probe. This is intentional: SQLite WAL requires shared-memory and locking semantics that these deployments cannot safely promise. `require_supported_sqlite_storage()` converts a failed report into `UnsupportedSQLiteStorage` for a doctor command's nonzero exit path.

An existing non-WAL database can be storage-compatible while still reporting `journal_mode_change_required=true`. That result is not healthy-for-start; execute the offline plan first.

## Stop / checkpoint / switch / start

Never switch journal mode while the service is active.

1. Stop every process that can write the database. Confirm there are no active writer connections.
2. Open one dedicated offline connection. Run `PRAGMA wal_checkpoint(TRUNCATE)` and require the returned busy count to be zero.
3. On that same controlled connection, run `PRAGMA journal_mode=WAL` and verify SQLite returns `wal`.
4. Close the migration connection. Start exactly one writer first and make it open through `SQLitePolicyV1`; then start the remaining services.
5. Run storage doctor plus the contention probe. Verify WAL, foreign keys, busy timeout, and no new lock-exhaustion diagnostics before restoring traffic.

`stop_checkpoint_start_plan(db_path)` returns the same five phases as structured data. It does not stop services or mutate a database.

## Rollback

Rollback may stop services and restore the previous application version, but it must keep lock diagnostics and unsupported-storage refusal. Do not switch away from WAL merely to roll application code back. If a journal change is genuinely required, use the same offline stop/checkpoint/switch/start sequence and preserve the database plus `-wal`/`-shm` files until the checkpoint is verified.

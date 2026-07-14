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

`run_with_lock_retry()` emits one durable diagnostic per lock decision through a supplied sink. `JsonlSQLiteDiagnosticSink` appends canonical JSON and calls `fsync` for every record. Integrations may provide a database/event sink, but they must preserve the version-1 fields:

- event: `sqlite.lock_retry`, `sqlite.lock_recovered`, or `sqlite.lock_exhausted`
- operation and attempt/max-attempt counts
- lock count and classification
- busy timeout, lease, renewal interval, and lease-safe budget
- elapsed time and the selected delay when retrying

Exhaustion raises `SQLiteLockExhausted`; it is never reported as success and never silently discarded.

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

If even one busy timeout cannot fit, `lease_retry_plan()` raises `LeaseUnsafePolicy`. Runtime elapsed time is checked again before each sleep, so a slow host fails explicitly before consuming the lease window. Jitter is bounded by `retry_jitter_ratio`; it cannot expand beyond the calculated worst case.

The contention probe holds `BEGIN IMMEDIATE` longer than one renewal interval, then verifies recovery before lease exhaustion, durable lock/recovery diagnostics, and exactly one target write:

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

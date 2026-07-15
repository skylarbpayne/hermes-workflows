# Canonical registry v2 contract

Registry v2 is the single catalog identity consumed by future CLI, plugin, and supervisor adapters. This module defines the catalog and identity service only. It does not wire those adapters, alter the legacy registry loader, write a registry, move a database, or mutate a live profile.

## Canonical schema

A v2 document has exactly these root fields:

```json
{
  "schema_version": 2,
  "state_root": "state",
  "dbs": {
    "palmer": {"path": "workflows.sqlite"}
  },
  "workflows": {
    "palmer-trip-planning": {
      "workflow_ref": "palmer_workflows.trip_planning:palmer_trip_planning_workflow",
      "db": "palmer",
      "defaults_overlay": "local"
    }
  },
  "runner": {"dbs": ["palmer"], "lease_seconds": 30}
}
```

Rules:

- `schema_version` is the integer `2`.
- `state_root` and every `dbs.<alias>.path` are normalized relative POSIX paths. Absolute, home-relative, drive-prefixed, backslash, empty-segment, `.`, `..`, NUL, and overlong values are rejected by the FND-REGLOC contract.
- A DB entry has exactly `path`; the legacy string spelling is not valid v2.
- DB, workflow, tag, runner-DB, and public consumer IDs are bounded lowercase canonical IDs. Duplicate JSON keys, tags, or runner DB aliases fail closed.
- A workflow has `workflow_ref` and `db`. `workflow_ref` has one importable `module:symbol` spelling; filesystem refs are rejected. Optional catalog metadata is `title`, `description`, `tags`, `default_input`, `trusted_resume`, `kanban_policy`, `dashboard_policy`, and `defaults_overlay` (`local` only).
- Workflow and runner DB references must name declared aliases. `runner.dbs` is nonempty and `lease_seconds` is an integer from 1 through 3600.
- Canonical output omits default-valued optional workflow fields, sorts object keys, workflow/DB aliases, tags, and runner DB aliases, normalizes strings to Unicode NFC, rejects non-finite JSON numbers, and emits compact UTF-8 JSON.

The complete fixture is `tests/fixtures/registry_v2_valid.json`.

## Resolution and identity

Given registry `/copy/.hermes/workflows.registry.json`:

1. Resolve `state_root` once from the registry directory, producing `/copy/.hermes/state`.
2. Resolve the selected DB's relative `path` beneath that state root.
3. Recheck registry, state-root, and DB containment through FND-REGLOC. Registry symlinks, intermediate symlinks, DB symlink escapes, noncanonical file paths, unstable files, and root escapes fail closed.
4. Accept a configured DB alias only. Raw public paths are never interpreted as aliases.

`RegistryCatalogV2.fingerprint` is `sha256:` plus SHA-256 of canonical normalized registry JSON. It is copy-independent: identical relocated catalogs have the same catalog fingerprint.

`RegistryIdentityV1` is the public consumer identity:

```json
{
  "schema_version": 1,
  "registry_fingerprint": "sha256:...",
  "registry_identity": "sha256:...",
  "db_alias": "palmer",
  "resolved_db_identity": "sha256:..."
}
```

The registry and resolved-DB identities are domain-separated hashes of the canonical resolved paths. They distinguish two copy-local databases with the same alias and filename without exposing private paths. Public identity contains no path field, registry filename, database filename, workflow defaults, or secret-bearing value.

Future adapters register one `RegistryIdentityServiceV1` under FND-OP service ID `registry.identity`, contract version `1`. `require_consumer_parity()` compares the exact identity returned to each named consumer. Catalog, registry-path, alias, or resolved-DB drift raises `registry_drift`; it never chooses a winner or silently falls back.

The service rereads the bounded canonical registry before each resolution. A post-load source-version or fingerprint change is drift rather than an implicit reload.

## Registry-v1 compatibility window

For one release, the parser accepts the existing read-only v1 object spelling:

- root `dbs` and `workflows`, with optional integer `schema_version: 1`;
- DB entries as a relative string or `{ "path": ... }`;
- workflow entries using the existing canonical `workflow_ref` key.

A migration-safe v1 catalog must place every DB below one shared first path component, such as `state/palmer.sqlite`. The dry-run migrator lifts that component into v2 `state_root`, converts DB entries to `{ "path": ... }`, normalizes workflow metadata, and adds the explicit runner policy over all aliases.

`dry_run_migrate_registry_file()` returns canonical target JSON, target fingerprint, and `would_write: false`. There is deliberately no write/migrate/apply function. V1 absolute paths, direct registry-directory DB files, divergent roots, traversal, aliases, or alternate workflow-ref spellings fail closed rather than changing DB identity.

Rollback order is consumers first, parser second. Drift refusal stays enabled while the v1 read window exists.

## Errors and diagnostics

All registry input, path, alias, migration, and drift failures raise `RegistryContractError` with `exit_code = 2`. Its FND-OP-style envelope is exactly:

```json
{"code":"registry_invalid","message":"...","fields":{},"conflict_id":null}
```

Messages are at most 256 UTF-8 bytes; complete envelopes are at most 4096 bytes. Parser details, rejected JSON values, registry paths, DB paths, secrets, and private defaults are never copied into diagnostics. Drift fields contain only bounded canonical consumer IDs.

Primary codes are:

- `registry_invalid`
- `registry_path_invalid`
- `registry_alias_required`
- `registry_unknown_alias`
- `registry_drift`
- `registry_invalid_consumer`
- `registry_migration_not_required`

## Verification

Focused contract:

```console
uv run pytest -q tests/test_registry_v2_contract.py tests/contracts/test_registry_consumer_contract.py
```

Location compatibility:

```console
uv run pytest -q tests/test_registry_location_contract.py tests/test_registry.py
```

The focused tests cover canonical fixture round-trips, normalized fingerprints, v1 dry-run migration without file mutation, registry-relative resolution, alias-only public identity, redaction, two similar copy-local DBs, consumer parity, exit-2 envelopes, malformed/duplicate IDs, traversal, symlink/root escape refusal, bounds, and secret/private-path non-disclosure. All tests use temporary roots or immutable fixtures; no live registry or database is read or written.

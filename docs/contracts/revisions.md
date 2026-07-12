# Revision base contract v1

Hermes Workflows stores each generated output, human edit, and selected next-attempt base as a durable revision. A later attempt must begin from the exact human-edited value when an edit exists; without an edit it begins from the generated output. “Branch” is not a separate semantic operation.

## Typed values and selection

`RevisionLedger.record_output()` and `record_edit()` coerce values through the declared workflow value type before hashing or persistence. Validation applies to JSON-like mappings, already-instantiated dataclasses, and declared `TypedDict` required fields, field types, and unknown fields. Missing, invalid, or unknown fields at any declared typed boundary (including dataclasses and `TypedDict` values nested through sequences, mappings, and optional values), hostile mapping failures, mapping keys that collide after JSON string conversion, and scalar or nested non-finite JSON numbers are rejected as bounded, nonleaking `RevisionValueError` values without appending lineage. Attempt numbers and diff counts require exact built-in integers so numeric subclasses cannot run attacker-controlled comparisons. An attempt's edit slot is finalized when the descendant attempt's base is selected: a first late edit is rejected rather than leaving the descendant on a stale generated-output base, while an idempotent replay of an edit recorded before selection still returns the existing record. `select_next_base()` chooses the latest edit from the immediately preceding attempt, otherwise that attempt's output. A selected base alone does not finalize an attempt and cannot be propagated again before that attempt records an output or edit. Recovered values are schema-coerced again after restart only when coercion preserves their exact canonical hash. Schema drift that would change the durable value fails closed without appending a base.

Attempt IDs are derived from canonical JSON containing only workflow ID and positive attempt number. Revision IDs additionally bind kind, canonical value SHA-256, parent revision ID, and base revision ID. Calling the same operation again returns the existing record; the same stable identity cannot name different content.

## Durable lineage

The file-backed v1 ledger is canonical UTF-8 JSON written through flush, `fsync`, and atomic replacement. Writers serialize through a ledger-specific file lock and reload durable state while holding it, so a stale instance cannot overwrite a conflicting slot or discard a prior revision. Filesystem write and lock failures fail closed as deterministic bounded `RevisionError` values without exposing operating-system error text. The ledger requires the exact built-in integer schema version `1` at the ledger, revision-entry, and diff levels; booleans, floats, strings, and other versions fail closed. It verifies all value hashes and stable IDs when opened. Malformed JSON, including duplicate object keys, excessive nesting, oversized numeric literals whose parser behavior differs across Python versions, malformed persisted identifiers, non-finite persisted values, and duplicate slots fail closed as deterministic bounded `RevisionError` values without leaking persisted workflow IDs, parser errors, or canonical-JSON errors. Lineage rules are strict:

- first-attempt outputs have no parent;
- a selected base points to an output or edit from the immediately preceding attempt and preserves its exact value hash; reopening rejects a base that skips an earlier edit or propagates an unfinalized selected base;
- later-attempt outputs descend from that attempt's selected base;
- edits descend from an output or selected base in the same attempt;
- parents and bases must already exist in the same workflow.
- each workflow/attempt/kind slot appears at most once, including after restart.

The complete typed value is durable because it is required to continue after restart. It is not emitted in public descriptors.

## Bounded, nonleaking descriptors

An edit's public diff descriptor contains only schema version, before/after SHA-256 values, and a changed-leaf count. It contains no field names, paths, or values and is capped at 512 UTF-8 bytes. The `revision.summary` projection likewise contains only counts, kind, stable IDs, and the latest value hash; full values remain in the ledger.

## Operator service boundary

The process-local FND-OP registry exposes this contract at service ID `revision.service`, contract version `1`. `resolve_revision_service()` rejects a missing service or an object that does not implement `RevisionServiceV1`. `RevisionLedger` is also a `ProjectionContributorV1` and emits the bounded `revision.summary` section.

## Restart evidence

`python tests/probes/revision_restart.py` creates attempt-one output and edit in one process, reopens the ledger and selects attempt two's base in another process, and exits nonzero unless the v2 base hash equals the edited-v1 hash and its base revision ID equals the edited-v1 revision ID. It also proves public selection refuses to propagate an unfinalized base and restart rejects equivalent stale persisted lineage. The trace includes fixture SHA-256, lineage IDs, hashes, bounded diff, rejection receipts, and process count; it never includes revision values.

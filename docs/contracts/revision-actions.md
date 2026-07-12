# Revision action validation contract

Contract version: `1`

Service ID: `revision.action.validator`

The framework owns one server-side validator for revision decisions. Callers resolve
that service through the operator service registry and call `validate(payload)` before
recording a response or deriving a caller-level idempotency key. A caller must not
duplicate or weaken this policy in JavaScript or workflow code.

## Invariant

`approve` accepts no `feedback` or `edited_output`. `request_changes` requires at
least one of:

- `feedback`: a string that remains nonblank after Unicode-aware `strip()`; or
- `edited_output`: a non-null JSON value. A string edit must remain nonblank after
  Unicode-aware `strip()`; empty objects and arrays are valid intentional edits.

The accepted action spellings are exactly `approve` and `request_changes`, with only
surrounding whitespace normalized. Values are never type-coerced. Unknown fields,
non-finite numbers, non-string JSON object keys, non-JSON values, and revision fields
on `approve` are rejected.

The validator snapshots every mapping before reading fields. Custom mapping iteration
and `items()` must expose the same unique string keys; an overridable `get()` is never
trusted. Accepted string values and mapping keys are copied to exact built-in strings
without invoking string subclass overrides before whitespace checks or persistence.
Accepted numeric subclasses are copied to detached exact built-in integers or floats,
without retaining subclass state in the normalized payload. Edited JSON
rejects cycles, nesting beyond 64 levels, more than 10,000 values,
strings or canonical payloads beyond 1,000,000 UTF-8 bytes, integers beyond 4,096
decimal digits, and invalid Unicode scalar values. The aggregate byte budget includes
object keys, escaped string bytes, and JSON structural bytes and is enforced before the
whole canonical payload is serialized. Every such failure is returned as a
`RevisionActionValidationError`, never as a raw encoder or recursion exception. Errors
raised while reading hostile containers or values are replaced with deterministic field
messages; exception text is never invoked or exposed.

The empty request-changes error message is exactly:

`request_changes requires nonblank feedback or valid edited_output`

The exception envelope has `code`, `message`, and ordered `field_errors`; each field
error has `field`, `code`, and `message`. Missing actionable input reports both
`feedback` and `edited_output` so every adapter can render field-level errors.

## Normalization and idempotency

Validation trims action and feedback, recursively copies edited JSON, and freezes the
returned normalized payload. It hashes canonical UTF-8 JSON with SHA-256: sorted object
keys, compact separators, literal Unicode, and no non-finite numbers. The validator
returns both the 64-character `normalized_payload_hash` and
`revision-action:v1:<normalized_payload_hash>` as the normalized idempotency key.
Equivalent mapping order and surrounding action/feedback whitespace therefore replay
to the same key; different feedback, edits, and decisions do not collide.

Caller-owned workflow/request identity is still part of the durable response command's
scope. Adapters combine that stable scope with this normalized key; they must not use
the payload key globally across unrelated review requests.

## All-entrypoint matrix

| Entrypoint | Required server-side behavior | Integration owner |
| --- | --- | --- |
| Direct engine/service | Resolve contract v1, validate before durable response recording, return the validator error envelope unchanged | `ADP-HW07-ENGINE` |
| CLI | Submit through the validated direct service; print field errors and fail without recording on invalid input | `ADP-HW07-OPERATOR` |
| Dashboard HTTP | Treat browser checks as convenience only; submit through the validated direct service and return field errors | `ADP-HW07-OPERATOR` |
| Hermes plugin/tool | Submit through the validated direct service and preserve the same message and field errors | `ADP-HW07-OPERATOR` |

No entrypoint may accept an empty `request_changes` response if another entrypoint
rejects it. Rollback disables request-changes submission rather than bypassing the
validator. Wiring these callers is intentionally outside this contract module's
allowlist and belongs to the named adapters above.

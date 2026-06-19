# Contributing

This project is still a v0 runtime spike. The bar for contributions is simple: preserve the boring durable core.

## Development setup

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=src:. pytest -q
```

## Design constraints

- Keep workflows code-first. Python deciders beat YAML slop.
- Keep the runtime boring: event history, replay, leases, approvals, memoized steps, provenance, inspectability.
- Keep agents bounded behind explicit runner interfaces.
- Preserve approval receipts and idempotency keys.
- Separate generated-code approval from external side-effect approval.
- Default examples to read-only/dry-run behavior.

## Testing expectations

Use tests for behavior, not just snapshots. For workflow changes, cover:

- replay/idempotency
- pending command behavior
- approval requested -> signal -> resumed execution
- generated workflow source/hash/provenance
- side-effect counts and fail-closed behavior

For artifact/UI changes, add regression coverage for broken HTML/code rendering. The command-center demo has tests specifically guarding against syntax-highlighter markup leakage.

## Sensitive data rule

Never commit real participant data, local snapshots, workflow DBs, generated real-run receipts, share tokens, or `.env` files. Use synthetic fixtures in tests and examples.

## Pull request checklist

- [ ] `PYTHONPATH=src:. pytest -q` passes or the PR documents the unrelated failing test.
- [ ] New workflow behavior has tests.
- [ ] No real secrets/PII/local exports are committed.
- [ ] Generated-code and side-effect approval boundaries remain explicit.
- [ ] README/docs are updated for operator-facing changes.

# Example outputs

This directory contains redacted output packets that demonstrate what a workflow run produced without committing raw private inputs.

## `hackathon-real-dry-run.redacted.json`

Generated from a private Hack the Valley participant follow-up dry run.

What it preserves:

- workflow completion status
- source row counts
- generated workflow symbol and SHA-256
- approval gate sequence
- agent/audit event counts
- draft counts, placeholder participant/project refs, and risk flags
- zero-side-effect receipt
- draft-shape metadata such as subject shape, line count, project-link presence, and omitted raw body marker

What it removes:

- participant names
- participant emails
- project titles
- raw registration rows
- raw submissions
- private receipt paths

The corresponding review artifact can be regenerated from a private receipt with:

```bash
PYTHONPATH=src:. python examples/redact_hackathon_review_packet.py \
  --receipt /path/to/private/receipt.json \
  --snapshot /path/to/private/snapshot.json \
  --out-dir dist/workflows-real-run-output
cp dist/workflows-real-run-output/packet.json examples/outputs/hackathon-real-dry-run.redacted.json
```

Do not commit raw receipts, workflow databases, snapshots, CSV exports, or share tokens.

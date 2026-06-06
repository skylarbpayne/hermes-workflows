# Hackathon redacted workflow output packet

This folder contains a public-safe derivative of a private Hack the Valley `/workflows` dry run.

It is meant to show what happened without committing raw participant data, private source exports, share tokens, or credential-bearing paths.

## Files

- `index.html` — reviewable visual packet for humans.
- `packet.json` — machine-readable redacted packet for docs, tests, and blog examples.

## What it demonstrates

- real private inputs were converted into a sanitized snapshot
- the workflow generated participant follow-up drafts
- generated workflow execution was approval-gated
- agent quality approval was recorded
- human side-effect approval was recorded
- no Gmail drafts or sends happened in dry-run mode
- unmatched participant/project joins are surfaced as blockers

## Privacy boundary

This folder must never include:

- raw registration exports
- raw submission exports
- raw receipt JSON with draft bodies
- participant emails or full names
- private artifact/share tokens
- local private run paths

Regenerate it from a private dry run with:

```bash
PYTHONPATH=src:. python examples/redact_hackathon_review_packet.py \
  --snapshot /tmp/workflows-real-run/snapshot.json \
  --receipt /tmp/workflows-real-run/receipt.json \
  --out-dir docs/output/hackathon-redacted-packet-2026-06-05
```

Or render the HTML plus canonical JSON example with:

```bash
PYTHONPATH=src:. python examples/render_hackathon_output_packet.py \
  --snapshot /tmp/workflows-real-run/snapshot.json \
  --receipt /tmp/workflows-real-run/receipt.json \
  --out docs/output/hackathon-redacted-packet-2026-06-05/index.html \
  --summary-json examples/outputs/hackathon-real-dry-run.redacted.json
```

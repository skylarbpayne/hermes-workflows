---
layout: page
title: Workflow Inspectability Cookbook
---

# Workflow Inspectability Cookbook

This cookbook is the operator path for answering the question: “why is this workflow stuck?” without replaying the workflow or changing workflow instance state.

## Default rule

Use inspection commands first. `list`, `status`, `events`, and `outbox` do not replay the workflow or advance commands, but the current CLI still opens through `WorkflowEngine` and may initialize a missing DB path. Verify the DB path before running against a new location; for forensic audits, copy the DB and inspect the copy.

Do not run `signal`, `worker`, `reconcile-*`, or `cancel` until the read packet makes the next mutation obvious and authorized.

## Fast path

1. Confirm the DB path is the existing workflow DB you intend to inspect.
2. Confirm the workflow exists: `PYTHONPATH=src:. python -m hermes_workflows list --db <db>`.
3. Filter if the DB is noisy: `PYTHONPATH=src:. python -m hermes_workflows list --db <db> --status waiting`.
4. Inspect the stuck instance: `PYTHONPATH=src:. python -m hermes_workflows status --db <db> --id <workflow_id> --recent-events 5`.
5. If `pending_commands` is empty but the workflow is failed or suspicious, include bounded history: `PYTHONPATH=src:. python -m hermes_workflows status --db <db> --id <workflow_id> --recent-events 5 --commands failed --command-limit 5`.
6. If the active wait is an approval, inspect `approvals` in the status packet before sending another approval signal.
7. If the active wait is a child workflow, inspect `child_workflows` and then run `status` on the child id.
8. If a command row looks stale, inspect the outbox directly: `PYTHONPATH=src:. python -m hermes_workflows outbox --db <db> --id <workflow_id>`.
9. If events are enough, inspect only recent events: `PYTHONPATH=src:. python -m hermes_workflows events --db <db> --id <workflow_id> --limit 20`.

## How to read the packet

- `status` is the workflow instance state: `running`, `waiting`, `completed`, `failed`, or `cancelled`.
- `waiting_on` is the durable wait key. It is the first field to compare against commands, approvals, and child workflow waits.
- `pending_commands` contains active `pending` or `running` command rows. Empty does not always mean healthy; a failed command may live only in opt-in command history.
- `diagnostics` summarizes active command labels.
- `approvals` summarizes `ApprovalRequested` events plus matching `approval.decision` signals and validation errors.
- `child_workflows` summarizes requested child workflow ids and their current status when a parent is waiting on children.
- `command_history` appears only when `--commands failed|recent|all` is passed. It is bounded and redacts full payloads into `payload_context` previews.
- `terminal_reason` appears for cancelled workflows and should explain who/what superseded the run.

## Common cases

### Waiting on human approval

Evidence shape: `status="waiting"`, `waiting_on="signal:approval.decision:<key>"`, `approvals` has the same key with `status="waiting"`.

Safe next move: surface the exact approval key, artifact/PR/status packet, and required human source. Do not fabricate an approval signal.

### Approval signal exists but the workflow still looks stuck

Evidence shape: `approvals` shows a decision, or `outbox` has `matching_signal_exists` on a notification row.

Safe next move: verify the decision source is valid. If source validation failed, ask for a corrected human-sourced signal. If the workflow is already terminal, treat the command row as historical/stale rather than resending approvals.

### Failed step with no pending commands

Evidence shape: default `status` has `status="failed"` and no active `pending_commands`; `status --commands failed` shows the failed command, bounded `last_error`, attempts, and payload preview metadata.

Safe next move: fix the underlying step/runner problem with a regression test, then start a fresh approved workflow/run if replay semantics require it. Do not edit the DB by hand.

### Parent waiting on a child workflow

Evidence shape: parent `waiting_on` starts with `child:` or `child-gather:`, and `child_workflows` names one or more child ids.

Safe next move: inspect each child with `status`. If a child is completed but the parent still waits, use `reconcile-child` or `reconcile-children` only after confirming the parent and child ids match the request event.

### Cancelled or superseded workflow

Evidence shape: `status="cancelled"`, `waiting_on=null`, and `terminal_reason` includes a reason/source/superseded_by payload.

Safe next move: do nothing unless the cancellation provenance is missing or wrong. Cancellation is the audit-preserving cleanup path for stale workflow rows.

## Mutation gates

- `signal` requires real external/human provenance for human approval gates.
- `worker` and `reconcile-*` are operational mutations; run them only when the read packet shows an active command or child wait that should progress.
- `cancel` is the right cleanup path for stale or superseded runs, but it must include a reason, source, and superseded-by target when available.
- Never use raw SQLite writes for normal operator cleanup. If the CLI cannot express the safe mutation, write a plan or issue first.

## Repo-local stewardship baseline

For the repo PR workflow DB, start with: `PYTHONPATH=src:. python -m hermes_workflows list --db .hermes/pr-workflows/repo-pr.sqlite`.

For a bounded stuck-run packet, use: `PYTHONPATH=src:. python -m hermes_workflows status --db .hermes/pr-workflows/repo-pr.sqlite --id <workflow_id> --recent-events 3 --commands failed --command-limit 3`.

Expected local verification after docs/runtime changes: `PYTHONPATH=src:. pytest -q` and `python -m compileall -q src tests examples`.

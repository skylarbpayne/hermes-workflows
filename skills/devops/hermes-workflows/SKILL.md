---
name: hermes-workflows
description: Use when an agent needs to run, inspect, approve, or harden hermes-workflows durable workflow instances with real approval provenance and no accidental side effects.
version: 1.0.0
author: Hermes Workflows
license: Apache-2.0
metadata:
  hermes:
    tags: [workflows, approvals, durable-execution, agents, sqlite]
    related_skills: []
---

# Hermes Workflows

## Overview

`hermes-workflows` is a tiny code-first durable workflow runtime. Use it when an agent needs receipts, resumability, explicit approval gates, generated child workflow inspection, and a clean stop before external side effects.

The runtime is not the agent. The workflow records steps, waits, approvals, and receipts in SQLite; the agent/model/tool loop remains outside the runtime and connects through explicit runners or step workers.

## When to Use

Use this skill when you need to:

- start or resume a local workflow instance
- inspect a workflow DB before deciding what to do next
- request or validate human approval before generated code, PR landing, sending, publishing, spending, or credential changes
- render a local approval/status dashboard
- dogfood workflows for repo changes, launch prep, demos, or operational packets

Do not use it for:

- one-off shell scripts with no resumability or approval boundary
- bypassing a Kanban/human approval gate
- executing generated workflow code before its approval request exists and is approved

## Quick Setup

From the repo root:

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=src:. pytest -q
hermes-workflows --help
```

If the console script is not on PATH, use:

```bash
PYTHONPATH=src:. python -m hermes_workflows --help
```

## Core Loop

1. Start or replay a workflow:

```bash
PYTHONPATH=src:. python -m hermes_workflows run \
  examples.repo_pr_workflow:repo_change_plan_workflow \
  --db /tmp/workflow.sqlite \
  --id wf_plan \
  --input-json '{"goal":"..."}'
```

2. Inspect before mutating:

```bash
PYTHONPATH=src:. python -m hermes_workflows status \
  --db /tmp/workflow.sqlite \
  --id wf_plan \
  --recent-events 5 \
  --commands recent \
  --command-limit 5
```

3. If waiting on approval, send a human-provenance signal only after the approval request exists:

```bash
PYTHONPATH=src:. python -m hermes_workflows signal \
  examples.repo_pr_workflow:repo_change_plan_workflow \
  --db /tmp/workflow.sqlite \
  --id wf_plan \
  --type approval.decision \
  --key approve_implementation_plan \
  --payload-json '{"action":"approve","by":"skylar"}' \
  --source-json '{"kind":"human","id":"skylar","channel":"kanban","message_url":"kanban://task/comment"}' \
  --idempotency-key kanban-comment-id
```

4. Render a dashboard when a human/operator needs the state on screen:

```bash
PYTHONPATH=src:. python -m hermes_workflows dashboard \
  --db /tmp/workflow.sqlite \
  --out /tmp/workflows-dashboard.html
```

5. If you need a local approval surface, run the dashboard server. It still sends the same validated `approval.decision` signal; it is not a separate approval model:

```bash
PYTHONPATH=src:. python -m hermes_workflows serve-dashboard \
  examples.repo_pr_workflow:repo_change_plan_workflow \
  --db /tmp/workflow.sqlite \
  --host 127.0.0.1 \
  --port 8765
```

## Approval Rules

Approval is not a vibe. A valid human approval signal needs:

- a matching prior `ApprovalRequested` event for the same key
- `payload.action` in the approval request's allowed actions
- `payload.by` matching the requested human id when the approver is `human:<id>`
- `source.kind == "human"`
- `source.id` matching the approver when specified
- a channel plus external provenance (`message_url`, `message_id`, or `event_id`)

Invalid approval signals fail closed before they are appended to history or used to complete the approval notification command. That means the workflow should remain waiting and the dashboard/status surfaces should still show the approval as active.

Separate approval keys for separate gates. Plan approval is not merge approval. Generated-workflow execution approval is not send/publish approval.

## Read-Only Inspection

Use these surfaces before raw SQLite surgery:

```bash
hermes-workflows list --db /tmp/workflow.sqlite
hermes-workflows status --db /tmp/workflow.sqlite --id wf_id --commands recent
hermes-workflows events --db /tmp/workflow.sqlite --id wf_id --limit 20
hermes-workflows outbox --db /tmp/workflow.sqlite --id wf_id --status pending
hermes-workflows dashboard --db /tmp/workflow.sqlite --out /tmp/dashboard.html
```

These commands should inspect existing DBs, not create new empty DBs or mutate rows.

## Common Pitfalls

1. Sending `approval.decision` before the workflow has requested approval. This is rejected because it approves no concrete artifact.
2. Treating an agent comment as human approval. Use `source.kind="human"` and preserve message provenance.
3. Reusing one approval for multiple side effects. Split generated-code execution, PR landing, send/publish, and merge into separate keys.
4. Reading only the compact status packet when a workflow looks stuck. Add `--commands recent` or inspect `outbox` before touching SQLite.
5. Claiming a dashboard button approved something without checking the receipt. The dashboard server is allowed to approve only by calling the same validated `approval.decision` signal path; verify `status`/events show human provenance and the expected approval key.

## Verification Checklist

- [ ] `pytest -q` passes or the failing subset is understood and reported.
- [ ] Workflow waits at the expected `waiting_on` key before approval.
- [ ] Invalid/missing/agent approval provenance is rejected without appending `SignalReceived`.
- [ ] Valid human approval completes the exact intended gate only.
- [ ] `status` or `dashboard` shows approval state and pending diagnostics without DB mutation.
- [ ] Any external side effect remains dry-run/draft-only unless a separate approval gate was satisfied.

---
layout: page
title: Hermes Workflows Launch Hardening Review — 2026-06-05
---

# Hermes Workflows Launch Hardening Review — 2026-06-05

## Scope

the maintainer's launch-hardening request was to dogfood workflows, inspect the runtime architecture, harden approval semantics, make install/use easier, add agent-facing operating guidance, build a simple local dashboard, and audit operator agent workloads for adoption candidates. Blog writing stays deferred until the repo work is in reviewable shape.

## Architecture review

### Runtime boundary

The runtime is correctly shaped as a code-first durable control plane, not an agent replacement:

- `WorkflowEngine` owns SQLite persistence, replay, outbox commands, signals, child workflow reconciliation, cancellation, and status packets.
- `WorkflowContext` exposes workflow author APIs: durable steps, gather, child workflow starts, generated-workflow approval checks, generic signal waits, and `ctx.approval.request(...)`.
- `AgentStep` / `SubprocessAgentRunner` keep model/tool execution outside the runtime and store generated child workflow source as inspectable values.
- CLI commands are thin operators around the same engine API, which is good: no hidden second approval path.

The biggest launch risk was not durability; it was approval ambiguity. Workflows can only be trusted for high-blast-radius ops if approval state is concrete, inspectable, and fail-closed.

### Approval semantics reviewed

Approval requests now use `ctx.approval.request(...)` instead of asking agents to hand-roll `ctx.wait_for("approval.granted", ...)`. The typed approval path records:

- approval key
- prompt
- artifact under approval
- approver, including `human:<id>`
- allowed actions
- authority/scope
- human decision payload
- human provenance source

Hardened behavior:

1. `approval.decision` is accepted only after a matching prior `ApprovalRequested` event exists.
2. The decision action must be one of the request's allowed actions.
3. `human:<id>` approval requires matching `payload.by` and `source.id`.
4. The source must be human-originated.
5. The source must include external provenance: `message_url`, `message_id`, or `event_id`.
6. Invalid approval decisions fail before `SignalReceived` is appended and before approval notification commands are marked complete.
7. A second conflicting decision for the same approval key is rejected; exact idempotent replay is safe.
8. Late approval and late step-completion attempts after terminal workflow status are ignored without mutating event history.
9. Read-only status/list/events/outbox/dashboard engine paths open SQLite in `mode=ro` and mutation methods now fail before attempting writes.

This directly addresses the concern that approvals could be bypassed or recorded as vague agent state.

### Install/use hardening

Added/verified launch-oriented surfaces:

- `hermes-workflows doctor` checks local Python, SQLite, DB path writability, and optional workflow importability.
- Packaged example: `hermes_workflows.examples.trip:trip_planning_workflow` runs without relying on repo-local `examples/` paths.
- README examples now use typed approval requests.
- `docs/setup-for-agents.md` includes dashboard usage and approval fail-closed rules.
- `skills/devops/hermes-workflows/SKILL.md` gives agents a reusable approval-safe operating loop.

Recommended first user path:

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=src:. pytest -q
PYTHONPATH=src:. python -m hermes_workflows doctor --workflow-ref hermes_workflows.examples.trip:trip_planning_workflow
PYTHONPATH=src:. python -m hermes_workflows run hermes_workflows.examples.trip:trip_planning_workflow --db /tmp/workflow.sqlite --id wf_trip --input-json '{"destination":"NYC","approver":"human:operator"}'
PYTHONPATH=src:. python -m hermes_workflows dashboard --db /tmp/workflow.sqlite --out /tmp/workflows-dashboard.html
PYTHONPATH=src:. python -m hermes_workflows approve hermes_workflows.examples.trip:trip_planning_workflow --db /tmp/workflow.sqlite --id wf_trip --key approve_trip_plan --by operator --channel cli --message-id manual-approval-1
```

### Dashboard / approval surface review

There are now two operator surfaces:

- `hermes-workflows dashboard --db <workflow.sqlite> --out <dashboard.html>` renders a static read-only dashboard.
- `hermes-workflows serve-dashboard <workflow_ref> --db <workflow.sqlite>` runs a local approval server for humans who need a button/form instead of hand-typing CLI signals.

The server is deliberately a thin adapter, not a new approval system. Its POST handler calls `WorkflowEngine.submit_approval_decision(ApprovalDecisionInput(...), resume=True)` with the same human provenance fields required by CLI/chat adapters. That keeps the core open enough for future Hermes plugin, Discord, Telegram, Kanban, or third-party runtime adapters: they all need to translate a human action into one canonical approval decision shape.

Current capability:

- Shows workflow status, waiting key, approval table, active diagnostics, pending commands, recent command history, and recent events.
- Provides local form-based approval capture with `human` source, channel, and message/event provenance.
- The `--once` mode supports deterministic smoke tests: start server, POST approval, verify workflow completes and the server exits.

What should become a Hermes plugin later:

- Discover waiting workflows from configured DBs.
- Render approval cards in Hermes chat/dashboard.
- Capture Discord/Telegram/Kanban message provenance automatically.
- Call the same `approval.decision` engine path.
- Post a concise receipt when the workflow resumes.

Do not put Hermes-specific messaging concerns in the core runtime. Core should expose stable approval/status APIs; Hermes plugins/adapters should own delivery, identity mapping, and channel-specific provenance.

## Workflow adoption audit

Live surfaces inspected:

- operator cron jobs: 25 total in the active profile.
- Active Kanban/task workload: canonical board `~/.hermes/kanban.db`; recent active tasks include multiple `hermes-workflows` approval/hardening tasks plus HTV, email, artifact, OAuth, and personal-admin work.
- operator skills: 98 profile skills matched workflow/approval/side-effect-related terms. Many already mention workflow patterns generically; only a smaller subset should actually become workflow-backed.

### Good first adoption candidates

1. `hermes-workflows autonomous steward worker` cron (`971dc715b86f`)
   - Best dogfood loop: repo inspection, plan artifact, approval gate, implementation, review packet.
   - Needs separate approvals for generated workflow execution, PR creation, and merge/landing.

2. `Email execution triage` cron (`d7550ac98477`)
   - Strong fit because email has obvious side-effect boundaries.
   - Workflow gates should separate classify → draft → the maintainer approval → send/archive. Current rule remains draft-only unless approved.

3. `Decision unblock batch` cron (`ee6ee8253356`)
   - Strong fit for accumulating pending approvals and turning them into typed workflow gates rather than loose chat summaries.

4. HTV send/publish/payment tasks
   - Strong fit because they combine external emails, publishing, sponsor/payment artifacts, and approval gates.
   - Especially relevant active tasks: `t_368bb223`, `t_4c747f6e`, `t_1b9a6714`, `t_d6157c53`.

5. Artifact deployment / public dashboard work
   - Use workflow gates for publish/share-link changes and credential-adjacent deploy steps.
   - Relevant cron: `9bbb3a556335`; relevant skill: `devops/artifact-deployment`.

### Candidates to leave alone for now

- Pure read-only or script-only hygiene jobs like qmd refresh, iMessage extraction, and imported download cleanup. They need receipts, not durable workflow overhead.
- One-shot personal reminders. Calendar/cron is simpler and appropriate.
- Low-risk research/summary skills unless they cross into publishing, sending, purchases, or credential mutation.

### Adoption gaps

- There is no built-in bridge yet from a Hermes Kanban task to a workflow instance ID and dashboard path.
- There is no first-class cron wrapper that starts/resumes a workflow and posts only approval packets when blocked.
- Approval source capture is CLI-friendly but still manual; Discord/Telegram/Kanban approval adapters should generate `source-json` automatically.
- Dashboard is local/static. Good for safety, but not yet a persistent operator control surface.
- No migration guide exists for converting a skill/cron prompt into a workflow-backed operating loop.

## Review verdict

The repo is now in a much better launch posture for the concern that mattered: approvals are no longer just vibes. The next slice should not be a blog post; it should be a small integration bridge that makes one real operator cron/workload create a workflow instance, stop at a typed approval gate, render a dashboard/packet, and resume only from a human-provenance decision.

## Verification

Executed from `/path/to/hermes-workflows`:

```bash
PYTHONPATH=src:. pytest -q
```

Result: `122 passed, 2 skipped in 9.20s`.

Approval surface smoke:

```bash
hermes-workflows run hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-approval-smoke.sqlite \
  --id wf_approval_smoke \
  --input-json '{"destination":"NYC","approver":"human:operator"}'

hermes-workflows serve-dashboard hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-approval-smoke.sqlite \
  --host 127.0.0.1 \
  --port 18765 \
  --once

curl -fsS -X POST http://127.0.0.1:18765/approve \
  --data-urlencode workflow_id=wf_approval_smoke \
  --data-urlencode key=approve_trip_plan \
  --data-urlencode by=operator \
  --data-urlencode channel=local-dashboard \
  --data-urlencode message_id=smoke-click-1
```

Observed receipt:

```text
{'status': 'completed', 'approved_by': 'operator', 'source': {'channel': 'local-dashboard', 'id': 'operator', 'kind': 'human', 'message_id': 'smoke-click-1'}}
```

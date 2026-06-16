# Approval adapters, Review Queue, and Hermes plugin path

`hermes-workflows` core stays runtime-agnostic. Human review is useful only if people can answer from the surfaces they already use, but the runtime should not become Discord-specific, Telegram-specific, or Hermes-specific.

The product surface is the Review Queue: one place for typed `ask(...)` requests and approve/reject approval gates.

## Stable core contract

Core owns:

- workflow status/read APIs
- Review Queue request views
- approval request events
- typed human-input responses for `ask(...)`
- canonical approval decision validation
- receipts/events/status packets
- durable state transitions that wake the resident Workflow Worker

Core should not own:

- Discord/Telegram/Gmail/Kanban identity mapping
- message delivery
- notification fanout
- Hermes profile config
- chat-specific buttons or callbacks
- credential storage
- resident process supervision

## Adapter shape

Every review surface should do the same thing:

```text
waiting workflow
  -> adapter lists Review Queue requests from configured DB aliases
  -> adapter displays the prompt, schema/actions, and redacted artifact
  -> human submits an approve/reject decision or typed response
  -> adapter captures provenance
  -> adapter records the response/decision with resume=false by default
  -> resident Workflow Worker continues from the durable state transition
```

For `ask(...)`, adapters record typed responses matching the request schema. For approval gates, adapters record approve/reject decisions. They should not invent a parallel review path or run workflow code inside a chat/gateway callback.

## Current adapters

- CLI: `hermes-workflows approve|reject` for approval gates; lower-level runtime APIs can respond to typed `ask(...)` requests.
- Static dashboard: `hermes-workflows dashboard` renders read-only status and review information.
- Local dashboard server: `hermes-workflows serve-dashboard` exposes explicit local approval forms when opted in.
- Hermes Agent plugin/dashboard: discovered via the `hermes_agent.plugins` entry point and exposes Review Queue tools plus `/workflows` dashboard UI.

See [`../integrations/hermes-plugin.md`](../integrations/hermes-plugin.md) for install/config details.

## Hermes plugin MVP

The plugin adds profile-aware Review Queue operations without changing core:

- configure one or more workflow DB aliases per Hermes profile
- configure workflow catalog/import roots for dashboard source/run/DAG routes
- list waiting Review Queue requests
- render review cards in Hermes chat/dashboard
- capture human/channel/message provenance automatically
- record typed review responses and approval decisions
- leave continuation to the resident Workflow Worker by default

Implemented public tool names:

```text
workflow_review_requests_list(db?: string, status?: string, limit?: int)
workflow_review_respond(db, workflow_id, key, payload, by, channel?, message_id?, resume?=false)
workflow_approval_decide(db, workflow_id, key, action, by, channel?, message_id?, resume?=false)
```

Legacy compatibility handlers may remain internally, but public docs and UI should say Review Queue, Human Input, review request, and approval gate. Do not make users choose between separate review/approval/operator concepts.

Exact-token gateway hook format for approval gates:

```text
hwf-approval:v1:approve:<structured-token>
hwf-approval:v1:reject:<structured-token>
```

The plugin should remain a thin adapter over `hermes_workflows`. It should not own replay, validation, workflow execution, or worker lifecycle. `resume=false` is the safe default for plugin/gateway/dashboard calls.

## Continuation after review

After an adapter records a response or approval with `resume=false`, the workflow may remain waiting/runnable until the trusted local worker sees the durable transition. The normal follow-up path is not `resume-trusted` or a gateway-side engine call. It is the resident worker for the same registry/DB:

```bash
hermes-workflows worker --config .hermes/workflows.registry.json
```

For smokes and controlled repairs, bound the worker:

```bash
hermes-workflows worker \
  --config .hermes/workflows.registry.json \
  --max-commands 10 \
  --idle-exit-after 1
```

`invoke`, `resume-trusted`, `resume-pending`, scoped workers, and direct engine calls are advanced adapter/recovery surfaces, not the default operator setup.

## Other agent/runtime adapters

The same contract should support:

- a LangGraph node that pauses on a Review Queue request
- a Temporal activity that records the decision/response
- a custom web app that posts review responses
- a Claude/Codex/OpenCode agent wrapper that waits for human approval before high-blast-radius steps

If an adapter cannot provide durable provenance, it may show status but should not record review decisions.

---
layout: page
title: Hermes Workflows documentation
---

# Hermes Workflows

Code-first durable workflows for agent work that should not disappear into chat history. Hermes Workflows gives Python projects persistent workflow state, typed agent work, typed human review, a resident **Workflow Worker**, and inspectable receipts for restarts, approvals, retries, and handoffs.

The launch-facing SDK is intentionally small:

```python
from hermes_workflows import agent, ask, bash, goal, parallel, pipeline, workflow
```

Use `agent(...)` for typed AI/worker work, `ask(...)` for typed human or external review, `bash(...)` for deterministic durable shell commands, `parallel(...)` / `pipeline(...)` for fan-out and staged item work, and `goal(...)` for bounded improve-until-accepted loops.

## Start here

<div class="doc-grid" markdown="1">

- **[Author workflows](authoring.html)**
  Learn the public SDK: `workflow`, `agent`, `ask`, `bash`, `parallel`, `pipeline`, and `goal`.

- **[Setup guide](setup-for-agents.html)**
  Install from source, create a registry, run a workflow, start the Workflow Worker, and reach the Review Queue.

- **[Hermes dashboard plugin](integrations/hermes-plugin.html)**
  Configure the Review Queue dashboard, DB aliases, workflow catalog entries, and trusted approval actions.

- **[Architecture](architecture/domain-model-and-seams.html)**
  Domain model, runtime loop, extension seams, execution environments, and failure modes.

- **[Inspectability cookbook](operations/inspectability-cookbook.html)**
  Commands for status, event history, outbox, approvals, failed commands, and recovery.

- **[Runtime boundary](architecture/runtime-vs-skills-subagents.html)**
  What belongs in durable workflow state versus prompts, skills, subagents, and operators.

- **[Launch readiness](summary.html)**
  Public-launch status, docs/accessibility notes, and verification expectations.

</div>

## Runtime model in one pass

```text
operator starts/replays workflow
  hermes-workflows run <alias-or-ref> --config .hermes/workflows.registry.json --id <id>
    -> durable workflow activation is recorded
    -> missing workflow/step/agent/child work is queued
    -> command exits after current durable state is stored

resident Workflow Worker
  hermes-workflows worker --config .hermes/workflows.registry.json
    -> leases queued commands from configured DBs
    -> executes step/agent/child work through configured runners
    -> replays the workflow against the same DB/run id
    -> stops at Review Queue requests, approvals, or terminal state

review surface
    -> dashboard/chat/CLI records typed human input or approval with provenance
    -> the worker observes the durable transition and continues
```

The CLI, worker, and dashboard must point at the same configured workflow DB. If they do not, the dashboard may look empty while work is waiting somewhere else.

## Design archive

These implementation/design records are useful for contributors, but they are not required for the launch quickstart.

- [Agent / parallel / pipeline API grill](architecture/agent-parallel-pipeline-api-grill.html)
- [Agent / parallel / pipeline API visual plan](plans/2026-06-12-agent-parallel-pipeline-api-visual-plan.html)
- [Dashboard UX research](ux/workflows-dashboard-ux-research-2026-06-06.html)

## Site build

GitHub Pages builds the repository with Jekyll from `main`. The committed layout adds navigation and client-side Mermaid rendering for architecture diagrams.

---
name: hermes-workflows
description: "Umbrella skill for Hermes Workflows. Load this to choose the right plugin-bundled workflow skill: running existing workflows vs creating workflow definitions. Generic product knowledge only."
---

# Hermes Workflows

Hermes Workflows is the durable workflow layer for agent work: state, steps, Review Queue requests, artifacts, receipts, workers, and side-effect gates.

This plugin-bundled skill is intentionally generic. It teaches agents how to use Hermes Workflows; it must not contain user-specific workflow shapes, private project context, demo plans, or one-off operational decisions.

## Pick the specific skill

- **Running / operating an existing workflow**: load `hermes-workflows-approvals:hermes-workflows-running`.
  - Start a run.
  - Run a worker.
  - Inspect status/events/artifacts.
  - Handle Review Queue approvals or typed human input.
  - Diagnose stuck/waiting runs, worker/catalog/DB mismatches, or agent-runner issues.

- **Creating / modifying a workflow definition**: load `hermes-workflows-approvals:hermes-workflows-creating`.
  - Design workflow steps and gates.
  - Use `@workflow`, `agent(...)`, `bash(...)`, `ask(...)`, `parallel(...)`, `pipeline(...)`, and `goal(...)`.
  - Define typed dataclass inputs/outputs.
  - Add reviewable artifacts, receipts, side-effect ledgers, and smoke tests.

If the task is runtime/package development rather than ordinary use or authoring, inspect the current repo docs/tests and use normal software-development workflow discipline. Do not use this umbrella skill as a dumping ground for runtime implementation scar tissue.

## Core product model

- Workflows are ordinary Python orchestration code with durable execution state.
- Agents do judgment-heavy work; deterministic tools produce receipts; humans approve or provide typed input through Review Queue surfaces.
- External side effects require explicit gates: send, publish, schedule, commit, push, PR, merge, deploy, payment, credential, and destructive data changes.
- A completed workflow claim needs evidence: terminal status, artifacts/receipts, side-effect ledger, and external handles where applicable.

## Keep out of this skill

- Personal workflow designs.
- Demo portfolio notes.
- Org-specific communication/content/event/coding preferences.
- Temporary project status.
- Long historical debugging transcripts.
- Runtime implementation details that belong in repo docs, issues, or a separate package-development skill.

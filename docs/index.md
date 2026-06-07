---
layout: page
title: hermes-workflows docs
---

# hermes-workflows docs

`hermes-workflows` is a durable Python workflow runtime for trusted agent and automation projects. It records what happened, what is waiting, who approved it, and how to resume safely after process exits or human review.

## Start here

<div class="doc-grid" markdown="1">

- **[Architecture](architecture/domain-model-and-seams.html)**
  Domain model, runtime loop, extension seams, execution environments, and failure modes.

- **[Setup guide](setup-for-agents.html)**
  Install the package, run examples, and configure a trusted local workspace.

- **[Runtime boundary](architecture/runtime-vs-skills-subagents.html)**
  What belongs in durable workflow state versus prompts, skills, subagents, and operators.

- **[Inspectability cookbook](operations/inspectability-cookbook.html)**
  Commands for status, event history, outbox, approvals, and failed commands.

- **[Approval adapters](architecture/approval-adapters-and-hermes-plugin.html)**
  How human decisions are recorded with provenance and replayed safely.

- **[Integration guide](integrations/hermes-plugin.html)**
  Example plugin/adapter configuration and approval-inbox surfaces.

</div>

## Deeper notes

- [Dynamic sub-workflows](architecture/dynamic-sub-workflows.html)
- [Dashboard runtime semantics and approval artifacts](architecture/dashboard-runtime-semantics-agentstep-approvals.html)
- [Launch hardening review](architecture/launch-hardening-review-2026-06-05.html)
- [Invocation audit](operations/invocation-audit-2026-06-06.html)
- [Dashboard UX research](ux/workflows-dashboard-ux-research-2026-06-06.html)
- [Real-run open-source blog plan](plans/2026-06-05-real-run-open-source-blog.html)
- [Resumable child workflows plan](plans/2026-05-29-resumable-child-workflows.html)

## Site build

This documentation is intentionally lightweight. GitHub Pages builds the repository with Jekyll from `main`; the committed layout adds navigation and client-side Mermaid rendering for architecture diagrams.

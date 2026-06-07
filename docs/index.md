---
layout: page
title: hermes-workflows docs
---

# hermes-workflows docs

`hermes-workflows` is a durable workflow runtime for Hermes-operated agent workspaces. It records what happened, what is waiting, who approved it, and how to resume safely after process exits or human review.

## Start here

- [Project README](https://github.com/skylarbpayne/hermes-workflows#readme)
- [Architecture, domain model, seams, execution environments, and failure modes](architecture/domain-model-and-seams.html)
- [Hermes/operator setup guide](setup-for-agents.html)
- [Runtime vs skills/subagents boundary](architecture/runtime-vs-skills-subagents.html)
- [Inspectability cookbook](operations/inspectability-cookbook.html)
- [Approval adapters and Hermes plugin](architecture/approval-adapters-and-hermes-plugin.html)
- [Hermes plugin integration](integrations/hermes-plugin.html)
- [Documentation summary and CI notes](summary.html)

## Current plan gates

- [Workflow definition ergonomics, discovery, and uv script plan](plans/2026-06-07-workflow-definition-ergonomics.html) — pre-implementation plan only; do not implement until approved.

## Existing deeper notes

- [Dynamic sub-workflows](architecture/dynamic-sub-workflows.html)
- [Launch hardening review](architecture/launch-hardening-review-2026-06-05.html)
- [Invocation audit](operations/invocation-audit-2026-06-06.html)
- [Dashboard UX research](ux/workflows-dashboard-ux-research-2026-06-06.html)
- [Real-run open-source blog plan](plans/2026-06-05-real-run-open-source-blog.html)
- [Resumable child workflows plan](plans/2026-05-29-resumable-child-workflows.html)

## Site build

The docs site is intentionally lightweight. GitHub Actions builds the `docs/` directory with GitHub Pages/Jekyll on pull requests and pushes. Once GitHub Pages is enabled in repo settings, the same workflow can be run manually to publish the built site.

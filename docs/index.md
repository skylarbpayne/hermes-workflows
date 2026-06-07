---
layout: page
title: hermes-workflows docs
---

# hermes-workflows docs

`hermes-workflows` is a durable workflow runtime for Hermes-operated agent workspaces. It records what happened, what is waiting, who approved it, and how to resume safely after process exits or human review.

## Start here

- [Project README](https://github.com/skylarbpayne/hermes-workflows#readme)
- [Architecture, domain model, seams, execution environments, and failure modes](architecture/domain-model-and-seams.md)
- [Hermes/operator setup guide](setup-for-agents.md)
- [Runtime vs skills/subagents boundary](architecture/runtime-vs-skills-subagents.md)
- [Inspectability cookbook](operations/inspectability-cookbook.md)
- [Approval adapters and Hermes plugin](architecture/approval-adapters-and-hermes-plugin.md)
- [Hermes plugin integration](integrations/hermes-plugin.md)
- [Documentation summary and CI notes](summary.md)

## Existing deeper notes

- [Dynamic sub-workflows](architecture/dynamic-sub-workflows.md)
- [Launch hardening review](architecture/launch-hardening-review-2026-06-05.md)
- [Invocation audit](operations/invocation-audit-2026-06-06.md)
- [Dashboard UX research](ux/workflows-dashboard-ux-research-2026-06-06.md)
- [Real-run open-source blog plan](plans/2026-06-05-real-run-open-source-blog.md)
- [Resumable child workflows plan](plans/2026-05-29-resumable-child-workflows.md)

## Site build

The docs site is intentionally lightweight. GitHub Actions builds the `docs/` directory with GitHub Pages/Jekyll on pull requests and pushes. Once GitHub Pages is enabled in repo settings, the same workflow can be run manually to publish the built site.

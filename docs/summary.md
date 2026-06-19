---
layout: page
title: Launch readiness
---

# Launch readiness

Hermes Workflows is ready to present as an alpha developer library for code-first durable agent workflows.

## Current launch surface

- **SDK:** `agent(...)`, `ask(...)`, `bash(...)`, `goal(...)`, `parallel(...)`, `pipeline(...)`, and `workflow` are the launch-facing authoring surface.
- **Runtime:** `hermes-workflows run ...` records/replays workflow state; `hermes-workflows worker --config ...` owns continuation.
- **Review:** Review Queue requests and approval gates are typed, durable, and provenance-stamped.
- **Dashboard:** the Hermes plugin reads configured DB aliases/catalog entries and avoids arbitrary SQLite path routing.
- **Docs:** the README and docs site teach install → registry → run → Workflow Worker → Review Queue.

## Public docs URLs

- [Project page](../)
- [Docs home](./)
- [Setup guide](setup-for-agents.html)
- [Hermes dashboard/plugin guide](integrations/hermes-plugin.html)
- [Architecture](architecture/domain-model-and-seams.html)
- [Inspectability cookbook](operations/inspectability-cookbook.html)

## Launch checks before/after a public flip

Before calling a public launch complete, verify:

1. GitHub Actions tests pass on `main`.
2. GitHub Pages/docs build succeeds on `main`.
3. A public docs crawl finds no broken internal links.
4. Package metadata includes repository, homepage, documentation, and issue URLs.
5. Repo metadata includes homepage, description, and useful topics.
6. Launch-facing docs use the facade-first quickstart and example curriculum rather than old `ctx`/`@step` demos.
7. Tracked-file scans do not expose credentials, private paths, or obsolete generated run packets.
8. The repo visibility is public and the public docs URLs are reachable without private GitHub access.

## Known alpha boundaries

Hermes Workflows is intentionally shipping as alpha infrastructure. Some deeper design records remain available under the design archive, and several roadmap issues remain open for future ergonomics such as triggers, richer artifact rendering, conversational steps, and edit/retry flows. Those are not launch blockers for the current alpha library surface.

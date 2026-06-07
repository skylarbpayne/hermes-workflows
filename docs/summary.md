---
layout: page
title: Documentation summary
---

# Documentation summary

This branch refreshes the README and adds a lightweight docs site track.

## What changed

- Replaced the long launch/history README with a concise WHY -> quickstart -> toy workflow -> docs link structure.
- Removed the Hack the Valley/hackathon walkthrough from the README. The existing blog, plan, output, and example artifacts remain in `docs/` and `examples/`.
- Documented that the current tested command surface is `hermes-workflows` / `python -m hermes_workflows`; this repository does not currently implement a `hermes workflows` wrapper.
- Added `docs/architecture/domain-model-and-seams.md` with runtime model, domain objects, extension seams, execution environments, failure modes, and Mermaid diagrams.
- Added `docs/index.md` and `docs/_config.yml` so the `docs/` directory can be built as a lightweight GitHub Pages/Jekyll site.
- Added `.github/workflows/docs.yml` for PR-safe docs build validation and push-to-main deployment.

## CI status notes

- Existing `.github/workflows/test.yml` already runs on `pull_request` to `main`, so PRs are covered by the Python test workflow.
- The new docs workflow also runs on `pull_request` to `main`, but only deploys Pages on `push` to `main`.
- Local validation for this branch is recorded in the commit/task summary rather than generated into this file.

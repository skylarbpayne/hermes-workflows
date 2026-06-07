---
layout: page
title: Repo change review — ctx.gather and dogfood repo_change_review
---

# Repo change review: Add ctx.gather for parallel durable step execution and dogfood repo_change_review

Plan approved by: skylar-current-chat
Landing approved by: skylar-current-chat
Recommendation: approve

## Repo

- Path: `/Users/skylarpayne/code/hermes-workflows`
- Branch: `main`
- Baseline HEAD: `79ce9a1`

## Plan

Goal: Add ctx.gather for parallel durable step execution and dogfood repo_change_review

### Approval gates

- `approve_change_plan`
- `approve_change_landing`

### Verification commands

- `pytest -q`
- `python -m compileall -q src tests examples`

### Implementation boundary

External/manual implementation happens after plan approval and before implementation.ready signal.

### Risk notes

- Workflow does not bypass human landing approval.
- Commit/push are optional and require approve_change_landing.
- Verification output is captured before landing.

### Intended implementation slice

- Add durable fan-out/fan-in through `ctx.gather(step_a(...), step_b(...))`.
- Ensure all missing child steps enqueue before the workflow exits waiting.
- Resume with completed child outputs in argument order.
- Prove completed child steps are not rerun after restart.
- Dogfood the new `repo_change_review_workflow` against this repo change.

## Verification

- Tests: pass

### `pytest -q`

```text
.........                                                                [100%]
9 passed in 0.85s
```
### `python -m compileall -q src tests examples`

```text

```

## Changed files

```text
M README.md
 M src/hermes_workflows/decorators.py
 M src/hermes_workflows/engine.py
?? examples/repo_change_review_workflow.py
?? tests/test_gather.py
?? tests/test_repo_change_review_workflow.py
```

## Diff stat

```text
README.md                          | 17 +++++++++++++
 src/hermes_workflows/decorators.py | 27 +++++++++++++++++---
 src/hermes_workflows/engine.py     | 52 ++++++++++++++++++++++++++++++++++++--
 3 files changed, 90 insertions(+), 6 deletions(-)
```

## Blockers

- none

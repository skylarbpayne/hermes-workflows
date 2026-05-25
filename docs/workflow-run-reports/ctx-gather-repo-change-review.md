# Repo change review: Add ctx.gather for parallel durable step execution and dogfood repo_change_review

Plan approved by: skylar-current-chat
Landing approved by: skylar-current-chat
Recommendation: approve

## Repo

- Path: `/Users/skylarpayne/code/hermes-workflows`
- Branch: `main`
- Baseline HEAD: `79ce9a1`

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

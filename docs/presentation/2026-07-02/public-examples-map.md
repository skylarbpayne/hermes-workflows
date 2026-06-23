# Public examples map

The presentation should not make people infer the product from internals. Use these examples as a ladder from simple to powerful.

| Example | Public primitive proved | Demo value | Use live? |
| --- | --- | --- | --- |
| `src/hermes_workflows/examples/reviewable_draft.py` | `agent(...)` + typed `ask(...)` | Smallest Review Queue demo. Deterministic mock output, no credentials, no side effects. | Yes |
| `examples/bash_repo_health.py` | `bash(...)` + typed summary + Review Queue | Shows deterministic command receipts before human review. | Optional |
| `examples/parallel_research.py` | `parallel([... agent(...) ...])` | Shows fan-out before waiting on one aggregate review. | Optional |
| `examples/pipeline_section_review.py` | `pipeline(...)` with per-item reviews | Shows staged item processing and multiple Review Queue cards. | Optional |
| `examples/goal_revision_loop.py` | `goal(...)` | Shows bounded improve-until-accepted loops without provider credentials. | Optional |
| `examples/dynamic_workflow_return.py` | `agent(..., returns=Workflow)` + child workflows | Shows dynamic workflow composition and child run receipts. | Yes, if time |
| `examples/local_model_adapter_workflow.py` | `agent(..., model=...)` runner config shape | Shows model-selection plumbing without requiring a live provider. | Mention only |

## Presentation recommendation

Live-demo only two examples:

1. `reviewable-draft` because it is small and proves the core human-review loop.
2. `dynamic-workflow-return` because it proves the system is not just static scripts.

Use the rest as a slide/table. More live demos will dilute the story and invite demo gremlins.

## Story ladder

1. **Reviewable draft:** the workflow stops at a typed human decision.
2. **Bash health check:** deterministic checks become receipts, not vibes.
3. **Parallel research:** several independent agents can fan out without losing identity.
4. **Pipeline review:** every item carries its own review state.
5. **Goal loop:** improvement can be bounded and inspectable.
6. **Dynamic workflow return:** generated workflows can become durable child runs.
7. **Local model adapter:** runner/model configuration stays outside workflow logic.

## Readiness notes

- All examples avoid external side effects by default.
- Launch example tests already cover expected waiting/completed states in `tests/test_launch_examples.py`.
- The presentation runbook uses `--project-root .` explicitly so the `run` command resolves the source checkout even when registry paths are moved.

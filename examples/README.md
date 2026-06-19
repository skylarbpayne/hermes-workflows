# Examples

These examples are ordered as a launch curriculum. Start with the small facade-first files; keep the older direct-engine demos for advanced/runtime contributors.

## Launch-facing examples

| Example | Shows | Run shape |
| --- | --- | --- |
| `src/hermes_workflows/examples/reviewable_draft.py` | installed quickstart, `agent(...)` with deterministic `mock_output`, typed `ask(...)`, Review Queue request | `hermes-workflows run reviewable-draft --config .hermes/workflows.registry.json --id wf_reviewable_draft` |
| `examples/bash_repo_health.py` | durable `bash(...)`, typed agent interpretation, typed human review | source checkout / registry alias |
| `examples/parallel_research.py` | `parallel([... agent(...) ...])` fan-out/fan-in, aggregate review | source checkout / registry alias |
| `examples/pipeline_section_review.py` | `pipeline(items, draft, check, review)` with per-item Review Queue cards | source checkout / registry alias |
| `examples/goal_revision_loop.py` | bounded `goal(do_fn, check_fn)` loop | source checkout / registry alias |
| `examples/local_model_adapter_workflow.py` | `agent(..., model=...)` with fake-output fallback and local/Hermes runner configuration shape | source checkout / registry alias |

All launch-facing examples avoid direct `WorkflowEngine`, low-level `ctx.approval.request(...)`, and manual signal plumbing in the workflow body.

## Registry snippet

From a source checkout, a compact registry for the examples can look like this:

```json
{
  "dbs": {"default": "workflows.sqlite"},
  "workflows": {
    "reviewable-draft": {
      "workflow_ref": "hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow",
      "db": "default"
    },
    "bash-repo-health": {
      "workflow_ref": "examples/bash_repo_health.py:bash_repo_health_workflow",
      "db": "default",
      "project_root": "."
    },
    "parallel-research": {
      "workflow_ref": "examples/parallel_research.py:parallel_research_workflow",
      "db": "default",
      "project_root": "."
    },
    "pipeline-section-review": {
      "workflow_ref": "examples/pipeline_section_review.py:pipeline_section_review_workflow",
      "db": "default",
      "project_root": "."
    },
    "goal-revision-loop": {
      "workflow_ref": "examples/goal_revision_loop.py:goal_revision_loop_workflow",
      "db": "default",
      "project_root": "."
    },
    "local-model-adapter": {
      "workflow_ref": "examples/local_model_adapter_workflow.py:local_model_adapter_workflow",
      "db": "default",
      "project_root": "."
    }
  }
}
```

Then run:

```bash
hermes-workflows run parallel-research --config .hermes/workflows.registry.json --id wf_parallel_research
hermes-workflows worker --config .hermes/workflows.registry.json --worker-id examples-worker --max-commands 10 --idle-exit-after 0.1
hermes-workflows status --db .hermes/workflows.sqlite --id wf_parallel_research
```

Workflows containing `ask(...)` intentionally stop with Review Queue requests. Respond through the Hermes dashboard/plugin Review Queue or another configured review adapter, then run the worker again.

## Advanced / runtime examples

Older examples such as `dynamic_workflow_return.py`, `restart_signal_demo.py`, `repo_change_review_workflow.py`, and `workflows_demo_2026_06_05.py` are useful for contributors inspecting runtime seams, generated child workflows, direct engine usage, or historical demos. They are not the day-one authoring model.

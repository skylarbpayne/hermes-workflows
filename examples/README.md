# Examples

These examples are ordered as a launch curriculum. Start with four tiny source-tree examples that teach one framework idea each, then move into composition examples. Larger presentation/demo lanes live under `examples/advanced/` so they do not masquerade as the starter surface.

## Launch-facing examples

| Example | Shows | Run shape |
| --- | --- | --- |
| `examples/typed_review.py` | typed workflow input, `MarkdownArtifact`, typed `ask(...)` review | source checkout / registry alias |
| `examples/agent_workspace.py` | `agent(..., workspace_dir=..., isolation="worktree")` with deterministic mock output | source checkout / registry alias |
| `examples/prompt_file.py` | durable prompt template file rendered into `agent(...)` | source checkout / registry alias |
| `examples/artifact_review.py` | `JsonArtifact` review packet with typed Review Queue response | source checkout / registry alias |
| `src/hermes_workflows/examples/reviewable_draft.py` | installed quickstart, `agent(...)` with deterministic `mock_output`, typed `ask(...)`, Review Queue request | `hermes-workflows run reviewable-draft --config .hermes/workflows.registry.json --id wf_reviewable_draft` |
| `examples/bash_repo_health.py` | durable `bash(...)`, typed agent interpretation, typed human review | source checkout / registry alias |
| `examples/parallel_research.py` | `parallel([... agent(...) ...])` fan-out/fan-in, aggregate review | source checkout / registry alias |
| `examples/pipeline_section_review.py` | `pipeline(items, draft, check, review)` with per-item Review Queue cards | source checkout / registry alias |
| `examples/goal_revision_loop.py` | bounded `goal(do_fn, check_fn)` loop | source checkout / registry alias |
| `examples/dynamic_workflow_return.py` | `agent(..., returns=Workflow)` returning generated workflow code, then `ctx.map_workflow(...)` child runs | source checkout / registry alias |
| `examples/local_model_adapter_workflow.py` | `agent(..., model=...)` with fake-output fallback and local/Hermes runner configuration shape | source checkout / registry alias |

## Advanced demos

These are useful for presentations and product design, but too large for first-contact examples:

- `examples/advanced/content_asset_lane.py`
- `examples/advanced/coding_review_demo.py`
- `examples/advanced/event_planning_demo.py`

All launch-facing examples avoid direct `WorkflowEngine`, low-level `ctx.approval.request(...)`, and manual signal plumbing in the workflow body.

## Registry snippet

From a source checkout, a compact registry for the examples can use one default DB and omit per-workflow `db` fields:

```json
{
  "dbs": {"default": "workflows.sqlite"},
  "workflows": {
    "reviewable-draft": {
      "workflow_ref": "hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow"
    },
    "typed-review": {
      "workflow_ref": "examples/typed_review.py:typed_review_workflow",
      "project_root": "."
    },
    "agent-workspace": {
      "workflow_ref": "examples/agent_workspace.py:agent_workspace_workflow",
      "project_root": "."
    },
    "prompt-file": {
      "workflow_ref": "examples/prompt_file.py:prompt_file_workflow",
      "project_root": "."
    },
    "artifact-review": {
      "workflow_ref": "examples/artifact_review.py:artifact_review_workflow",
      "project_root": "."
    },
    "bash-repo-health": {
      "workflow_ref": "examples/bash_repo_health.py:bash_repo_health_workflow",
      "project_root": "."
    },
    "parallel-research": {
      "workflow_ref": "examples/parallel_research.py:parallel_research_workflow",
      "project_root": "."
    },
    "pipeline-section-review": {
      "workflow_ref": "examples/pipeline_section_review.py:pipeline_section_review_workflow",
      "project_root": "."
    },
    "goal-revision-loop": {
      "workflow_ref": "examples/goal_revision_loop.py:goal_revision_loop_workflow",
      "project_root": "."
    },
    "dynamic-workflow-return": {
      "workflow_ref": "examples/dynamic_workflow_return.py:dynamic_workflow_return_workflow",
      "project_root": "."
    },
    "local-model-adapter": {
      "workflow_ref": "examples/local_model_adapter_workflow.py:local_model_adapter_workflow",
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

Workflows containing `ask(...)` intentionally stop with Review Queue requests. Respond through the Hermes dashboard/plugin Review Queue or a review adapter built from the contract in [Author workflows](../docs/authoring.md#building-a-review-queue-adapter), then run the worker again if you used the bounded smoke command above.

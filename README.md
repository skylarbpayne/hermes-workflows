# hermes-workflows

`hermes-workflows` makes long-running agent work reviewable instead of ephemeral. It gives trusted Python workflow projects durable state, typed agent work, typed human review, a resident Workflow Worker, and receipts that survive process exits, review pauses, and restarts.

The public authoring surface is intentionally small:

```python
from hermes_workflows import agent, ask, parallel, pipeline, workflow
```

Start there. Runtime internals such as `WorkflowEngine`, approval DTOs, worker internals, `step`, and low-level `approve` helpers still exist in submodules for adapters and advanced integrations, but they are not the launch-facing SDK.

## Quickstart

Install from a source checkout or package:

```bash
python -m pip install .
hermes-workflows --help
```

Create a registry in the workspace that will own workflow state:

```bash
mkdir -p .hermes
cat > .hermes/workflows.registry.json <<'JSON'
{
  "dbs": {
    "default": "workflows.sqlite"
  },
  "workflows": {
    "trip": {
      "workflow_ref": "hermes_workflows.examples.trip:trip_planning_workflow",
      "db": "default"
    }
  }
}
JSON
```

Run the installed demo and then let the Workflow Worker drain runnable commands until the workflow reaches the human review gate:

```bash
hermes-workflows run trip \
  --config .hermes/workflows.registry.json \
  --id wf_trip_quickstart \
  --input-json '{"destination":"NYC","approver":"human:operator"}'

hermes-workflows worker \
  --config .hermes/workflows.registry.json \
  --worker-id quickstart-worker \
  --max-commands 5 \
  --idle-exit-after 0.1

hermes-workflows status \
  --db .hermes/workflows.sqlite \
  --id wf_trip_quickstart
```

`run` records or replays the workflow instance. It does not pretend the current process is a forever worker. The resident `hermes-workflows worker --config ...` command owns continuation: it leases queued workflow/step/agent/child-workflow commands, records outputs, and re-enters the workflow until it is waiting for review or terminal.

Approve the review gate with human provenance, then let the same worker continue:

```bash
hermes-workflows approve hermes_workflows.examples.trip:trip_planning_workflow \
  --db .hermes/workflows.sqlite \
  --id wf_trip_quickstart \
  --key approve_trip_plan \
  --by operator \
  --channel cli \
  --message-id quickstart-approval-1

hermes-workflows worker \
  --config .hermes/workflows.registry.json \
  --worker-id quickstart-worker \
  --max-commands 5 \
  --idle-exit-after 0.1
```

A real always-on setup runs the worker under launchd, systemd, s6, tmux, or another supervisor without `--idle-exit-after`.

## Minimal authoring example

Workflow code is ordinary Python. `agent(...)` asks a configured worker/runner for typed work. `ask(...)` creates a typed Review Queue request for a human or external reviewer. `parallel(...)` and `pipeline(...)` compose those calls without exposing runtime bookkeeping in the workflow body.

```python
from dataclasses import dataclass
from typing import Literal

from hermes_workflows import agent, ask, workflow


@dataclass
class Draft:
    text: str


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@workflow
async def release_note_workflow(inputs):
    draft = await agent(
        "writer",
        prompt="Draft a release note for the supplied change.",
        input={"change": inputs["change"]},
        returns=Draft,
    )
    decision = await ask(
        prompt="Review this release note.",
        key="review_release_note",
        input=draft,
        returns=ReviewDecision,
    )
    return {"draft": draft.text, "decision": decision.action, "side_effects": {"published": False}}


if __name__ == "__main__":
    raise SystemExit(release_note_workflow.run())
```

The Review Queue schema comes from the `returns=` type. A dataclass with `action: Literal[...]` produces explicit action buttons instead of a raw JSON box.

## Runtime model in one screen

```text
operator starts/replays workflow
  hermes-workflows run <alias-or-ref> --config .hermes/workflows.registry.json --id <id>
    -> durable workflow activation is recorded
    -> missing workflow/step/agent/child work is queued
    -> command exits after current durable state is recorded

resident Workflow Worker
  hermes-workflows worker --config .hermes/workflows.registry.json
    -> leases queued commands from configured DBs
    -> executes step/agent/child work through configured runners
    -> replays the workflow against the same DB/run id
    -> stops at Review Queue requests, approvals, or terminal state

review surface
    -> dashboard/chat/CLI records human input or approval with provenance
    -> the worker observes the durable transition and continues
```

Do not split the CLI, worker, and dashboard across different SQLite files. If the worker drains one DB and the dashboard reads another, approvals will look missing even though the runtime is doing exactly what you configured.

## Documentation

- [Live docs site](https://skylarbpayne.com/hermes-workflows/docs/)
- [Docs site index](docs/index.md)
- [Hermes/operator setup guide](docs/setup-for-agents.md)
- [Hermes dashboard/plugin setup](docs/integrations/hermes-plugin.md)
- [Architecture, domain model, seams, execution environments, and failure modes](docs/architecture/domain-model-and-seams.md)
- [Runtime vs skills/subagents boundary](docs/architecture/runtime-vs-skills-subagents.md)
- [Approval adapters and Review Queue](docs/architecture/approval-adapters-and-hermes-plugin.md)
- [Inspectability cookbook](docs/operations/inspectability-cookbook.md)

## Examples directories

- `src/hermes_workflows/examples/` contains small installed examples that work after package installation, such as `hermes_workflows.examples.trip:trip_planning_workflow`.
- `examples/` contains contributor demos, deterministic runners, scripts, prompts, and larger scenario assets for source-tree development.

## Development checks

```bash
python -m pip install -e '.[dev]'
pytest -q
python -m compileall -q src tests examples
git diff --check
```

Pull requests are covered by `.github/workflows/test.yml`, which runs on `pull_request` to `main` for Python 3.9 and 3.11. The docs site workflow also validates the GitHub Pages/Jekyll build on pull requests without deploying.

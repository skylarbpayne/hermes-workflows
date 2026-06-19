# hermes-workflows

`hermes-workflows` makes long-running agent work reviewable instead of ephemeral. It gives trusted Python workflow projects durable state, typed agent work, typed human review, a resident Workflow Worker, and receipts that survive process exits, review pauses, and restarts.

> **Affiliation disclaimer:** Hermes Workflows is an independent project by Skylar Payne. It is not affiliated with, endorsed by, sponsored by, or officially connected to Nous Research or the Nous Research Hermes Agent project.

The public authoring surface is intentionally small:

```python
from hermes_workflows import agent, ask, bash, goal, parallel, pipeline, workflow
```

Start there. Runtime internals such as `WorkflowEngine`, approval DTOs, worker internals, `step`, and low-level `approve` helpers still exist in submodules for adapters and advanced integrations, but they are not the launch-facing SDK.

## Quickstart

Install from a trusted source checkout. Until a PyPI release is published, do not use `pip install hermes-workflows` / `uvx` / `pipx` instructions; clone the repository and install the checkout:

```bash
git clone https://github.com/skylarbpayne/hermes-workflows.git
cd hermes-workflows
python -m venv .venv
. .venv/bin/activate
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
    "reviewable-draft": {
      "workflow_ref": "hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow",
      "db": "default"
    }
  }
}
JSON
```

Run the installed facade-first demo and then let the Workflow Worker drain runnable commands until the workflow reaches the typed Review Queue request:

```bash
hermes-workflows run reviewable-draft \
  --config .hermes/workflows.registry.json \
  --id wf_reviewable_draft_quickstart \
  --input-json '{"topic":"Hermes Workflows launch","approver":"human:operator"}'

hermes-workflows worker \
  --config .hermes/workflows.registry.json \
  --worker-id quickstart-worker \
  --max-commands 5 \
  --idle-exit-after 0.1

hermes-workflows status \
  --db .hermes/workflows.sqlite \
  --id wf_reviewable_draft_quickstart
```

`run` records or replays the workflow instance. It does not pretend the current process is a forever worker. The resident `hermes-workflows worker --config ...` command owns continuation: it leases queued workflow/step/agent/bash/child-workflow commands, records outputs, and re-enters the workflow until it is waiting for Review Queue input or terminal.

Respond to the Review Queue request through the Hermes dashboard/plugin or another configured review adapter, then start the worker again if you used the bounded smoke command above. In a real supervised setup, the resident worker keeps running and continues automatically after the response is recorded. The response payload must match the `returns=` dataclass schema and include provenance from the adapter that recorded it.

A real always-on setup runs the worker under launchd, systemd, s6, tmux, or another supervisor without `--idle-exit-after`.

## Minimal authoring example

Workflow code is ordinary Python. `agent(...)` asks a configured worker/runner for typed work. `bash(...)` runs deterministic shell commands as durable worker steps with captured stdout/stderr, exit status, timing, timeouts, and optional redaction. `ask(...)` creates a typed Review Queue request for a human or external reviewer. `parallel(...)`, `pipeline(...)`, and `goal(...)` compose those calls without exposing runtime bookkeeping in the workflow body.

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
- [Author workflows with the public SDK](docs/authoring.md)
- [Hermes/operator setup guide](docs/setup-for-agents.md)
- [Hermes dashboard/plugin setup](docs/integrations/hermes-plugin.md)
- [Architecture, domain model, seams, execution environments, and failure modes](docs/architecture/domain-model-and-seams.md)
- [Runtime vs skills/subagents boundary](docs/architecture/runtime-vs-skills-subagents.md)
- [Approval adapters and Review Queue](docs/architecture/approval-adapters-and-hermes-plugin.md)
- [Inspectability cookbook](docs/operations/inspectability-cookbook.md)

## Examples directories

- `src/hermes_workflows/examples/` contains small installed examples that work after package installation, starting with `hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow`.
- `examples/` contains the launch example curriculum, contributor demos, deterministic runners, scripts, prompts, and larger scenario assets for source-tree development.

## Development checks

```bash
python -m pip install -e '.[dev]'
pytest -q
python -m compileall -q src tests examples
git diff --check
```

Pull requests are covered by `.github/workflows/test.yml`, which runs on `pull_request` to `main` for Python 3.9 and 3.11. The docs site workflow also validates the GitHub Pages/Jekyll build on pull requests without deploying.

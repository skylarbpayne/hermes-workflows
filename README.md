# hermes-workflows

`hermes-workflows` makes long-running agent work reviewable instead of ephemeral. It gives trusted Python workflow projects durable state, typed agent work, typed human review, a foreground Workflow Runner v2, and receipts that survive process exits, review pauses, and restarts.

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
    "typed-quickstart": {
      "workflow_ref": "hermes_workflows.examples.install_smoke:release_note_workflow",
      "db": "default"
    }
  }
}
JSON
```

Run the installed, fully typed quickstart and then run the Workflow Runner v2 in the foreground until it drains runnable commands and the workflow reaches the typed Review Queue request:

```bash
hermes-workflows run typed-quickstart \
  --config .hermes/workflows.registry.json \
  --id wf_typed_quickstart \
  --input-json '{"change":"Expose typed workflow contracts."}'

hermes-workflows runner run \
  --config .hermes/workflows.registry.json \
  --worker-id quickstart-worker \
  --max-commands 5 \
  --idle-exit-after 0.1

hermes-workflows status \
  --db .hermes/workflows.sqlite \
  --id wf_typed_quickstart
```

`hermes-workflows run` stays in the Python interpreter and environment that installed the console script. It does not discover or invoke an unrelated `uv` on `PATH`. Activate and install into the environment you want before running the command; `--project-root` controls project/registry discovery, not interpreter selection.

`run` records or replays the workflow instance. It does not pretend the current process is a forever worker. The canonical foreground continuation command is `hermes-workflows runner run --config ...`: it leases queued workflow/step/agent/bash/child-workflow commands, records outputs, and re-enters the workflow until it is waiting for Review Queue input or terminal. The older `hermes-workflows worker --config ...` spelling remains a compatibility alias, but new docs and operators should prefer `runner run` / `runner once`.

Respond to the Review Queue request through the Hermes dashboard/plugin or another configured review adapter, then start the runner again if you used the bounded smoke command above. A recorded operator response always creates a visible durable continuation command; the runner, not a hidden chat callback, consumes that command. In a real supervised setup, keep `runner run` alive under launchd, systemd, s6, tmux, or another supervisor only after the foreground command works in your workspace. The response payload must match the `returns=` dataclass schema and include provenance from the adapter that recorded it.

With workflow id `wf_typed_quickstart`, the exact serialized input and state transitions are:

```json
{"change":"Expose typed workflow contracts."}
```

`run` records the initial state:

```json
{"error":null,"result":null,"status":"running","waiting_on":null,"workflow_id":"wf_typed_quickstart"}
```

After the runner executes the credential-free mock agent step, the typed `ask(...)` request is waiting:

```json
{"error":null,"result":null,"status":"waiting","waiting_on":"signal:operator.response:review_release_note","workflow_id":"wf_typed_quickstart"}
```

After an `approve` response with feedback `Ready to ship.` is recorded and the runner continues, the exact result is:

```json
{"error":null,"result":{"decision":{"action":"approve","feedback":"Ready to ship."},"draft":{"text":"Release note: Expose typed workflow contracts."},"side_effects":{"published":false}},"status":"completed","waiting_on":null,"workflow_id":"wf_typed_quickstart"}
```

A real always-on setup runs the runner under launchd, systemd, s6, tmux, or another supervisor without `--idle-exit-after`.

## Minimal authoring example

Workflow code is ordinary Python. `agent(...)` asks a configured runner for typed work. `bash(...)` runs deterministic shell commands as durable runner steps with captured stdout/stderr, exit status, timing, timeouts, and optional redaction. `ask(...)` creates a typed Review Queue request for a human or external reviewer. `parallel(...)`, `pipeline(...)`, and `goal(...)` compose those calls without exposing runtime bookkeeping in the workflow body.

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from hermes_workflows import agent, ask, workflow


@dataclass(frozen=True)
class ReleaseNoteInput:
    change: str


@dataclass(frozen=True)
class Draft:
    text: str


@dataclass(frozen=True)
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: Optional[str] = None


@dataclass(frozen=True)
class SideEffects:
    published: bool = False


@dataclass(frozen=True)
class ReleaseNoteResult:
    draft: Draft
    decision: ReviewDecision
    side_effects: SideEffects


@workflow
async def release_note_workflow(inputs: ReleaseNoteInput) -> ReleaseNoteResult:
    draft = await agent(
        "writer",
        prompt="Draft a release note for the supplied change.",
        input=inputs,
        returns=Draft,
        # The canonical quickstart must reach typed review without credentials.
        mock_output={"text": f"Release note: {inputs.change}"},
    )
    decision = await ask(
        "Review this release note.",
        key="review_release_note",
        input=draft,
        returns=ReviewDecision,
    )
    return ReleaseNoteResult(
        draft=draft,
        decision=decision,
        side_effects=SideEffects(),
    )


if __name__ == "__main__":
    raise SystemExit(release_note_workflow.run())  # type: ignore[attr-defined]
```

The first copyable workflow is typed end to end: serialized input is coerced to `ReleaseNoteInput`, `agent(...)` returns `Draft`, `ask(...)` returns `ReviewDecision`, and Python receives `ReleaseNoteResult` while durable status remains JSON. The Review Queue schema comes from the `returns=` type, so `action: Literal[...]` produces explicit action buttons instead of a raw JSON box. Loose dictionaries remain an advanced compatibility input, not the advertised authoring standard.

## Runtime model in one screen

```text
operator starts/replays workflow
  hermes-workflows run <alias-or-ref> --config .hermes/workflows.registry.json --id <id>
    -> durable workflow activation is recorded
    -> missing workflow/step/agent/child work is queued
    -> command exits after current durable state is recorded

foreground Workflow Runner v2
  hermes-workflows runner run --config .hermes/workflows.registry.json
    -> leases queued commands from configured DBs
    -> executes step/agent/child work through configured runners
    -> replays the workflow against the same DB/run id
    -> stops at Review Queue requests, approvals, or terminal state

optional daemon/supervisor, after foreground proof
  launchd/systemd/s6/tmux runs the same runner command without bounded smoke flags

review surface
    -> dashboard/chat/CLI records human input or approval with provenance
    -> durable operator response creates a visible continuation command
    -> the runner observes the command and continues
```

Do not split the CLI, runner/worker, and dashboard across different SQLite files. If the runner drains one DB and the dashboard reads another, approvals will look missing even though the runtime is doing exactly what you configured.

## Runner v2 operational states

Status surfaces expose these operator-facing states:

| State | Meaning | Typical next action |
| --- | --- | --- |
| Waiting on Skylar | The workflow is waiting on a typed Review Queue/human response. | Capture a real human response through the dashboard/plugin/adapter. |
| Queued | Runnable work exists and no runner has claimed it yet. | Start `hermes-workflows runner run --config ...` or run `runner once` for recovery. |
| Running | A runner has claimed a command and its heartbeat/lease is current. | Wait, or inspect the runner process if it exceeds expected time. |
| Stuck | A command repeatedly failed, a lease expired, no healthy worker is present for old queued work, or the claiming heartbeat is stale. | Use `runner status`, `runner doctor`, and command history to repair the runner or retry intentionally. |
| Failed | The workflow reached a terminal failure. | Inspect events/commands and launch a new corrected run if needed. |
| Completed | The workflow reached a terminal success. | Review receipts/artifacts. |
| Cancelled | The workflow was intentionally cancelled. | No continuation; launch a new run if work is still needed. |

Recovery commands are read-only unless they explicitly run work:

```bash
# Read heartbeat, queued/runnable commands, and recent workflow rows.
hermes-workflows runner status --db .hermes/workflows.sqlite

# Validate registry, DB alias resolution, workflow imports, and duplicate live workers.
hermes-workflows runner doctor --config .hermes/workflows.registry.json --db default

# Execute one visible queued command for controlled recovery.
hermes-workflows runner once --config .hermes/workflows.registry.json --db default

# Foreground runner loop; omit bounded flags only after the foreground loop is proven.
hermes-workflows runner run --config .hermes/workflows.registry.json --db default --max-commands 10 --idle-exit-after 1
```

Dogfood and demos must not fake human provenance. Human-gated completion requires a real human response with adapter provenance (`by`, `channel`, message/event id or equivalent). Test fixtures, local smokes, and manual signals are allowed only when clearly labeled as test/manual provenance; do not present them as a human approval.

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

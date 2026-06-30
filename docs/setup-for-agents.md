---
layout: page
title: Set up hermes-workflows for an agent
---

# Set up `hermes-workflows` for an agent

`hermes-workflows` is a durable runtime you put underneath an agent when work needs state, review gates, external runner execution, and receipts. The normal production-ish shape is one workspace registry, one shared workflow DB per source, and one foreground Workflow Runner v2 command that you prove locally before supervising it as a daemon.

## 1. Install in the operator workspace

Use a trusted Hermes workspace or another local operator workspace to own the checkout, venv, registry, workflow DB, dashboard config, and runner supervisor.

```bash
git clone https://github.com/skylarbpayne/hermes-workflows.git
cd hermes-workflows
python -m venv .venv
. .venv/bin/activate
python -m pip install .

hermes-workflows --help
hermes-workflows doctor \
  --db .hermes/workflows.sqlite \
  --workflow-ref hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow
```

Until a package-index release is published, install from a trusted source checkout. Do not use `pip install hermes-workflows`, `uvx`, or `pipx` launch instructions yet.

For contributor work, install dev extras and run tests:

```bash
python -m pip install -e '.[dev]'
pytest -q
```

This package exposes the `hermes-workflows` CLI and `python -m hermes_workflows`. It does not currently install a `hermes workflows` subcommand.

## 2. Create a registry

Put workflow aliases and DB aliases in `.hermes/workflows.registry.json`:

```json
{
  "dbs": {
    "default": "workflows.sqlite"
  },
  "workflows": {
    "reviewable-draft": {
      "workflow_ref": "hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow",
      "db": "default",
      "default_input": {}
    }
  }
}
```

Relative DB paths are resolved from the registry file, so `workflows.sqlite` means `.hermes/workflows.sqlite` when the registry lives under `.hermes/`. Use absolute paths if the DB is shared by multiple workspaces or services.

Validate aliases before wiring a foreground runner:

```bash
hermes-workflows registry doctor --config .hermes/workflows.registry.json
```

## 3. Start workflow runs

Start or replay a workflow instance through the registry:

```bash
hermes-workflows run reviewable-draft \
  --config .hermes/workflows.registry.json \
  --id wf_reviewable_draft_demo \
  --input-json '{"topic":"Hermes Workflows launch"}'
```

`run` records the workflow activation and queues missing work. It is not the always-on continuation loop. A run can return `running` before the Review Queue request exists because a runner still needs to execute queued steps and replay the workflow to the next wait.

## 4. Run the foreground Workflow Runner v2

For recurring agent-owned workflows, first run the canonical runner in the foreground from the same registry. Defer daemon/supervisor setup until this command can drain work in the operator workspace:

```bash
hermes-workflows runner run \
  --config .hermes/workflows.registry.json \
  --worker-id workflows-local-runner \
  --agent-command python \
  --agent-request-stdin json \
  --agent-arg -m \
  --agent-arg hermes_workflows.agent_cli_adapter \
  --agent-arg --agent-command \
  --agent-arg hermes \
  --agent-arg --agent-model-arg \
  --agent-arg --model \
  --agent-arg --agent-model-arg \
  --agent-arg '{model}' \
  --agent-arg --agent-prompt-arg \
  --agent-arg --oneshot
```

The runner leases runnable or lease-expired `run_workflow`, `run_step`, `external_agent`, and child-workflow commands from configured DBs. It loads each instance's stored `workflow_ref` through the registry, executes the command, records durable output, and replays the workflow until it reaches a Review Queue request, another durable wait, or a terminal state.

`agent(...)` already runs through the existing agent-step machinery: the workflow emits an `external_agent` command, the runner calls `WorkflowEngine.agent_runner`, and the canonical `hermes_workflows.agent_runner.SubprocessAgentRunner` runs the configured adapter command. The compatibility module `hermes_workflows.runners` re-exports the same runner classes for older code. For Hermes CLI, keep using that path: `agent_cli_adapter` receives the durable runner request on stdin, expands `agent(..., model="...")` with `--agent-model-arg`, and passes the rendered prompt to Hermes as `--oneshot <prompt>` with `--agent-prompt-arg`.

```text
agent(..., model="openrouter/example")
  -> durable external_agent command stores the agent request, including model
  -> Workflow Runner v2 leases the existing external_agent command
  -> existing SubprocessAgentRunner invokes hermes_workflows.agent_cli_adapter
  -> adapter invokes: hermes --model openrouter/example --oneshot <request prompt>
  -> adapter returns strict JSON output to the existing agent step path
```

Provider CLIs do not agree on a standard model flag, so hermes-workflows only appends model argv when the operator configures one or more model argument templates. Each `--agent-model-arg` entry is appended only for requests with a non-empty model, with `{model}` replaced by the requested model. Examples:

```bash
# Provider uses one --model=<name> argv entry.
hermes-workflows runner run \
  --config .hermes/workflows.registry.json \
  --agent-command provider-cli \
  --agent-model-arg '--model={model}'

# Provider uses a flag/value pair.
hermes-workflows runner run \
  --config .hermes/workflows.registry.json \
  --agent-command provider-cli \
  --agent-model-arg --model \
  --agent-model-arg '{model}'
```

Use bounded flags only for tests, smoke checks, and recovery:

```bash
# Execute one command and exit.
hermes-workflows runner once --config .hermes/workflows.registry.json

# Drain a small smoke run, then exit after becoming idle.
hermes-workflows runner run \
  --config .hermes/workflows.registry.json \
  --max-commands 10 \
  --idle-exit-after 1
```

For production-ish use, supervise the same `runner run` command with launchd, systemd, s6, tmux, or another process manager and omit `--idle-exit-after`. Do not start a daemon against a live DB until the foreground command and dashboard/catalog are pointed at the same registry/DB and have been smoke-tested.

Environment fallback for agent runners:

```bash
# Existing adapter path for Hermes CLI: model goes to --model; prompt goes to --oneshot.
export HERMES_WORKFLOWS_AGENT_COMMAND=python
export HERMES_WORKFLOWS_AGENT_REQUEST_STDIN=json
export HERMES_WORKFLOWS_AGENT_ARGS_JSON='["-m","hermes_workflows.agent_cli_adapter","--agent-command","hermes","--agent-model-arg","--model","--agent-model-arg","{model}","--agent-prompt-arg","--oneshot"]'

# Generic provider runner: configure provider argv and optional model templates.
export HERMES_WORKFLOWS_AGENT_COMMAND=<provider-command>
export HERMES_WORKFLOWS_AGENT_ARGS_JSON='["--some-arg"]'
export HERMES_WORKFLOWS_AGENT_MODEL_ARGS_JSON='["--model={model}"]'
```

## 5. Author workflows with the public facade

Launch-facing workflow authors should import the small facade:

```python
from dataclasses import dataclass
from typing import Literal

from hermes_workflows import agent, ask, bash, goal, parallel, pipeline, workflow


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@workflow
async def reviewable_draft_workflow(inputs):
    draft = await agent(
        "writer",
        prompt="Draft a concise packet for the requested topic.",
        input={"topic": inputs["topic"]},
        returns=dict,
    )
    decision = await ask(
        prompt="Review this packet.",
        key="review_packet",
        input=draft,
        returns=ReviewDecision,
    )
    return {"draft": draft, "decision": decision.action, "side_effects": {"sent": 0}}


if __name__ == "__main__":
    raise SystemExit(reviewable_draft_workflow.run())
```

Use `parallel([...])` for fan-out/fan-in, `pipeline(items, stage_a, stage_b, ...)` for staged item work, `bash(...)` for deterministic shell checks, and `goal(do_fn, check_fn, max_iters=...)` for bounded improve-until-accepted loops. Avoid teaching new users `WorkflowEngine`, low-level runtime context APIs, `step`, or manual command draining unless you are writing an adapter, migration, or advanced test. See [Author workflows](authoring.html) for the complete launch-facing SDK guide.

## 6. Record human decisions

For typed `ask(...)` review requests, respond through the Review Queue adapter or the lower-level runtime API used by that adapter. The response payload must match the request schema and include human provenance. A response that satisfies a human gate is only dogfood-valid when a real human provided it and the adapter recorded that provenance (`by`, `channel`, plus message/event id or equivalent). Test fixtures, local smokes, and manual signals must be labeled as test/manual provenance and must not be reported as human approval.

Hermes plugin/tool shape:

```json
{
  "db": "default",
  "workflow_id": "wf_reviewable_draft_demo",
  "key": "review_draft_packet",
  "payload": {"action": "approve", "feedback": null},
  "by": "operator",
  "channel": "dashboard",
  "resume": false
}
```

Review Queue responses create an inspectable workflow continuation. With `resume=false`, the runtime only records the operator response and leaves a visible `run_workflow` continuation command with reason `operator_response`; a trusted foreground runner must consume that command. Trusted local adapters may still request `resume=true`, but operators should treat the returned post-resume state and command history as the source of truth. Continuation should be observable in `hermes-workflows status --commands recent` / `hermes-workflows runner status`, not hidden inside a chat callback.

## 7. Configure the Hermes dashboard/plugin

The Hermes plugin should point at the same DB aliases and workflow catalog that the CLI and runner use. A mismatched dashboard DB is the fastest way to make real approvals look missing.

Hermes profile config shape:

```yaml
plugins:
  enabled:
    - hermes-workflows-approvals
  entries:
    hermes-workflows-approvals:
      workflow_dbs:
        - name: default
          path: /absolute/path/to/workspace/.hermes/workflows.sqlite
      workflow_catalog:
        - name: reviewable-draft
          workflow_ref: hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow
          db: default
          project_root: /absolute/path/to/workspace
          python_paths:
            - /absolute/path/to/hermes-workflows/src
```

The dashboard route is `/workflows`. It should show a Review Queue, active workflow source alias, run state, recent events, command diagnostics, and redacted artifacts. Dashboard approval decisions and typed Review Queue responses from `ask(...)` / `select(...)` do not use dashboard approver ids. The backend strips browser-supplied actor/provenance fields and stamps dashboard event provenance. Review Queue responses/approval decisions create inspectable continuation state. Trusted local adapters may request `resume=true` and return the resulting post-resume state; remote or untrusted adapters may pass `resume=false` for record-only behavior. In both cases, command history and `runner status` remain the operator truth for whether work is queued, running, stuck, or complete.

Environment fallback for local smokes:

```bash
export HERMES_WORKFLOWS_DBS='{"default":"/absolute/path/to/workspace/.hermes/workflows.sqlite"}'
export HERMES_WORKFLOWS_CATALOG='[{"name":"reviewable-draft","workflow_ref":"hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow","db":"default","project_root":"/absolute/path/to/workspace"}]'
```

Dashboard routes intentionally use configured aliases instead of arbitrary DB paths. That keeps the Hermes process from becoming a local SQLite file browser.

## 8. Inspect state and recover

```bash
hermes-workflows status --db .hermes/workflows.sqlite --id wf_reviewable_draft_demo --commands recent
hermes-workflows runner status --db .hermes/workflows.sqlite
hermes-workflows runner doctor --config .hermes/workflows.registry.json --db default
hermes-workflows events --db .hermes/workflows.sqlite --id wf_reviewable_draft_demo --limit 20
hermes-workflows outbox --db .hermes/workflows.sqlite --id wf_reviewable_draft_demo
hermes-workflows list --db .hermes/workflows.sqlite
```

Runner v2 status surfaces use this state machine:

| State | How to interpret it | Recovery path |
| --- | --- | --- |
| Waiting on Skylar | A typed Review Queue request or approval gate is waiting for a real human response. | Record the response through the dashboard/plugin/adapter with human provenance. |
| Queued | Runnable work exists, but no foreground runner has claimed it. | Start `runner run --config ...`, or use `runner once` for one-command repair. |
| Running | A runner has a live claim/heartbeat for the command. | Wait, or inspect that foreground process if it exceeds expected time. |
| Stuck | The command repeatedly failed, its lease expired, the claiming heartbeat is stale, or old queued work has no healthy runner. | Run `runner status`, `runner doctor`, inspect `status --commands recent`, repair the runner/agent config, then retry intentionally. |
| Failed | The workflow failed terminally. | Inspect events and command diagnostics; launch a corrected run rather than mutating history. |
| Completed | The workflow completed terminally. | Review receipts/artifacts. |
| Cancelled | The workflow was cancelled. | No continuation; launch a new run if needed. |

If `status` shows queued commands but nothing changes, the runner is not running, is pointed at the wrong registry/DB, lacks an agent runner, or is failing command execution. Fix that instead of manually poking resume commands. If a human response was recorded with `resume=false`, you should see a visible continuation command queued for the runner; if you do not, inspect the response receipt and recent events before claiming completion.

## Advanced / legacy commands

`invoke`, `resume-trusted`, `resume-pending`, scoped `worker <workflow_ref> --db ... --id ...`, and direct `WorkflowEngine` embedding are advanced adapter/recovery surfaces. The legacy `hermes-workflows worker --config ...` command remains compatible, but new operators should prefer `hermes-workflows runner run` / `hermes-workflows runner once`. These advanced commands remain useful for tests, migrations, and controlled repairs, but they should not be the default setup path for new agents. The default path is:

```text
registry -> hermes-workflows run -> foreground hermes-workflows runner run --config -> Review Queue -> visible continuation command -> runner continuation
```

## Safety defaults

- Keep CLI, runner/worker, dashboard, and Review Queue on the same configured DB.
- Keep workflow source import roots in the registry/catalog; do not make operators pass raw persistence paths in normal use.
- Do not run downstream workflow code inside a chat/gateway callback.
- Generated workflow code is inspectable, not silently trusted.
- Approval to execute generated code is separate from approval to create drafts or send/publish/deploy.
- Public packets omit raw private data, secret-looking fields, raw local file paths, and real participant exports.
- Side effects should be explicit workflow steps with their own review/approval keys.

If your agent cannot explain what it read, what it generated, who reviewed it, which DB/source owns the run, and what it did not do, it is not ready to touch production systems.

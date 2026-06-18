---
layout: page
title: Set up hermes-workflows for an agent
---

# Set up `hermes-workflows` for an agent

`hermes-workflows` is a durable runtime you put underneath an agent when work needs state, review gates, external worker execution, and receipts. The normal production-ish shape is one workspace registry, one shared workflow DB per source, and one resident Workflow Worker that drains work from that registry.

## 1. Install in the operator workspace

Use a trusted Hermes workspace or another local operator workspace to own the checkout, venv, registry, workflow DB, dashboard config, and worker supervisor.

```bash
git clone https://github.com/<owner>/hermes-workflows.git
cd hermes-workflows
python -m pip install .

hermes-workflows --help
hermes-workflows doctor \
  --db .hermes/workflows.sqlite \
  --workflow-ref hermes_workflows.examples.trip:trip_planning_workflow
```

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
    "trip": {
      "workflow_ref": "hermes_workflows.examples.trip:trip_planning_workflow",
      "db": "default",
      "default_input": {"approver": "human:operator"}
    }
  }
}
```

Relative DB paths are resolved from the registry file, so `workflows.sqlite` means `.hermes/workflows.sqlite` when the registry lives under `.hermes/`. Use absolute paths if the DB is shared by multiple workspaces or services.

Validate aliases before wiring a resident worker:

```bash
hermes-workflows registry doctor --config .hermes/workflows.registry.json
```

## 3. Start workflow runs

Start or replay a workflow instance through the registry:

```bash
hermes-workflows run trip \
  --config .hermes/workflows.registry.json \
  --id wf_trip_demo \
  --input-json '{"destination":"NYC"}'
```

`run` records the workflow activation and queues missing work. It is not the always-on continuation loop. A run can return `running` before the Review Queue request exists because a worker still needs to execute queued steps and replay the workflow to the next wait.

## 4. Run the resident Workflow Worker

For recurring agent-owned workflows, run one resident worker from the same registry:

```bash
hermes-workflows worker \
  --config .hermes/workflows.registry.json \
  --worker-id workflows-local-worker \
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

The worker leases runnable or lease-expired `run_workflow`, `run_step`, `external_agent`, and child-workflow commands from configured DBs. It loads each instance's stored `workflow_ref` through the registry, executes the command, records durable output, and replays the workflow until it reaches a Review Queue request, another durable wait, or a terminal state.

`agent(...)` already runs through the existing agent-step machinery: the workflow emits an `external_agent` command, the worker calls `WorkflowEngine.agent_runner`, and the standard `SubprocessAgentRunner` runs the configured adapter command. For Hermes CLI, keep using that path: `agent_cli_adapter` receives the durable runner request on stdin, expands `agent(..., model="...")` with `--agent-model-arg`, and passes the rendered prompt to Hermes as `--oneshot <prompt>` with `--agent-prompt-arg`.

```text
agent(..., model="openrouter/example")
  -> durable external_agent command stores the agent request, including model
  -> Workflow Worker leases the existing external_agent command
  -> existing SubprocessAgentRunner invokes hermes_workflows.agent_cli_adapter
  -> adapter invokes: hermes --model openrouter/example --oneshot <request prompt>
  -> adapter returns strict JSON output to the existing agent step path
```

Provider CLIs do not agree on a standard model flag, so hermes-workflows only appends model argv when the operator configures one or more model argument templates. Each `--agent-model-arg` entry is appended only for requests with a non-empty model, with `{model}` replaced by the requested model. Examples:

```bash
# Provider uses one --model=<name> argv entry.
hermes-workflows worker \
  --config .hermes/workflows.registry.json \
  --agent-command provider-cli \
  --agent-model-arg '--model={model}'

# Provider uses a flag/value pair.
hermes-workflows worker \
  --config .hermes/workflows.registry.json \
  --agent-command provider-cli \
  --agent-model-arg --model \
  --agent-model-arg '{model}'
```

Use bounded flags only for tests, smoke checks, and recovery:

```bash
# Execute one command and exit.
hermes-workflows worker --config .hermes/workflows.registry.json --once

# Drain a small smoke run, then exit after becoming idle.
hermes-workflows worker \
  --config .hermes/workflows.registry.json \
  --max-commands 10 \
  --idle-exit-after 1
```

For production-ish use, supervise the worker with launchd, systemd, s6, tmux, or another process manager and omit `--idle-exit-after`.

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

from hermes_workflows import agent, ask, parallel, pipeline, workflow


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

Use `parallel([...])` for fan-out/fan-in and `pipeline(items, stage_a, stage_b, ...)` for staged item work. Avoid teaching new users `WorkflowEngine`, low-level `ctx.approval.request`, `step`, or manual command draining unless you are writing an adapter, migration, or advanced test.

## 6. Record human decisions

For CLI approval gates in existing workflows:

```bash
hermes-workflows approve hermes_workflows.examples.trip:trip_planning_workflow \
  --db .hermes/workflows.sqlite \
  --id wf_trip_demo \
  --key approve_trip_plan \
  --by operator \
  --channel cli \
  --message-id approval-message-1
```

For typed `ask(...)` review requests, respond through the Review Queue adapter or the lower-level runtime API used by that adapter. The response payload must match the request schema and include human provenance. Dashboard and gateway callbacks should normally record the response or approval and leave continuation to the resident worker.

## 7. Configure the Hermes dashboard/plugin

The Hermes plugin should point at the same DB aliases and workflow catalog that the CLI and worker use. A mismatched dashboard DB is the fastest way to make real approvals look missing.

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
        - name: trip
          workflow_ref: hermes_workflows.examples.trip:trip_planning_workflow
          db: default
          project_root: /absolute/path/to/workspace
          python_paths:
            - /absolute/path/to/hermes-workflows/src
      dashboard_approver_id: operator
```

The dashboard route is `/workflows`. It should show a Review Queue, active workflow source alias, run state, recent events, command diagnostics, and redacted artifacts. Approval/review buttons are record-only in the dashboard by default; the Workflow Worker performs continuation.

Environment fallback for local smokes:

```bash
export HERMES_WORKFLOWS_DBS='{"default":"/absolute/path/to/workspace/.hermes/workflows.sqlite"}'
export HERMES_WORKFLOWS_CATALOG='[{"name":"trip","workflow_ref":"hermes_workflows.examples.trip:trip_planning_workflow","db":"default","project_root":"/absolute/path/to/workspace"}]'
export HERMES_WORKFLOWS_DASHBOARD_APPROVER_ID=operator
```

Dashboard routes intentionally use configured aliases instead of arbitrary DB paths. That keeps the Hermes process from becoming a local SQLite file browser.

## 8. Inspect state

```bash
hermes-workflows status --db .hermes/workflows.sqlite --id wf_trip_demo --commands recent
hermes-workflows events --db .hermes/workflows.sqlite --id wf_trip_demo --limit 20
hermes-workflows outbox --db .hermes/workflows.sqlite --id wf_trip_demo
hermes-workflows list --db .hermes/workflows.sqlite
```

If `status` shows queued commands but nothing changes, the worker is not running, is pointed at the wrong registry/DB, lacks an agent runner, or is failing command execution. Fix that instead of manually poking resume commands.

## Advanced / legacy commands

`invoke`, `resume-trusted`, `resume-pending`, scoped `worker <workflow_ref> --db ... --id ...`, and direct `WorkflowEngine` embedding are advanced adapter/recovery surfaces. They remain useful for tests, migrations, and controlled repairs, but they should not be the default setup path for new agents. The default path is:

```text
registry -> hermes-workflows run -> resident hermes-workflows worker --config -> Review Queue -> worker continuation
```

## Safety defaults

- Keep CLI, worker, dashboard, and Review Queue on the same configured DB.
- Keep workflow source import roots in the registry/catalog; do not make operators pass raw persistence paths in normal use.
- Do not run downstream workflow code inside a chat/gateway callback.
- Generated workflow code is inspectable, not silently trusted.
- Approval to execute generated code is separate from approval to create drafts or send/publish/deploy.
- Public packets omit raw private data, secret-looking fields, raw local file paths, and real participant exports.
- Side effects should be explicit workflow steps with their own review/approval keys.

If your agent cannot explain what it read, what it generated, who reviewed it, which DB/source owns the run, and what it did not do, it is not ready to touch production systems.

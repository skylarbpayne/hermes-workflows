---
name: hermes-workflows-running
description: "Run, inspect, resume, and troubleshoot Hermes Workflows from CLI, worker, dashboard, and Review Queue surfaces. Use when executing an existing workflow, checking status, responding to review requests, or diagnosing a waiting/stuck run."
---

# Hermes Workflows — running workflows

Use this for operating an existing Hermes Workflow. This skill is generic product/operator knowledge, not a place for project-specific workflow designs.

## Mental model

- A workflow run is durable state plus commands, events, review requests, artifacts, and side-effect receipts.
- `run` starts or re-enters a workflow instance.
- `worker` executes runnable workflow/step/agent commands.
- `ask(...)` and approval gates show up in the Review Queue and require a typed human/operator response.
- Approval/resume is not broad side-effect authorization unless the gate says so. Commit/push/merge/send/publish/schedule/payment/credential changes need explicit gates.

## Before running

1. Identify the workflow ref or registry alias.
2. Identify the intended project root / Python path.
3. Identify the workflow DB/source alias the dashboard and worker will also read.
4. Check whether an agent runner is required for `agent(...)` steps.
5. If the workflow can perform external side effects, confirm it has gates before those transitions.

## Common command shape

From a source checkout:

```bash
PYTHONPATH=src:. hermes-workflows run <workflow-alias-or-ref>   --config path/to/workflows.registry.json   --project-root .   --db default   --id wf_example   --input-json '{"key":"value"}'
```

Then run a worker against the same config/source:

```bash
PYTHONPATH=src:. hermes-workflows worker   --config path/to/workflows.registry.json   --db default   --worker-id local-worker   --max-commands 20   --idle-exit-after 0.1
```

If `agent(...)` steps are present, provide the configured runner, for example:

```bash
hermes-workflows worker   --config path/to/workflows.registry.json   --db default   --worker-id agent-worker   --agent-command "$HERMES_WORKFLOWS_AGENT_COMMAND"   --agent-request-stdin json
```

## Inspect status and evidence

```bash
hermes-workflows status   --db path/to/workflows.sqlite   --id wf_example   --recent-events 50
```

Check:

- workflow status: running, waiting, completed, failed, cancelled;
- `waiting_on` / Review Queue keys;
- pending commands / worker ownership;
- recent events;
- artifacts and side-effect ledger;
- whether the dashboard is pointed at the same configured DB/source.

## Review Queue / approvals

Use the dashboard or configured Hermes workflow tools when available. CLI approval shape:

```bash
hermes-workflows approve <workflow-ref-or-alias>   --db path/to/workflows.sqlite   --id wf_example   --key approve_some_stage   --by <human-or-operator-id>   --channel cli   --message-id manual-approval-1
```

For typed `ask(...)` responses, submit the expected schema payload. Do not bypass schema semantics with raw internal signals unless explicitly debugging runtime plumbing.

## Stuck-run diagnosis

1. Status says `waiting`: find the Review Queue key and respond through the configured surface.
2. Status says `running` with pending commands: start a worker against the same DB/source/catalog.
3. Agent command is pending: confirm `--agent-command` / `HERMES_WORKFLOWS_AGENT_COMMAND` is configured.
4. Dashboard shows no run/review but CLI does: DB/source/catalog mismatch. Compare registry config and dashboard plugin DB aliases.
5. Import failure: check `--project-root`, `PYTHONPATH`, registry `python_paths`, and workflow ref path/module.
6. Repeated lease/stale-worker issues: inspect worker heartbeat/ownership and run a fresh worker with a unique id.

## Verification before saying “done”

A run is complete only when live status/evidence supports it:

- terminal workflow status is `completed`, or a clear waiting/blocker state is reported;
- expected artifacts exist or are linked in status;
- side-effect ledger matches what was approved;
- if a PR/deploy/send/publish was expected, verify the external handle directly;
- if no side effect was approved, explicitly say it did not happen.

## Do not put here

- Organization-specific workflow shapes.
- One-off presentation notes.
- User/project preferences unrelated to operating Hermes Workflows.
- Runtime development implementation scar tissue; that belongs in repo docs or a package-development skill.

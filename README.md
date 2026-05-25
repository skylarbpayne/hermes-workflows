# hermes-workflows v0

Code-first durable workflow control-plane spike for Hermes.

This is intentionally small. It proves the core idea before we build Kanban, artifact UI, agent workers, or a Hermes plugin:

- `@workflow` plain async function authoring
- `@step` durable awaits
- SQLite append-only-ish event log
- step memoization across process restarts
- graceful exit when a step/signal is pending
- local step worker execution through `run_until_idle()` / `drain()`
- command claiming/leasing for external worker processes
- approval request primitive through `ctx.approval.request(...)`
- durable fan-out/fan-in through `ctx.gather(step_a(...), step_b(...))`
- workflow-backed repository PR path through `examples.repo_pr_workflow`
- manual `signal()` resume API
- tiny cross-process CLI: `python -m hermes_workflows start|run|worker|signal|status|list`

## The core runtime idea

A workflow function is a **decider**, not a daemon.

```text
external event arrives
  -> append event to SQLite history
  -> replay workflow function from the top
  -> completed step awaits resolve from history
  -> missing step/signal emits request/command
  -> workflow exits as waiting
```

So this looks like normal Python:

```python
@workflow
async def trip_planning(ctx, inputs):
    constraints = await collect_constraints(ctx, inputs)
    options = await draft_options(ctx, constraints)
    approval = await ctx.wait_for("approval.granted", key="approve_trip_plan")
    return {"options": options, "approved_by": approval["by"]}
```

But it does **not** keep a coroutine alive. A pending step or signal raises an internal `WorkflowWaiting` exception that the engine catches, persists, and exits cleanly.

## V0 example

```python
from hermes_workflows import WorkflowEngine, workflow, step

@step
async def collect_constraints(ctx, inputs):
    # v0 decider does not execute step bodies.
    # Workers/adapters complete step commands out-of-band.
    ...

@step
async def draft_options(ctx, constraints):
    ...

@workflow
async def trip_planning(ctx, inputs):
    constraints = await collect_constraints(ctx, inputs)
    options = await draft_options(ctx, constraints)
    approval = await ctx.wait_for("approval.granted", key="approve_trip_plan")
    return {"options": options, "approved_by": approval["by"]}

engine = WorkflowEngine("workflow.sqlite")

# Start and drain local step commands until approval is needed.
print(engine.run_until_idle(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip"))

# Manual signal resumes the decider after a process restart and drains downstream steps.
engine = WorkflowEngine("workflow.sqlite")
print(engine.signal(
    "wf_trip",
    "approval.decision",
    key="approve_trip_plan",
    payload={"action": "approve", "by": "skylar"},
    source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://..."},
    idempotency_key="discord-message-1",
))
```

## Durable gather

`ctx.gather(...)` is the first fan-out/fan-in primitive. It only accepts `@step` calls in this spike:

```python
@workflow
async def research_brief(ctx, inputs):
    competitors, pricing = await ctx.gather(
        research_competitors(ctx, inputs),
        research_pricing(ctx, inputs),
    )
    return {"competitors": competitors, "pricing": pricing}
```

On the first decider pass it records `StepRequested` for every missing child and exits on `gather:0`. When workers complete the children, replay resolves the gathered results in argument order without re-running completed steps.

## Workflow-backed PR path

`examples.repo_pr_workflow` is the first repo PR operating path. It is intentionally explicit instead of magical:

1. gather git evidence from the branch against `origin/main`
2. run verification commands such as `pytest -q`
3. write a PR body with commits, changed files, diff stat, tests, and approval/merge placeholders
4. optionally push/open the GitHub PR with `gh pr create` or refresh an existing branch PR body/title
5. optionally watch GitHub checks with `gh pr checks --watch`, retrying briefly while GitHub attaches checks to a newly pushed branch
6. pause on `approve_pr_landing` so the human approval/merge provenance is recorded before status is finalized
7. write a reviewable status/landing packet under `.hermes/pr-workflows/` before approval, then overwrite it with final approval provenance after a human signal

For a real self-dogfood run from a feature branch:

```bash
PYTHONPATH=src:. python -m hermes_workflows run \
  examples.repo_pr_workflow:repo_pr_workflow \
  --db .hermes/pr-workflows/repo-pr.sqlite \
  --id wf_repo_pr_<slug> \
  --input-json '{"repo_path":"/Users/skylarpayne/code/hermes-workflows","goal":"Add workflow-backed PR operating path","verification_commands":["pytest -q","python -m compileall -q src tests examples"],"create_pr":true,"push_branch":true,"watch_checks":true,"gh_home":"/Users/skylarpayne"}'
```

The run should end waiting on `signal:approval.decision:approve_pr_landing`. That is the landing gate: review the PR/status packet, then send a human-sourced approval signal if merge/landing should proceed. Do not treat the agent opening the PR as merge approval.

## Human approval provenance

`ctx.approval.request(..., approver="human:...")` now requires `approval.decision` signals to include human provenance. Agent-authored or missing-source approval signals fail closed instead of quietly advancing the workflow.

```python
engine.signal(
    "wf_trip",
    "approval.decision",
    key="approve_trip_plan",
    payload={"action": "approve", "by": "skylar"},
    source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://..."},
)
```

The decision returned to workflow code includes the validated `source` so final reports can show who approved, where, and with what provenance.

## Minimal CLI

The CLI is intentionally boring and requires the workflow module path so a fresh process can import/register the decider and steps:

```bash
PYTHONPATH=src:. python -m hermes_workflows start \
  examples.first_real_trip_workflow:first_real_trip_workflow \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --input-json '{"destination":"NYC"}'

PYTHONPATH=src:. python -m hermes_workflows worker \
  examples.first_real_trip_workflow:first_real_trip_workflow \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --worker-id worker-1 \
  --once

PYTHONPATH=src:. python -m hermes_workflows signal \
  examples.first_real_trip_workflow:first_real_trip_workflow \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --type approval.decision \
  --key approve_trip_plan \
  --payload-json '{"action":"approve","by":"skylar"}' \
  --source-json '{"kind":"human","id":"skylar","channel":"discord","message_url":"discord://..."}' \
  --idempotency-key manual-approval-1

PYTHONPATH=src:. python -m hermes_workflows status \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip

PYTHONPATH=src:. python -m hermes_workflows list \
  --db /tmp/hermes-workflows.sqlite
```

## Current limitations

V0 is a spike, not production runtime:

- external worker process exists, but it is intentionally small and workflow-specific
- command claiming/leasing exists, but no backoff policy yet
- `ctx.gather` fan-out enqueues child steps together, but the local v0 drain loop still executes runnable commands serially
- no full approval policy engine yet, only approval request + source-provenance validation
- no feedback/rerun invalidation yet
- no Kanban/artifact adapters yet
- no workflow versioning/determinism guard yet
- no async caller support; `WorkflowEngine` is sync for the spike

Those are next slices after proving the durability model.

## Run tests

```bash
pytest -q
```

Expected now:

```text
26 passed
```

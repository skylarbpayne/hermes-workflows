# hermes-workflows v0

Code-first durable workflow control-plane spike for Hermes.

This is intentionally small. It proves the core idea before we build Kanban, artifact UI, agent workers, or a Hermes plugin:

- `@workflow` plain async function authoring
- `@step` durable awaits
- SQLite append-only-ish event log
- step memoization across process restarts
- graceful exit when a step/signal is pending
- local step worker execution through `run_until_idle()` / `drain()`
- approval request primitive through `ctx.approval.request(...)`
- manual `signal()` resume API
- idempotent signal handling

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
    idempotency_key="discord-message-1",
))
```

## Current limitations

V0 is a spike, not production runtime:

- no external/distributed worker process yet; only the local in-process worker loop exists
- no command claiming/locking/backoff yet
- no parallel `ctx.gather` yet
- no full approval policy engine yet, only approval request + signal mechanics
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
2 passed
```

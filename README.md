# hermes-workflows v0

Code-first durable workflow control-plane spike for Hermes.

This is intentionally small. It proves the core idea before we build Kanban, artifact UI, agent workers, or a Hermes plugin:

- `@workflow` plain async function authoring
- `@step` durable awaits
- SQLite append-only-ish event log
- step memoization across process restarts
- graceful exit when a step/signal is pending
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

# Start: emits StepRequested + run_step outbox command, then exits waiting.
print(engine.start(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip"))

# Simulate worker completion after a process restart.
engine = WorkflowEngine("workflow.sqlite")
print(engine.complete_step("wf_trip", "step:collect_constraints:0", {"hard": ["no red eyes"]}))

# Simulate next worker completion; workflow now waits for approval signal.
print(engine.complete_step("wf_trip", "step:draft_options:0", {"summary": "NYC plan"}))

# Manual signal resumes and completes the workflow.
print(engine.signal(
    "wf_trip",
    "approval.granted",
    key="approve_trip_plan",
    payload={"by": "skylar", "decision": "approved"},
    idempotency_key="discord-message-1",
))
```

## Current limitations

V0 is a spike, not production runtime:

- no worker loop yet
- no command claiming/locking/backoff yet
- no parallel `ctx.gather` yet
- no approval policy engine yet
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

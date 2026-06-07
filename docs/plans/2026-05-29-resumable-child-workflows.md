# Resumable Child Workflows Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after explicit human approval. Do not treat this plan PR landing as implementation approval.

**Goal:** Let parent workflows safely pause on child workflows that are waiting for signals or approvals, then resume the parent after the child completes.

**Architecture:** Keep child workflows as separate durable workflow instances. Parent workflows should record `ChildWorkflowRequested`, enqueue `start_child_workflow`, then wait on the correct parent wait key while the child is still waiting/running instead of failing closed. For single children, the parent wait key is the child event key; for `ctx.map_workflow(...)`, it is the existing `child-gather:<group>` key, not whichever individual child command ran last. When a child later completes, the engine records `ChildWorkflowCompleted` on the parent and reruns the parent decider. No generated workflow is imported or run before the existing generated-code approval gate succeeds.

**Tech Stack:** Python, SQLite-backed workflow event store, existing `WorkflowEngine`, `WorkflowContext.start_child`, `WorkflowContext.map_workflow`, CLI `signal`/`run`/`status` commands, pytest.

---

## Why this is the next slice

PR #20 landed the real CLI-backed `AgentStep` adapter. That means the workflow tool can now ask a trusted local agent command to produce strict JSON and even generate a `Workflow` typed value behind approval.

The next product blocker is not another provider. It is orchestration: generated or normal child workflows can currently complete synchronously, but a child that waits for a signal or human approval makes the parent fail with `ChildWorkflowIncomplete`.

Current documented limitation in `docs/architecture/dynamic-sub-workflows.md`:

> Child workflows that pause on human approval fail closed for now instead of deadlocking the parent. Parent wake-up after an independently signaled child is a later slice.

This plan implements that later slice.

## Current behavior to change

Relevant files:

- `src/hermes_workflows/engine.py`
  - `_execute_start_child_workflow_command(...)` runs the child via `self.run_until_idle(...)`.
  - If child result is `completed`, parent gets `ChildWorkflowCompleted` and reruns.
  - If child result is `failed`, parent fails.
  - Any other status, including `waiting`, currently appends `ChildWorkflowFailed` with `ChildWorkflowIncomplete` and fails the parent.
  - `WorkflowContext.start_child(...)` already looks for prior `ChildWorkflowCompleted` / `ChildWorkflowFailed` events and raises `WorkflowWaiting(event_key)` when child output is not ready.
  - `WorkflowContext.map_workflow(...)` already records `ChildWorkflowGatherWaiting` when children are pending.

- `tests/test_dynamic_workflow_return.py`
  - `test_child_workflow_waits_fail_closed_instead_of_deadlocking_parent` locks in the old fail-closed behavior.
  - `dynamic_waiting_child_pipeline` and `waiting_child` are the right fixtures for the new behavior.

## Desired behavior

### Single child

Given this parent:

```python
@workflow
async def parent(ctx, inputs):
    processor = await AgentStep(
        "build_waiting_child",
        prompt="Write a Python workflow that waits for a signal.",
        returns=Workflow,
        mock_output={"source": WAITING_CHILD_SOURCE, "symbol": "waiting_child"},
    )(ctx)
    return await processor(ctx, inputs["item"], key=inputs["item"]["id"])
```

And this generated child:

```python
@workflow
async def waiting_child(ctx, item):
    payload = await ctx.wait_for("dynamic.ready", key=item["id"])
    return {"payload": payload}
```

Expected flow:

1. Parent starts and generates/loads child workflow value.
2. Generated workflow approval is requested if required.
3. After approval, parent records `ChildWorkflowRequested` and enqueues `start_child_workflow`.
4. Worker starts the child.
5. Child records `WaitRequested` and becomes `waiting` on `signal:dynamic.ready:<id>`.
6. Parent remains `waiting` on the child event key, not `failed`.
7. A human/system sends `dynamic.ready` signal to the child workflow id.
8. Child completes.
9. Parent is woken/replayed, records `ChildWorkflowCompleted`, and completes with the child result.

### Mapped children

For `ctx.map_workflow(processor, items, key_fn=...)`:

1. Parent requests all missing children in deterministic item order.
2. Completed child results are reused from `ChildWorkflowCompleted` events.
3. Waiting/running children keep the parent waiting on `child-gather:<group>`, not an individual child key. This matters because multiple child commands may complete or wait in any drain order; the parent-level wait key must keep representing the map/gather barrier.
4. When individual children complete, the parent records completion events and replays.
5. Parent result preserves original item order.
6. If any child fails, parent fails with a durable `ChildWorkflowFailed` event for that child key.

## Non-goals

- No new provider credentials, model config, or network assumptions.
- No worker pool/concurrency scheduler. `run_until_idle` may still drain serially.
- No sandbox. Generated-code approval remains a gate, not a sandbox.
- No automatic approval of generated code.
- No cross-database parent/child references.
- No background daemon requirement. The CLI should be able to demonstrate the flow with explicit `run`/`signal`/`drain` calls.

## Design decisions

### 1. Waiting child is not an error

Replace the current `ChildWorkflowIncomplete` failure path for `child_result.status in {"waiting", "running"}` with a durable waiting state on the parent.

Proposed internal helper:

```python
def _record_child_waiting(
    self,
    con: sqlite3.Connection,
    *,
    parent_workflow_id: str,
    child_event_key: str,
    child_workflow_id: str,
    child_status: str,
    child_waiting_on: str | None,
    parent_waiting_on: str,
) -> None:
    self._append_event(
        con,
        parent_workflow_id,
        "ChildWorkflowWaiting",
        key=child_event_key,
        payload={
            "child_workflow_id": child_workflow_id,
            "status": child_status,
            "waiting_on": child_waiting_on,
        },
        idempotency_key=f"child-waiting:{child_event_key}:{child_status}:{child_waiting_on or ''}",
        ignore_duplicate=True,
    )
    con.execute(
        """
        UPDATE workflow_instances
        SET status = 'waiting', waiting_on = ?, updated_at = ?
        WHERE id = ? AND status != 'cancelled'
        """,
        (parent_waiting_on, _now(), parent_workflow_id),
    )
```

For a single `ctx.start_child(...)`, `parent_waiting_on` is the child event key. For `ctx.map_workflow(...)`, preserve the existing `workflow_instances.waiting_on` when it is a `child-gather:<group>` matching the child command's map group; otherwise reconstruct `child-gather:<group>` from the command payload's `group` when it starts with `map:`. Do not let a waiting mapped child overwrite the parent wait key with the individual child key.

The parent decider already raises `WorkflowWaiting(event_key)` from `start_child(...)` and `WorkflowWaiting(child-gather:<group>)` from `map_workflow(...)`. This helper keeps the stored parent state aligned with that decider behavior.

### 2. Parent wake-up should be explicit and deterministic

Add an engine method that can reconcile parent child requests after a child changes state. Keep it small and synchronous for this slice.

Proposed shape:

```python
def reconcile_child_result(self, parent_workflow_id: str, child_event_key: str) -> RunResult:
    """If the requested child is terminal, record parent child result and rerun parent."""
```

Implementation approach:

- Load the parent `ChildWorkflowRequested` event for `child_event_key`.
- Read `child_workflow_id` from that payload.
- Load the parent instance before touching child state.
- If the parent is already `completed`, `failed`, or `cancelled`, return the current parent result without changing state.
- Then read child instance result via `_result_from_instance(child_workflow_id)`.
- If the child instance does not exist yet because the `start_child_workflow` command has not run, return the parent as waiting with a clear diagnostic/event rather than raising a confusing `KeyError`.
- If child is `completed`, append `ChildWorkflowCompleted` idempotently, set parent `status='running'`, and rerun parent decider.
- If child is `failed` or `cancelled`, append `ChildWorkflowFailed`, mark parent failed/cancelled according to existing parent semantics, and return.
- If child is still waiting/running, update parent `status='waiting'`, `waiting_on=<parent wait key>`, and return waiting. For mapped children, `<parent wait key>` must remain `child-gather:<group>`.

This avoids scanning all workflows for the first implementation. The CLI can call this explicitly after signaling a child.

### 3. CLI should make the flow inspectable

Extend the CLI with a small, explicit command rather than hiding behavior in magic background loops.

Proposed command:

```bash
PYTHONPATH=src:. python -m hermes_workflows reconcile-child \
  examples.dynamic_workflow_return:dynamic_waiting_child_pipeline \
  --db /tmp/resumable-child.sqlite \
  --id wf_parent \
  --child-key child:waiting_child:<hash>:needs-signal
```

If the exact child key is awkward for humans, add a bounded convenience flag:

```bash
PYTHONPATH=src:. python -m hermes_workflows reconcile-children \
  examples.dynamic_workflow_return:dynamic_waiting_child_pipeline \
  --db /tmp/resumable-child.sqlite \
  --id wf_parent
```

The convenience command should:

- inspect pending `ChildWorkflowRequested` events without matching `ChildWorkflowCompleted` / `ChildWorkflowFailed`,
- reconcile each child once,
- print a bounded JSON summary of changed parent state.

Do not add a daemon in this slice.

### 4. Generated workflow approval gate remains before child start

`WorkflowContext.start_child(...)` currently calls `_require_generated_workflow_approval(workflow_ref)` before appending `ChildWorkflowRequested`. Keep that ordering.

Tests must prove:

- unapproved generated child still waits on `approval:<key>`;
- no child workflow instance exists before approval;
- after approval, child can start and wait;
- after child signal, parent can complete.

## Implementation tasks

### Task 1: Replace the old fail-closed regression with a waiting-parent regression

**Objective:** Establish the new expected behavior before changing code.

**Files:**

- Modify: `tests/test_dynamic_workflow_return.py`

**Step 1: Rename and rewrite the old test**

Replace `test_child_workflow_waits_fail_closed_instead_of_deadlocking_parent` with:

```python
def test_child_workflow_waits_without_failing_parent(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        dynamic_waiting_child_pipeline,
        {"item": {"id": "needs-signal"}},
        workflow_id="wf_waiting_child",
    )

    assert result.status == "waiting"
    assert result.waiting_on.startswith("child:")
    assert "waiting children are not supported" not in (result.error or "")
    assert not [event for event in engine.events("wf_waiting_child") if event["type"] == "ChildWorkflowFailed"]

    child_requested = [
        event for event in engine.events("wf_waiting_child") if event["type"] == "ChildWorkflowRequested"
    ][0]
    child_id = child_requested["payload"]["child_workflow_id"]
    child_status = engine.workflow_status(child_id, recent_events=1)
    assert child_status["status"] == "waiting"
    assert child_status["waiting_on"] == "signal:dynamic.ready:needs-signal"
```

**Step 2: Run and verify failure**

Run:

```bash
PYTHONPATH=src:. pytest tests/test_dynamic_workflow_return.py::test_child_workflow_waits_without_failing_parent -q
```

Expected: FAIL because the current engine marks the parent failed with `ChildWorkflowIncomplete`.

### Task 2: Record waiting child state instead of failing the parent

**Objective:** Change `_execute_start_child_workflow_command(...)` so child `waiting`/`running` leaves the parent waiting.

**Files:**

- Modify: `src/hermes_workflows/engine.py` around `_execute_start_child_workflow_command(...)`

**Step 1: Add helper or inline logic**

In the `else:` branch after child completed/failed handling, replace the `ChildWorkflowIncomplete` failure with:

```python
parent_wait_key = _parent_wait_key_for_child_wait(
    parent_row=con.execute("SELECT waiting_on FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone(),
    child_event_key=key,
    child_group=payload.get("group"),
)
self._record_child_waiting(
    con,
    parent_workflow_id=workflow_id,
    child_event_key=key,
    child_workflow_id=child_id,
    child_status=child_result.status,
    child_waiting_on=child_result.waiting_on,
    parent_waiting_on=parent_wait_key,
)
return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=parent_wait_key)
```

`_parent_wait_key_for_child_wait(...)` should return the individual child key for a single child. For mapped children whose payload `group` starts with `map:`, it should preserve an existing matching `child-gather:<group>` wait key or reconstruct that key. This keeps the concrete code aligned with the mapped-child acceptance criteria.

**Step 2: Run targeted test**

```bash
PYTHONPATH=src:. pytest tests/test_dynamic_workflow_return.py::test_child_workflow_waits_without_failing_parent -q
```

Expected: PASS.

**Step 3: Run dynamic workflow tests**

```bash
PYTHONPATH=src:. pytest tests/test_dynamic_workflow_return.py -q
```

Expected: all pass.

### Task 3: Add parent reconciliation after child completion

**Objective:** Let a parent record child completion/failure after the child workflow later reaches a terminal state.

**Files:**

- Modify: `src/hermes_workflows/engine.py`
- Test: `tests/test_dynamic_workflow_return.py`

**Step 1: Write failing test**

Add:

```python
def test_parent_completes_after_waiting_child_is_signaled_and_reconciled(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        dynamic_waiting_child_pipeline,
        {"item": {"id": "needs-signal"}},
        workflow_id="wf_waiting_child_resume",
    )
    assert first.status == "waiting"

    child_requested = [
        event for event in engine.events("wf_waiting_child_resume") if event["type"] == "ChildWorkflowRequested"
    ][0]
    child_key = child_requested["key"]
    child_id = child_requested["payload"]["child_workflow_id"]

    child_after_signal = engine.signal(
        child_id,
        "dynamic.ready",
        key="needs-signal",
        payload={"ok": True},
        source={"kind": "test", "id": "unit"},
    )
    assert child_after_signal.status == "completed"

    final = engine.reconcile_child_result("wf_waiting_child_resume", child_key)

    assert final.status == "completed"
    assert final.result == {"payload": {"ok": True}}
    completed = [
        event for event in engine.events("wf_waiting_child_resume") if event["type"] == "ChildWorkflowCompleted"
    ]
    assert completed
    assert completed[0]["payload"]["child_workflow_id"] == child_id
```

**Step 2: Implement `WorkflowEngine.reconcile_child_result(...)`**

Pseudo-code:

```python
def reconcile_child_result(self, workflow_id: str, child_key: str) -> RunResult:
    requested = self._last_event_payload(workflow_id, "ChildWorkflowRequested", child_key)
    if requested is None:
        raise KeyError(f"no child workflow requested for key: {child_key}")

    child_id = requested["child_workflow_id"]

    with self._connect() as con:
        con.execute("BEGIN IMMEDIATE")
        parent = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
        if parent is None:
            raise KeyError(f"unknown workflow_id: {workflow_id}")
        if parent["status"] in {"completed", "failed", "cancelled"}:
            return self._result_from_row(parent)

    try:
        child_result = self._result_from_instance(child_id)
    except KeyError:
        parent_wait_key = _parent_wait_key_for_child_wait(
            parent_row=parent,
            child_event_key=child_key,
            child_group=requested.get("group"),
        )
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._record_child_waiting(
                con,
                parent_workflow_id=workflow_id,
                child_event_key=child_key,
                child_workflow_id=child_id,
                child_status="pending",
                child_waiting_on=None,
                parent_waiting_on=parent_wait_key,
            )
        return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=parent_wait_key)

    with self._connect() as con:
        con.execute("BEGIN IMMEDIATE")
        parent = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
        if parent["status"] in {"completed", "failed", "cancelled"}:
            return self._result_from_row(parent)

        if child_result.status == "completed":
            self._append_event(... "ChildWorkflowCompleted" ...)
            con.execute("UPDATE workflow_instances SET status = 'running', waiting_on = NULL, updated_at = ? WHERE id = ?", ...)
        elif child_result.status == "failed":
            self._append_event(... "ChildWorkflowFailed" ...)
            con.execute("UPDATE workflow_instances SET status = 'failed', error_json = ?, updated_at = ? WHERE id = ?", ...)
            return RunResult(...)
        elif child_result.status == "cancelled":
            self._append_event(... "ChildWorkflowFailed" with type ChildWorkflowCancelled ...)
            con.execute("UPDATE workflow_instances SET status = 'failed', error_json = ?, updated_at = ? WHERE id = ?", ...)
            return RunResult(...)
        else:
            parent_wait_key = _parent_wait_key_for_child_wait(parent, child_key, requested.get("group"))
            self._record_child_waiting(... parent_waiting_on=parent_wait_key ...)
            return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=parent_wait_key)

    workflow_name = self._instance(workflow_id)["workflow_name"]
    return self._run_decider(workflow_id, _WORKFLOW_REGISTRY[workflow_name])
```

`_last_event_payload(...)` is a new helper to add in this slice unless the implementer chooses equivalent existing event lookup logic.

Avoid duplicating too much child-result event logic; extract helpers only if the code starts getting ugly.

**Step 3: Run the test**

```bash
PYTHONPATH=src:. pytest tests/test_dynamic_workflow_return.py::test_parent_completes_after_waiting_child_is_signaled_and_reconciled -q
```

Expected: PASS.

### Task 4: Reconcile all pending children for a parent

**Objective:** Add a convenience method for map/gather-style parent workflows.

**Files:**

- Modify: `src/hermes_workflows/engine.py`
- Test: `tests/test_dynamic_workflow_return.py`

**Step 1: Add method**

```python
def reconcile_children(self, workflow_id: str) -> RunResult:
    pending = self.pending_child_workflow_keys(workflow_id)
    result = self._result_from_instance(workflow_id)
    for child_key in pending:
        result = self.reconcile_child_result(workflow_id, child_key)
        if result.status in {"failed", "cancelled"}:
            return result
    return result
```

Add helper:

```python
def pending_child_workflow_keys(self, workflow_id: str) -> list[str]:
    requested = ... # ChildWorkflowRequested keys in seq order
    completed = ... # ChildWorkflowCompleted keys
    failed = ... # ChildWorkflowFailed keys
    return [key for key in requested if key not in completed and key not in failed]
```

When reconciling mapped children, derive the map wait key from the parent `ChildWorkflowGatherWaiting` event or from each requested child's `group` payload. Repeated `reconcile_children(...)` calls must be idempotent: no duplicate `ChildWorkflowCompleted` / `ChildWorkflowFailed` events and no parent status regression from terminal back to running.

**Step 2: Add mapped-child test**

Use `dynamic_waiting_child_pipeline` as a model, or add a small workflow that calls `ctx.map_workflow(...)` with `WAITING_CHILD_SOURCE` for two items. Assert:

- initial parent status is `waiting`;
- parent `waiting_on` remains `child-gather:<group>` after each still-waiting child command is processed;
- both children are waiting on their item signals;
- signaling one child and reconciling all keeps parent waiting;
- signaling the second child and reconciling all completes parent;
- result order matches input order.

**Step 3: Run tests**

```bash
PYTHONPATH=src:. pytest tests/test_dynamic_workflow_return.py -q
```

Expected: all pass.

### Task 5: Add CLI commands for explicit reconciliation

**Objective:** Make the flow usable from the command line without writing Python snippets.

**Files:**

- Modify: `src/hermes_workflows/cli.py`
- Test: existing CLI tests if present; otherwise add `tests/test_cli.py` coverage around parser/output.

**Step 1: Add parser commands**

Add:

```bash
reconcile-child MODULE:WORKFLOW --db PATH --id WORKFLOW_ID --child-key CHILD_KEY
reconcile-children MODULE:WORKFLOW --db PATH --id WORKFLOW_ID
```

Use existing workflow import loading helper used by `run`, `signal`, and `status`.

**Step 2: Print bounded JSON result**

Return the same `result_payload(result)` shape used by `run`/`signal`:

```json
{"workflow_id":"wf_parent","status":"completed","waiting_on":null,"result":{...},"error":null}
```

**Step 3: Add CLI smoke test**

Test should run commands via `subprocess.run([...], cwd=REPO_ROOT, env={"PYTHONPATH":"src"})`, not shell strings.

Expected flow:

1. Run parent, get waiting.
2. Inspect `status --json` to find child workflow id/key if CLI supports it; otherwise use engine API inside test for the key and reserve CLI for reconcile.
3. Signal child via CLI.
4. Run `reconcile-child` or `reconcile-children` via CLI.
5. Assert final JSON status completed.

### Task 6: Update docs and examples

**Objective:** Make the new behavior obvious and prevent stale docs from claiming waiting children fail closed.

**Files:**

- Modify: `docs/architecture/dynamic-sub-workflows.md`
- Modify: `examples/dynamic_workflow_return.py` or create `examples/resumable_child_workflow.py`
- Maybe modify: `README.md`

**Required doc changes:**

- Replace the current limitation line:
  - old: `Child workflows that pause on human approval fail closed for now instead of deadlocking the parent.`
  - new: parent workflows can wait on child workflows and resume after explicit reconciliation; background worker-pool automation remains future work.
- Add CLI example showing run → signal child → reconcile → status.
- State that generated workflow approval remains required before any child import/execution.

### Task 7: Full validation and independent review

**Objective:** Prove the slice is safe and does not regress prior workflow behavior.

Run:

```bash
PYTHONPATH=src:. pytest tests/test_dynamic_workflow_return.py -q
PYTHONPATH=src:. pytest -q
PYTHONPATH=src:. python -m compileall -q src tests examples
git diff --check
```

Smoke the CLI flow:

```bash
rm -f /tmp/resumable-child.sqlite
PYTHONPATH=src:. python -m hermes_workflows run \
  examples.dynamic_workflow_return:dynamic_waiting_child_pipeline \
  --db /tmp/resumable-child.sqlite \
  --id wf_resumable_child \
  --input-json '{"item":{"id":"needs-signal"}}'
# signal child workflow id discovered from status/events
PYTHONPATH=src:. python -m hermes_workflows reconcile-children \
  examples.dynamic_workflow_return:dynamic_waiting_child_pipeline \
  --db /tmp/resumable-child.sqlite \
  --id wf_resumable_child
```

Independent review must check:

- parent waiting state is durable and inspectable;
- child completion/failure is idempotently recorded exactly once;
- replay does not start duplicate children;
- generated workflow approval still happens before child import/start;
- approval-gate regression covers the live generated workflow path: unapproved generated `Workflow` creates `ApprovalRequested` only, with no `ChildWorkflowRequested`, no child workflow instance, and no generated module import before approval;
- mapped child results preserve input order;
- no broad scan/daemon behavior creates surprise side effects;
- CLI commands use argv/no shell in tests.

## Acceptance criteria

Implementation is complete only when:

- The old fail-closed test is replaced with a waiting/resume test.
- A child workflow that waits for a signal leaves the parent `waiting`, not `failed`.
- Signaling the child and reconciling records `ChildWorkflowCompleted` on the parent and completes the parent with the child result.
- `ctx.map_workflow(...)` can wait on multiple child workflows and preserve result order after reconciliation.
- Mapped parent `waiting_on` remains the child-gather key, not an arbitrary individual child key.
- Child failure still fails the parent with an inspectable `ChildWorkflowFailed` event.
- Generated workflow approval remains required before child import/start.
- CLI reconciliation works in a smoke flow.
- Docs no longer claim waiting children fail closed.
- Local full validation passes.
- GitHub checks pass on the implementation PR.

## Approval gates

- This plan can be reviewed/landed as docs-only.
- Landing this docs-only plan PR does **not** authorize implementation.
- A separate explicit the maintainer approval of this plan authorizes implementation work and opening an implementation PR.
- Implementation PR merge/landing requires separate explicit the maintainer approval.
- No generated Python from a live agent may run without the existing generated-workflow approval signal.

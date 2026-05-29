# Dynamic Python Workflow Returns

Status: implemented first slice
Date: 2026-05-28

## Problem

Static durable workflows are useful, but the dynamic-workflow unlock is this shape:

```python
items = await agent_step_producing_items(ctx, inputs)
processor = await build_item_workflow(ctx, items)  # returns a Workflow value
for item in items:
    await processor(ctx, item, key=item["id"])
```

The missing primitive was not a YAML/JSON workflow DSL. The missing primitive was making `Workflow` a normal typed value that an `AgentStep` can return.

## Design stance

Python remains the workflow language.

An agent step may return executable Python source. If the caller declares `returns=Workflow`, the runtime:

1. snapshots the generated source to the workflow DB directory,
2. hashes it,
3. validates its top-level shape,
4. if it came from a live runner, requests human approval before import/execution,
5. stores a typed `Workflow` value in the ordinary `StepCompleted` payload,
6. imports/registers the generated `@workflow` only after approval or for deterministic `mock_output`,
7. rehydrates the same value on replay without calling the agent again.

That keeps the magic where it belongs: `AgentStep` returns a value. One possible value is `Workflow`.

## Authoring surface

```python
from hermes_workflows import AgentStep, Workflow, workflow


@workflow
async def dynamic_pipeline(ctx, inputs):
    items = await AgentStep(
        "produce_items",
        prompt="Return items to process.",
        returns=list,
    )(ctx)

    processor = await AgentStep(
        "build_processor",
        prompt="Write Python defining @workflow async def process_item(ctx, item).",
        returns=Workflow,
    )(ctx)

    return await ctx.map_workflow(
        processor,
        items,
        key_fn=lambda item: item["id"],
        concurrency=8,
    )
```

Examples/tests can still use `mock_output` for deterministic generated Python without configuring a live agent runner:

```python
processor = await AgentStep(
    "build_processor",
    prompt="Write Python defining @workflow async def process_item(ctx, item).",
    returns=Workflow,
    mock_output={"source": generated_python, "symbol": "process_item"},
)(ctx)
```

For live execution, pass an `agent_runner` to `WorkflowEngine`. The runner receives a JSON-safe request packet with the prompt, rendered prompt, variables, return type, workflow id, and step key. Its exact response and provenance are persisted as `StepCompleted.metadata`.

If a live `AgentStep(..., returns=Workflow)` returns generated Python, the resulting `Workflow` is marked `approval_required=True`. Calling it with `ctx.start_child(...)`, `await processor(ctx, item)`, or `ctx.map_workflow(...)` first records an `ApprovalRequested` event and waits for `approval.decision`. No generated module is imported and no child workflow is requested until that approval is accepted.

## Replay rule

Generated Python is dynamic only at creation time. After the `AgentStep` completes, replay uses the stored `Workflow` value from history:

```json
{
  "__hermes_type__": "Workflow",
  "source": "from hermes_workflows import workflow...",
  "symbol": "process_item",
  "source_sha256": "...",
  "path": ".../generated_workflows/<sha>.py",
  "module_name": "hermes_generated_workflows.<sha>",
  "approval_required": true,
  "approval_key": "generated-workflow:<sha>:process_item",
  "provenance": {"runner_provenance": {"runner": "..."}}
}
```

The agent is not re-called on replay. The generated module is rehydrated from the stored source/path/hash.

## Child workflow execution

A `Workflow` value is callable from a parent workflow:

```python
result = await processor(ctx, item, key=item["id"])
```

That call records a deterministic child workflow request:

```text
child:<workflow-symbol>:<source-hash-prefix>:<key-or-key-hash>
```

and starts a child workflow instance:

```text
<parent-id>.child.<workflow-symbol>:<source-hash-prefix>.<key-or-key-hash>
```

The source hash is part of the child identity so two different generated workflows with the same symbol/key do not collide. Unsafe or long user keys are normalized with a hash suffix, so `a/b` and `a b` remain distinct.

Generated workflows and generated steps are registered in a generated namespace (`generated:<source-sha256>:<symbol>`), which prevents generated `@workflow`/`@step` names from overwriting static application registrations.

For many items, use:

```python
results = await ctx.map_workflow(processor, items, key_fn=lambda item: item["id"])
```

`map_workflow` starts all missing children before waiting and returns results in the original item order.

## Approval/status surface

Generated-code approvals are exposed through the normal workflow status shape:

```json
{
  "approvals": [
    {
      "key": "generated-workflow:<sha>:process_item",
      "status": "waiting",
      "artifact": {
        "kind": "generated_workflow.approval.v1",
        "symbol": "process_item",
        "source_sha256": "...",
        "runner_provenance": {"runner": "..."}
      }
    }
  ]
}
```

Approval signals use the existing human provenance check. For generated workflows the approval client currently asks for `human:skylar`; tests can satisfy that with a human source record containing `id`, `channel`, and an external event/message id.

## Current limitations

- `AgentStep` has a live injectable runner boundary, but no built-in vendor/LLM adapter yet.
- Generated module validation is intentionally narrow: parseable Python, `from hermes_workflows import workflow, step` only, at least one selected `@workflow`, no top-level executable statements beyond functions/literal assignments, and no import-time function shapes like decorator calls/default expressions/annotations. It is a shape check, not a sandbox.
- Child workflows that pause on human approval fail closed for now instead of deadlocking the parent. Parent wake-up after an independently signaled child is a later slice.
- No concurrency worker pool yet; local `run_until_idle` drains child commands serially.
- No sandbox yet. Approval prevents surprise execution; it does not make generated Python safe after approval.

## Example

Run the working example:

```bash
PYTHONPATH=src:. python -m hermes_workflows run \
  examples.dynamic_workflow_return:dynamic_item_workflow_example \
  --db /tmp/dynamic-workflow.sqlite \
  --id wf_dynamic_example \
  --input-json '{"items":[{"id":"a","label":"alpha"},{"id":"b","label":"beta"}]}'
```

Expected result:

```json
{"items":[{"processed":{"item_id":"a","label":"ALPHA"}},{"processed":{"item_id":"b","label":"BETA"}}]}
```

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
3. validates/imports it,
4. registers the generated `@workflow`,
5. stores a typed `Workflow` value in the ordinary `StepCompleted` payload,
6. rehydrates the same value on replay without calling the agent again.

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

For the first implemented slice, examples/tests use `mock_output` instead of a live model runner:

```python
processor = await AgentStep(
    "build_processor",
    prompt="Write Python defining @workflow async def process_item(ctx, item).",
    returns=Workflow,
    mock_output={"source": generated_python, "symbol": "process_item"},
)(ctx)
```

The runtime contract is real; the live agent runner can replace `mock_output` later.

## Replay rule

Generated Python is dynamic only at creation time. After the `AgentStep` completes, replay uses the stored `Workflow` value from history:

```json
{
  "__hermes_type__": "Workflow",
  "source": "from hermes_workflows import workflow...",
  "symbol": "process_item",
  "source_sha256": "...",
  "path": ".../generated_workflows/<sha>.py",
  "module_name": "hermes_generated_workflows.<sha>"
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

## Current limitations

- `AgentStep` has the durable typed-return surface, but no live LLM runner yet.
- Generated module validation is intentionally narrow: parseable Python, `from hermes_workflows import workflow, step` only, at least one selected `@workflow`, no top-level executable statements beyond functions/literal assignments, and no import-time function shapes like decorator calls/default expressions/annotations. It is a shape check, not a sandbox.
- Child workflows that pause on human approval fail closed for now instead of deadlocking the parent. Parent wake-up after an independently signaled child is a later slice.
- No concurrency worker pool yet; local `run_until_idle` drains child commands serially.
- No generated-code approval gate yet. Add one before enabling live model-generated Python with side effects.

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

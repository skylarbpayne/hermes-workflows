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

The built-in `SubprocessAgentRunner` lets that live runner be a trusted argv command instead of a Python callable. For a generic local CLI that can return strict JSON, run the optional adapter command behind it:

```python
import sys
from pathlib import Path

from hermes_workflows import SubprocessAgentRunner, WorkflowEngine

repo_root = Path(__file__).resolve().parent.parent
engine = WorkflowEngine(
    "workflow.sqlite",
    agent_runner=SubprocessAgentRunner([
        sys.executable,
        "-m",
        "hermes_workflows.agent_cli_adapter",
        "--agent-command",
        sys.executable,
        "--agent-arg",
        str(repo_root / "examples" / "runners" / "fake_json_cli_agent.py"),
    ]),
)
```

The subprocess boundary still receives an `agent_step.runner_request.v1` JSON object on stdin. The adapter turns that request into a provider prompt, invokes the configured provider CLI using argv-only `subprocess.Popen`, and requires the provider to return strict JSON on stdout:

```json
{
  "output": {"source": "from hermes_workflows import workflow\n...", "symbol": "process_item"},
  "provenance": {"runner": "my-agent-runner", "model": "example-model", "request_id": "abc123"}
}
```

`output` is required; `provenance` is optional but should be non-secret and review-useful. The subprocess runner and adapter fail closed on non-zero exit, timeout, invalid/chatty JSON, missing `output`, invalid provenance, and oversized output. The adapter redacts secret-looking argv values and bounded provider stdout/stderr diagnostics before writing provenance or error JSON. It deliberately has no provider-specific model config and no shell-string default.

Default tests and examples use `examples/runners/fake_json_cli_agent.py`, so they require no network, credentials, Hermes/Codex install, provider auth, or config mutation. Real local provider smoke is opt-in only via `HERMES_WORKFLOWS_REAL_AGENT_ADAPTER=1` and a caller-supplied `HERMES_WORKFLOWS_AGENT_COMMAND`; otherwise it is skipped.

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

Approval signals use the existing human provenance check. For generated workflows the approval client currently asks for `human:operator`; tests can satisfy that with a human source record containing `id`, `channel`, and an external event/message id.

## Resuming waiting children

A parent that starts a child workflow can remain durably waiting while the child waits for its own signal or approval. Resume is explicit: complete or signal the child, then reconcile the parent.

```bash
rm -f /tmp/resumable-child.sqlite
PYTHONPATH=src:. python -m hermes_workflows run \
  examples.dynamic_workflow_return:dynamic_waiting_child_pipeline \
  --db /tmp/resumable-child.sqlite \
  --id wf_resumable_child \
  --input-json '{"item":{"id":"needs-signal"}}'

# Discover the child workflow id/key from status/events.
PYTHONPATH=src:. python -m hermes_workflows status \
  --db /tmp/resumable-child.sqlite \
  --id wf_resumable_child

PYTHONPATH=src:. python -m hermes_workflows signal \
  examples.dynamic_workflow_return:dynamic_waiting_child_pipeline \
  --db /tmp/resumable-child.sqlite \
  --id '<child-workflow-id>' \
  --type dynamic.ready \
  --key needs-signal \
  --payload-json '{"ok":true}'

PYTHONPATH=src:. python -m hermes_workflows reconcile-children \
  examples.dynamic_workflow_return:dynamic_waiting_child_pipeline \
  --db /tmp/resumable-child.sqlite \
  --id wf_resumable_child
```

Generated workflow approval remains upstream of this flow: an unapproved generated child records `ApprovalRequested` and does not create `ChildWorkflowRequested` or a child workflow instance until the approval signal is valid.

## Current limitations

- A generic CLI adapter exists at `python -m hermes_workflows.agent_cli_adapter` for local Hermes/Codex-style commands that can produce strict JSON, but default tests use a fake CLI and real provider smoke is opt-in only; the runtime does not manage credentials or mutate provider config.
- Generated module validation is intentionally narrow: parseable Python, `from hermes_workflows import workflow, step` only, at least one selected `@workflow`, no top-level executable statements beyond functions/literal assignments, and no import-time function shapes like decorator calls/default expressions/annotations. It is a shape check, not a sandbox.
- Child workflows that pause for signals or human approval now leave the parent durably `waiting`. After the child completes, call `reconcile-child` for the specific child key or `reconcile-children` for all pending children on the parent to record the child result and replay the parent.
- No concurrency worker pool yet; local `run_until_idle` drains child commands serially, and parent wake-up is explicit reconciliation rather than a background daemon.
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

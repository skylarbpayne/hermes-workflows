---
layout: page
title: Author workflows with the public SDK
---

# Author workflows with the public SDK

The launch-facing authoring surface is deliberately small:

```python
from hermes_workflows import agent, ask, bash, goal, parallel, pipeline, workflow
```

Use those first. `WorkflowEngine`, `@step`, direct `ctx.*` calls, raw signals, approval DTOs, and outbox internals are advanced integration surfaces.

## Workflow shape

A normal workflow is an ordinary async Python function that takes one input mapping:

```python
from hermes_workflows import workflow


@workflow
async def my_workflow(inputs):
    return {"received": inputs}


if __name__ == "__main__":
    raise SystemExit(my_workflow.run())
```

The runtime records completed work durably. On replay, completed steps return stored outputs and only missing work is queued or executed.

## `agent(...)`: typed AI or worker work

`agent(...)` asks the configured worker/runner for typed output. It can run through a local fake runner, Hermes CLI, another provider CLI, or deterministic `mock_output` for examples/tests.

```python
from dataclasses import dataclass
from hermes_workflows import agent


@dataclass
class Summary:
    headline: str
    risks: list[str]


summary = await agent(
    "summarize_packet",
    prompt="Summarize this packet for a launch review.",
    input={"packet": packet},
    context={"audience": "maintainers"},
    returns=Summary,
    model="openrouter/example-model",
    key_by=packet["id"],
)
```

Common arguments:

- `prompt=`: instruction for the worker/agent.
- `input=`: structured data to process.
- `context=`: extra context that should not be confused with the primary input.
- `returns=`: `dict`, dataclass, or another JSON-compatible typed contract.
- `key=` / `key_by=`: stable identity for replay and fan-out.
- `model=`: requested model metadata. The resident worker maps this through configured runner/model argv templates.
- `tools=`, `skills=`, `files=`: optional runner hints.
- `mock_output=`: deterministic output for docs/tests/examples without provider credentials.

## `ask(...)`: typed Review Queue input

`ask(...)` mirrors `agent(...)`, but the responder is a human or review adapter instead of an AI worker.

```python
from dataclasses import dataclass
from typing import Literal
from hermes_workflows import ask


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


decision = await ask(
    "Review this launch packet.",
    key="review_launch_packet",
    input=packet,
    context={"risk": "public docs"},
    returns=ReviewDecision,
    approver="human:operator",
)
```

The Review Queue schema comes from `returns=`. A dataclass field like `action: Literal["approve", "request_changes"]` renders explicit action choices in Review Queue surfaces. Response payloads must match the schema and include provenance from the adapter/tool that records them.

## `bash(...)`: durable deterministic command steps

`bash(...)` runs a shell command as durable worker-executed work and captures stdout, stderr, exit code, timing, timeout state, and truncation flags.

```python
from hermes_workflows import bash

check = await bash(
    "python -m pytest -q",
    key="pytest",
    timeout_seconds=300,
    max_stdout_bytes=200_000,
)

if check.exit_code != 0:
    return {"status": "failed", "stderr": check.stderr[-4000:]}
```

Use it for deterministic local checks, not for unreviewed external side effects. Redact known secret values/patterns when command output may contain sensitive data.

## `parallel(...)`: fan out and fan in

`parallel(...)` starts independent durable calls before waiting for the group.

```python
from hermes_workflows import agent, parallel

notes = await parallel(
    [
        agent(
            "review_topic",
            prompt="Review this topic for launch risk.",
            input={"topic": topic},
            key_by=topic,
            returns=dict,
        )
        for topic in ["docs", "examples", "worker"]
    ],
    limit=3,
)
```

Use stable keys for fan-out items so reordering an input list does not rewrite workflow history.

## `pipeline(...)`: staged item processing

`pipeline(items, stage_a, stage_b, ...)` applies each stage to each item, with each stage able to return `agent(...)`, `ask(...)`, `bash(...)`, another awaitable, or a plain value.

```python
from hermes_workflows import agent, ask, pipeline


def draft_section(section):
    return agent("draft_section", prompt="Draft this section.", input=section, key_by=section["id"], returns=dict)


def review_section(draft):
    return ask("Review this section.", key=f"review_{draft['id']}", input=draft, returns=dict)

reviews = await pipeline(sections, draft_section, review_section, limit=2)
```

Use it when each item should pass through the same durable stages.

## `goal(...)`: bounded improve-until-accepted loops

`goal(do_fn, check_fn, max_iters=N)` runs a durable do/check loop. Both functions can return ordinary values, awaitables, or authoring calls such as `agent(...)`.

```python
from hermes_workflows import agent, goal


def revise(previous=None):
    return agent("revise_draft", prompt="Improve the draft.", input={"previous": previous}, returns=dict)


def good_enough(candidate):
    return bool(candidate.get("ready"))

final = await goal(revise, good_enough, max_iters=3)
```

Keep `max_iters` bounded and make the check explicit. If a human should decide, make the check function use `ask(...)`.

## Worker and Review Queue composition

A workflow run records state and queues work. A resident Workflow Worker drains queued workflow/step/agent/bash/child work and stops at Review Queue requests or terminal state.

```bash
hermes-workflows run my-alias --config .hermes/workflows.registry.json --id wf_example
hermes-workflows worker --config .hermes/workflows.registry.json --worker-id local-worker
```

For `agent(..., model=...)`, configure the worker runner/model mapping. For Hermes CLI one-shot style runners, the existing adapter path is:

```bash
hermes-workflows worker \
  --config .hermes/workflows.registry.json \
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

That keeps the execution path simple:

```text
agent(..., model="openrouter/example")
  -> existing external_agent command
  -> Workflow Worker leases it
  -> existing SubprocessAgentRunner invokes hermes_workflows.agent_cli_adapter
  -> adapter invokes hermes --model openrouter/example --oneshot <prompt>
  -> strict JSON output completes the agent step
```

## What to avoid in launch-facing workflows

Avoid these in day-one examples unless the document is explicitly about runtime internals:

- direct `WorkflowEngine(...)` construction;
- low-level `ctx.approval.request(...)`;
- raw `signal(...)` / `operator.response` instructions;
- hand-draining command outboxes;
- direct SQLite path routing in dashboard/operator instructions;
- broad shell commands that perform external side effects without a review gate.

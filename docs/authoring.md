---
layout: page
title: Author workflows with the public SDK
---

# Author workflows with the public SDK

The launch-facing authoring surface is deliberately small:

```python
from hermes_workflows import agent, ask, bash, goal, parallel, pipeline, workflow
```

Use those first. `WorkflowEngine`, `@step`, direct `ctx.*` calls, raw signals, approval DTOs, and outbox internals are **not intended for direct use in normal workflows**. They are low-level integration/runtime surfaces for maintainers building adapters or the runtime itself.

## Workflow shape

A normal workflow is an ordinary async Python function with a typed input and a typed result:

```python
from dataclasses import dataclass
from hermes_workflows import workflow


@dataclass
class EchoInput:
    message: str


@dataclass
class EchoResult:
    received: str


@workflow
async def my_workflow(inputs: EchoInput) -> EchoResult:
    return EchoResult(received=inputs.message)


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
class Packet:
    id: str
    body: str


@dataclass
class Summary:
    headline: str
    risks: list[str]


summary = await agent(
    "summarize_packet",
    prompt="Summarize this packet for a launch review.",
    input=Packet(id="docs", body="..."),
    context={"audience": "maintainers"},
    returns=Summary,
    model="openrouter/example-model",
    key_by="docs",
)
```

Common arguments:

- `prompt=`: instruction for the worker/agent. This is included in the durable agent request and sent to the configured runner.
- `input=`: the primary structured payload for the work. It is serialized into the durable request, sent to the runner, and used in the replay fingerprint.
- `context=`: supporting material sent alongside the prompt/input. The runtime stores it as labeled context bundles with hashes, includes it in the runner request, and includes its hash in the replay fingerprint. Use it for background/reference material, not for the primary object being transformed.
- `returns=`: dataclass, scalar, or another explicit JSON-compatible typed contract. Prefer real types over raw `dict` in public examples.
- `key=` / `key_by=`: stable identity for replay and fan-out.
- `model=`: requested model metadata. The resident worker maps this through configured runner/model argv templates.
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
    input=summary,
    context={"risk": "public docs"},
    returns=ReviewDecision,
    approver="human:operator",
)
```

The Review Queue schema comes from `returns=`. A dataclass field like `action: Literal["approve", "request_changes"]` renders explicit action choices in Review Queue surfaces. Response payloads must match the schema and include provenance from the adapter/tool that records them.

## `bash(...)`: durable deterministic command steps

`bash(...)` runs a shell command as durable worker-executed work and captures stdout, stderr, exit code, timing, timeout state, and truncation flags.

```python
from dataclasses import dataclass
from hermes_workflows import bash


@dataclass
class CheckResult:
    status: str
    stderr_tail: str | None = None


check = await bash(
    "python -m pytest -q",
    key="pytest",
    timeout_seconds=300,
    max_stdout_bytes=200_000,
)

if check.exit_code != 0:
    return CheckResult(status="failed", stderr_tail=check.stderr[-4000:])
```

Use it for deterministic local checks, not for unreviewed external side effects. Redact known secret values/patterns when command output may contain sensitive data.

## `parallel(...)`: fan out and fan in

`parallel(...)` starts independent durable calls before waiting for the group.

```python
from dataclasses import dataclass
from hermes_workflows import agent, parallel


@dataclass
class TopicReviewInput:
    topic: str


@dataclass
class TopicReview:
    topic: str
    risk: str


reviews = await parallel(
    [
        agent(
            "review_topic",
            prompt="Review this topic for launch risk.",
            input=TopicReviewInput(topic=topic),
            key_by=topic,
            returns=TopicReview,
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
from dataclasses import dataclass
from typing import Literal
from hermes_workflows import agent, ask, pipeline


@dataclass
class Section:
    id: str
    title: str


@dataclass
class DraftedSection:
    id: str
    body: str


@dataclass
class SectionReview:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


def draft_section(section: Section):
    return agent("draft_section", prompt="Draft this section.", input=section, key_by=section.id, returns=DraftedSection)


def review_section(draft: DraftedSection):
    return ask("Review this section.", key=f"review_{draft.id}", input=draft, returns=SectionReview)


reviews = await pipeline(sections, draft_section, review_section, limit=2)
```

Use it when each item should pass through the same durable stages.

## `goal(...)`: bounded improve-until-accepted loops

`goal(do_fn, check_fn, max_iters=N)` runs a durable do/check loop. Both functions can return ordinary values, awaitables, or authoring calls such as `agent(...)`.

```python
from dataclasses import dataclass
from hermes_workflows import agent, goal


@dataclass
class Draft:
    body: str
    ready: bool


@dataclass
class RevisionInput:
    previous: Draft | None = None


def revise(previous: Draft | None = None):
    return agent("revise_draft", prompt="Improve the draft.", input=RevisionInput(previous=previous), returns=Draft)


def good_enough(candidate: Draft):
    return candidate.ready


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

## Building a Review Queue adapter

A review adapter is just an input surface over durable workflow state. It should:

1. Read `WorkflowEngine(db).workflow_status(workflow_id)["review_requests"]` or the equivalent configured-source plugin API.
2. Render each request's `prompt`, `artifact`/`input`, `request_schema`, and action choices.
3. Collect a payload that matches the request schema.
4. Record it with `WorkflowEngine.submit_operator_response(...)` or the `workflow_review_respond` plugin tool, including `by`, `channel`, and `message_id`/`event_id` provenance.
5. Let the default `resume=True` continue the run immediately when the adapter is trusted to run local workflow code. If the adapter is remote/untrusted, pass `resume=False` and rely on the resident Workflow Worker for continuation.

Do not invent a second source of truth. The workflow DB stays canonical; the adapter only displays waiting requests and records typed responses with provenance.

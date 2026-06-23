# The agent had the instruction. The system did not.

A coding agent can do impressive work and still leave you with a mess.

Not because it ignored every instruction. The scarier failure is quieter than that. It follows the plan for a while. It reads the skill. It says the right things. Then somewhere across a restart, a handoff, a review, a new subagent, or a different model context, the important requirement becomes optional.

Run the tests. Stop before sending. Preserve the diff. Ask before merge. Keep the approval attached to the thing that was approved. Do not touch the live account. Write the receipt somewhere I can inspect tomorrow.

Those are not style preferences. They are obligations.

Most agent stacks still treat them like prompt text.

## The prompt is the wrong home for requirements that matter

Prompts are great for shaping behavior. Skills are great for reusable taste and procedures. Subagents are useful when the work can be split. Goals are useful when you want an agent to keep pushing until a condition is met.

But none of those, by themselves, give you a durable record of what happened.

If a requirement needs to survive process exits, review pauses, retries, handoffs, and later inspection, it needs a stronger home than "the agent was told."

That is the line Hermes Workflows draws.

Hermes Workflows is a small Python runtime for agent work with durable state, typed worker calls, typed human review, resident workers, deterministic checks, and receipts. The normal authoring surface is deliberately small:

```python
from hermes_workflows import agent, ask, bash, goal, parallel, pipeline, workflow
```

A workflow is ordinary Python. The difference is that the important parts of the work are recorded as workflow state.

## A tiny example

Here is the shape.

```python
from dataclasses import dataclass
from typing import Literal

from hermes_workflows import agent, ask, workflow


@dataclass
class Draft:
    title: str
    summary: str
    risks: list[str]


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@workflow
async def reviewable_draft_workflow(inputs):
    draft = await agent(
        "draft_packet",
        prompt="Draft a concise review packet for the supplied topic.",
        input={"topic": inputs["topic"]},
        returns=Draft,
    )

    decision = await ask(
        "Review this draft packet.",
        key="review_draft_packet",
        input=draft,
        returns=ReviewDecision,
    )

    return {
        "draft": draft,
        "decision": decision,
        "side_effects": {"published": False, "sent": False},
    }
```

The interesting part is not the syntax. The interesting part is what the runtime refuses to forget.

The agent request has a typed input, prompt, return schema, fingerprint, output, and step key. The review request has a schema-derived input surface. The worker can stop at human review instead of pretending approval happened. A later process can resume from the durable state instead of replaying the whole chat.

## The worker is part of the model

Long-running agent work should not require the initiating CLI process to stay alive forever.

Hermes Workflows separates starting a run from continuing a run:

```text
operator starts or replays workflow
  -> durable workflow activation is recorded
  -> missing workflow, step, agent, bash, or child work is queued
  -> command exits after current durable state is stored

resident Workflow Worker
  -> leases queued commands from configured DBs
  -> executes step, agent, bash, or child workflow work
  -> re-enters the workflow until it is waiting or terminal

review surface
  -> records typed human input or approval with provenance
  -> worker observes the durable transition and continues
```

That split matters. Without a worker, a workflow ledger can still become another place where work gets stuck. The worker is the component that owns continuation.

## Review is typed, not a mystery button

Human review is part of the workflow, not a side-channel comment.

If the workflow asks for:

```python
@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None
```

then Review Queue surfaces can render exactly those choices. The response can be recorded with who answered, from where, when, and what payload matched the schema.

That is different from asking an agent to "get approval" and hoping the next continuation interprets a chat reply correctly.

## Deterministic checks get receipts too

Agent work is not the only thing worth recording.

`bash(...)` lets a workflow run deterministic local checks as durable steps. The result includes stdout, stderr, exit code, timing, timeout state, and truncation flags. That makes checks reviewable evidence instead of a line in a final summary.

A coding workflow can require:

- plan packet
- branch/worktree evidence
- source diff
- tests run
- review packet
- explicit merge approval
- post-merge validation

Each stage can be state, not folklore.

## Parallel and pipeline work stop being a pile of chat tabs

Real agent work fans out.

Research topics run in parallel. Sections move through draft, fact-check, and review. Items get retried independently. Some pass. Some need edits. Some should not rerun just because a sibling failed.

`parallel(...)` and `pipeline(...)` give that work stable keys and durable stage boundaries. The runtime can tell which item is waiting, which one completed, and which review payload belongs to which output.

That is the difference between "five agents did some stuff" and an inspectable workflow.

## Dynamic workflows are allowed

A workflow does not have to be a static script.

Hermes Workflows can accept a generated workflow as a typed value. An agent can return workflow source, the runtime can validate and store it with a source hash, and the parent workflow can run child workflow instances from that value.

That matters for agent systems because sometimes the next process is discovered during the work. The trick is not letting that become uninspectable magic. Generated workflow code still gets a hash, child workflow IDs, events, outputs, and receipts.

## What belongs in a workflow

Do not wrap every task in ceremony. That is how good tools become wet cement.

Use a workflow when the work has obligations:

- a human must approve before a risky transition
- a deterministic check must run before the next stage
- an artifact needs to survive beyond the chat
- a worker must continue after the first process exits
- several agent calls need stable identity and fan-in
- a future reviewer needs to know what happened
- side effects must be gated and auditable

Keep prompts and skills for judgment, taste, and reusable procedure. Promote the requirements that keep getting dropped into workflow state.

## The real product claim

Hermes Workflows is not trying to make agents less agentic.

It is trying to make important agent work less slippery.

The agent can still write, inspect, plan, summarize, revise, call tools, and propose changes. The workflow records the obligations around that work: what was requested, what completed, what evidence exists, what is waiting, and what no one is allowed to do yet.

That is the whole point.

The agent had the instruction. The system did not.

Put the requirement in the system.

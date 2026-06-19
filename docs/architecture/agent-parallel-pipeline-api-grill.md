---
layout: page
title: Agent / parallel / pipeline API grill
---

# Agent / parallel / pipeline API grill

Status: implemented on branch `api-agent-parallel-pipeline`
Date: 2026-06-12
Owner: `hermes-workflows` runtime/API
Related issue: [#69](https://github.com/skylarbpayne/hermes-workflows/issues/69)
Companion visual plan: [Agent / parallel / pipeline API visual plan](../plans/2026-06-12-agent-parallel-pipeline-api-visual-plan.html)

## Why this doc exists

The current workflow authoring surface is honest runtime machinery, but it is not good product language:

```python
await low-level handoff plumbing
await low-level external-work plumbing
await ctx.wait_for("signal:...")
```

That API makes workflow authors think about handoff signals, waits, keys, and context plumbing. The public model we actually want is closer to Claude Dynamic Workflows / OpenCode-style orchestration:

```python
research = await agent("research", prompt="...", returns=ResearchPacket)
sections = await parallel([...], limit=4)
draft = await pipeline(
    sections,
    humanize,
    evidence_check,
    lambda section: ask(
        "Review section",
        key=f"review_section_{section.id}",
        input=section,
        returns=ReviewDecision,
    ),
)
```

This doc is the grill session: shared language, uncomfortable questions, proposed answers, and implementation pressure points. It is deliberately in the repo so the language survives chat context and issue drift.

## Source models we are copying, not worshipping

### Claude Dynamic Workflows

Useful ideas:

- A workflow is a script/harness that coordinates many subagents.
- The script holds loops, branching, and intermediate results; the chat context gets the final result.
- Workflows are useful when work needs parallelism, independent contexts, cross-checking, tournaments, verification, or long-running coordination.
- Agent work is inspectable as agent/subagent work, not hidden in a blob.
- The workflow is readable and rerunnable.

Non-goals for Hermes:

- Do not switch Hermes Workflows to JavaScript just because Claude uses JavaScript.
- Do not treat “dynamic” as “agent writes arbitrary code and we pray.” Generated workflow code still needs review/gates when it can execute.
- Do not hide durable state. Hermes Workflows is explicitly a durable ledger/runtime, not only a session-local harness.

### OpenCode / “omnicode”-style workflow API

Useful shape observed from public OpenCode dynamic-workflow work:

- `agent(...)` runs subagent/session work under the workflow run.
- `agent(...)` takes a prompt. The prompt is the agent's instruction contract, not an optional afterthought.
- `parallel(...)` runs many independent tasks under a shared run scope.
- `pipeline(...)` runs staged transformations over items.
- Options belong on the agent call: tools, skills, files, model/variant, worktree/isolation, budget.
- Workflow phases/logs/budget/progress are runtime metadata, not business entities.

Non-goals for Hermes:

- Do not make our public API `ctx.agent(...)` if we can avoid visible `ctx` for normal authoring.
- Do not split the API rehaul into a timid docs-only or alias-only sequence. The first implementation PR should include the surface as a coherent unit: `agent`, `parallel`, `pipeline`, and approvals.
- Do not make `parallel`/`pipeline` just UI labels. They need durable semantics and inspectable fan-out/fan-in.

## Shared language

| Term | Meaning | Public? | Notes |
| --- | --- | --- | --- |
| Workflow | A Python orchestration harness with durable replay | Yes | It coordinates work; it is not a static DAG file. |
| Agent call | A durable step completed by an agent/subagent/session runner | Yes | Public primitive: `agent(...)`. |
| Parallel block | Fan-out/fan-in over independent calls | Yes | Public primitive: `parallel(...)`. |
| Pipeline | Staged transformation over one or many items | Yes | Public primitive: `pipeline(...)`. |
| Human input | Human/operator-completed step | Yes | Public primitive: `ask(...)`. |
| Step | A durable unit in the run graph | Yes | Agent calls and approvals are step completion modes. |
| Phase | Human-readable progress label/grouping | Maybe | Useful in UI, but not a substitute for steps. |
| Signal | Runtime wake-up fact | No | Internal plumbing. Do not put in docs/examples unless debugging internals. |
| Wait | Runtime replay suspension | No | Internal plumbing. |
| Handoff | Old runtime vocabulary for externally completed work | No | Retire from normal authoring. |
| External | Vague alias over handoff | No | Also retire from normal authoring. |
| Outbox command | Worker leaseable runtime command | No | Internal runtime mechanism. |
| Context / `ctx` | Runtime handle injected into decider execution | Advanced only | Keep available internally, but normal workflow code should not start here. |

## Banned or demoted words

Banned from normal author-facing docs/examples:

- `handoff`
- `external`
- `signal`
- `wait key`
- `outbox`
- `lease`
- `ctx.wait_for`

Allowed in advanced/runtime docs:

- command lease
- signal record
- waiting state
- outbox row
- runtime context

The product sentence should be:

> “This workflow runs agents in parallel, pipes their outputs through review stages, and stops for approval before side effects.”

Not:

> “This workflow emits handoff commands and waits on agent.completed signals.”

## Target authoring API

### Minimal blog workflow sketch

```python
from dataclasses import dataclass

from hermes_workflows import agent, ask, parallel, pipeline, workflow


@dataclass(frozen=True)
class ResearchPacket:
    claims: list[str]
    sources: list[str]
    open_questions: list[str]


@dataclass(frozen=True)
class SectionDraft:
    slug: str
    title: str
    body_md: str


def research_topic(topic: str):
    return agent(
        "research",
        prompt=f"""Research this topic and return claims, sources, and open questions.

Topic: {topic}
""",
        input={"topic": topic},
        skills=["deep-research"],
        returns=ResearchPacket,
    )


@workflow(name="blog-post")
async def blog_post(topic: str) -> str:
    research = await research_topic(topic)

    angles = await agent(
        "angle_options",
        prompt="Generate 5 strong blog angles from this research.",
        input=research,
        returns=list[str],
    )

    angle = await ask(
        "Choose angle",
        key="choose_angle",
        input=angles,
        returns=str,
    )

    outline = await agent(
        "outline",
        prompt="Draft a concrete section outline from the selected angle and research.",
        input={"angle": angle, "research": research},
        returns=list[str],
    )

    outline_review = await ask("Review outline", key="review_outline", input=outline, returns=ReviewDecision)

    section_drafts = await parallel(
        [
            agent(
                f"draft_section_{index}",
                prompt="Draft this section using the approved outline and research.",
                input={"section": section, "outline": outline, "research": research},
                returns=SectionDraft,
            )
            for index, section in enumerate(outline)
        ],
        limit=4,
    )

    approved_sections = await pipeline(
        section_drafts,
        agent("humanize_section", prompt="Make the section sound like Skylar.", returns=SectionDraft),
        agent("evidence_check_section", prompt="Verify claims and sources.", returns=SectionDraft),
        lambda section: ask("Review section", key=f"review_section_{section.id}", input=section, returns=ReviewDecision),
        limit=4,
    )

    final = await agent(
        "assemble_final_draft",
        prompt="Assemble approved sections into one coherent Markdown draft.",
        input=approved_sections,
        returns=str,
    )

    return final
```

This is not final syntax. It is the smell test. If the real API cannot express this cleanly, the API is not good enough.

### Prompt builders / higher-order steps

`agent(...)` should always have a prompt, but workflow authors should not have to inline every prompt at the orchestration site. Higher-order helpers should be ordinary Python functions that format prompts from typed inputs and return call objects:

```python
def draft_section(section: SectionPlan, research: ResearchPacket):
    return agent(
        "draft_section",
        prompt=render_prompt(
            "prompts/draft_section.md",
            section=section,
            research=research,
        ),
        input={"section": section, "research": research},
        key_by=section.slug,
        returns=SectionDraft,
    )

section_drafts = await parallel([
    draft_section(section, research)
    for section in outline.sections
])
```

This gives us three layers without ceremony:

1. **Typed domain inputs** — dataclasses/Pydantic-ish objects such as `SectionPlan`.
2. **Prompt construction** — ordinary Python functions/templates that render the instruction.
3. **Durable agent call** — `agent(name, prompt, input, returns, ...)` records and memoizes the work.

## Primitive contracts

### `agent(...)`

A durable agent/subagent call. The prompt is required. `agent(...)` without a prompt is too implicit for workflow code.

```python
result = await agent(
    name: str,
    prompt: str | Prompt,
    *,
    input: Any = None,
    context: ContextSpec | Sequence[ContextSpec] | None = None,
    returns: type[T] | Schema[T] | None = None,
    key_by: str | None = None,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    files: list[str] | None = None,
    model: str | None = None,
    variant: str | None = None,
    isolation: Literal["none", "workspace", "worktree"] = "workspace",
    timeout: int | None = None,
    budget: float | None = None,
) -> T
```

Required semantics:

- Records a public step named `name`, scoped by lexical parent and optional `key_by`.
- Requires a prompt, either inline or rendered by a higher-order helper.
- Dispatches through configured Hermes Agent / subagent / subprocess runner adapter.
- Stores the rendered prompt, structured input snapshot, output schema, result, artifacts, logs, provenance, model/variant, tool/skill selection, cost metadata, and runner identity.
- Replays from the durable ledger without re-running the agent.
- Returns typed output after first completion and after replay.
- Fails closed on missing/invalid structured output when `returns` is supplied.

Grill questions:

1. **Is `agent` too model-specific?**
   Maybe, but it is the right common noun for this product. If the implementation runs a tool worker or human work surface later, that is a completion mode under the hood. Normal authors need the orchestration noun.

2. **Should there also be `task`?**
   Not in v1. `task` is generic enough to become meaningless. Use `agent` for intelligent/subagent work, `step` for local deterministic Python, and `approve` for human gates.

3. **Does `agent` imply LLM-only?**
   It should not. It means “agent/session runner boundary.” A deterministic subprocess runner can satisfy it in tests.

4. **Where do tool permissions live?**
   On the call (`tools`, `skills`, `files`, `isolation`) and in the configured runner policy. The runtime records what was requested and what was actually used if the runner reports it.

5. **Can agents edit files?**
   Yes, only through a declared isolation/capability model. For coding work, `isolation="worktree"` should become the sane default.

### `parallel(...)`

A durable fan-out/fan-in helper.

```python
results = await parallel(
    calls: Iterable[Awaitable[T] | AgentCall[T] | StepCall[T]],
    *,
    limit: int | None = None,
    fail: Literal["fast", "collect", "null"] = "fast",
) -> list[T]
```

Required semantics:

- Starts all missing child calls before waiting where possible.
- Uses existing outbox/worker command machinery rather than inline polling.
- Preserves input order by default.
- Supports keyed/dict results as a later ergonomic layer.
- Bounds concurrency when `limit` is set.
- Has inspectable fan-out/fan-in topology in the run DAG.

Grill questions:

1. **Is this just `ctx.gather` renamed?**
   No. `ctx.gather` is runtime-ish and currently narrow. `parallel` is the public fan-out primitive and must support agent calls, steps, and maybe child workflows.

2. **What happens on one failure?**
   Default should be `fail="fast"` for safety. `collect` can return per-item success/failure envelopes. `null` is useful for large best-effort research but should be explicit because silent drops can hide rot.

3. **Can parallel agents mutate the same files?**
   Only if the author accepts that risk. Coding defaults should isolate worktrees or partition paths.

4. **What does the dashboard show?**
   A fan-out/fan-in structure: one block/group with child steps, not a fake sequence caused by event ordering.

### `pipeline(...)`

A staged transformation helper.

```python
outputs = await pipeline(
    items: Iterable[T],
    *stages: Stage,
    limit: int | None = None,
    fail: Literal["fast", "collect", "null"] = "fast",
) -> list[U]
```

A stage can be:

```python
agent("humanize_section", prompt="...", returns=SectionDraft)
lambda section: ask("Review section", key=f"review_section_{section.id}", input=section, returns=ReviewDecision)
step(normalize_section)
lambda item: ...
```

Required semantics:

- Applies stage 1 to every item, then stage 2 to every stage-1 result, etc.
- Can run items within a stage concurrently.
- Records stage progress and per-item results.
- Gives each item stable identity via default index or explicit `key_by` later.
- Makes pipeline topology inspectable: items and stages should not collapse into a wall of unrelated events.

Grill questions:

1. **Is `pipeline` overkill?**
   No. It captures a real recurring shape: draft section → humanize → verify → approve; migrate file → test → review; classify ticket → severity check → route.

2. **Should pipeline stages be sequential per item or barriered by stage?**
   Default should be stage barriers because it is easier to inspect and resume. Later we can allow streaming mode if needed.

3. **How do human/review inputs inside a pipeline work?**
   `lambda section: ask("Review section", key=f"review_section_{section.id}", input=section, returns=ReviewDecision)` inside a pipeline should create per-item Review Queue requests with stable item-derived keys, but publicly still read as `review_section/<item>`.

4. **What does rejection do?**
   For `ask(..., returns=ReviewDecision)`, rejection/edit feedback feeds the prior stage or configured revision stage. It should not terminate the whole workflow unless the author chooses that.

### `ask(...)`

`ask(...)` is the general typed human/external-feedback primitive. The product surface is the Review Queue; approval is one schema/preset over the same request model.

```python
decision = await ask("Choose an angle", key="choose_angle", input=angles, returns=SelectedAngle)
outline_review = await ask("Review outline", key="review_outline", input=outline, returns=ReviewDecision)
```

Required semantics:

- Human-input requests and approval gates appear in one Review Queue: what needs attention.
- The requested schema drives the input surface: a dataclass response with an `action: Literal[...]` field renders those exact action choices; structured outputs render forms or structured-entry fallbacks.
- Raw approval/signal/wait plumbing stays private.
- Provenance is recorded: actor, channel/source, message/event handle, timestamp, idempotency key, and submitted value.
- `ask(...)` composes inside `parallel(...)` and `pipeline(...)`: all ready cards emit before waiting, each with its own key/artifact/schema/provenance.
- Approval attempts are lifecycle/provenance of one public review step unless there is a good reason to split them.

Grill questions:

1. **Is approval a kind of agent?**
   No. It is a step completion mode. Treating humans as agents muddies safety language.

2. **Can approval be used inside `parallel` and `pipeline`?**
   Yes. That is a core requirement.

3. **Should approval keys be explicit?**
   Public approval names should be explicit and readable. Low-level wait/signal keys should be inferred/internal.

## Current substrate mapping

| Target primitive | Current substrate likely used | Missing piece |
| --- | --- | --- |
| `agent(...)` | `agent(...)` + agent runner + step events | Public helper, typed replay, docs/examples, no visible `ctx`. |
| `parallel(...)` | `ctx.gather`, outbox commands, worker service | Public helper that supports agent calls and inspectable fan-out/fan-in. |
| `pipeline(...)` | repeated steps/gather plus metadata | Stage abstraction, per-item keying, topology/progress events. |
| `ask(...)` | `feedback_loop=True` plus hand-rolled loops | First-class loop semantics and feedback routing. |
| typed return replay | JSON event payloads | Dataclass/Pydantic/schema rehydration based on return contract. |

This means the rehaul is probably not a total rewrite. It is an API layer plus some real runtime gaps: typed replay, parallel agent calls, pipeline metadata, and approval-loop ergonomics.

## Context injection and memoization

This is the part that can quietly ruin the API if we hand-wave it. Agent steps memoize correctly only if the runtime knows what the agent call actually depended on. The call descriptor should have a durable fingerprint roughly like:

```text
step_key = workflow_id + lexical_parent + name + key_by
fingerprint = hash({
  rendered_prompt,
  structured_input_json,
  returns_schema_id,
  tools, skills, files, model, variant, isolation,
  prompt_template_id_or_version,
})
```

Replay rule:

1. Workflow code re-runs and constructs the same `AgentCall` descriptor.
2. If a completed output exists for `step_key` and the fingerprint matches, return the saved typed output.
3. If no output exists, enqueue/dispatch the agent call.
4. If a completed output exists but the fingerprint changed, fail loudly unless the author explicitly requested a new version/attempt/invalidation. Silent re-use with changed prompt/input is poison; silent re-run is also poison.

Inputs should therefore be explicit enough to fingerprint without a second fuzzy `context` channel. If a worker needs repository files, memory, constraints, or other reference material, put that material in the typed `input=` object.

```python
research = await agent(
    "research",
    prompt=research_prompt(topic),
    input={
        "topic": topic,
        "reference_files": repo.files(["README.md", "docs/**/*.md"]),
        "principles": memory_pack("hermes-workflows-api-principles"),
    },
    returns=ResearchPacket,
)
```

Saved agent outputs should store both the result and the concrete request. That gives the dashboard a truthful receipt: “this output came from this prompt and this typed input.”

## Design constraints

1. **Python stays the authoring language.**
   No YAML workflow DSL. No separate compiler ceremony.

2. **No visible `ctx` in normal workflow code.**
   Keep it for advanced/runtime APIs, but examples should teach `agent`, `parallel`, `pipeline`, and approvals.

3. **Dynamic topology is still runtime-derived.**
   `parallel` and `pipeline` help expose intent, but the dashboard still derives concrete topology from run events.

4. **Steps remain the public graph model.**
   `signal:`, `wait:`, `handoff:`, and outbox commands do not become public DAG nodes.

5. **Agent calls must be durable.**
   Re-running a workflow must not re-run completed agents unless explicitly invalidated.

6. **Approvals block side effects.**
   Pretty API cannot weaken approval provenance, terminal-state guards, or idempotency.

7. **Repo/runtime boundary stays clean.**
   The API belongs in `hermes-workflows`; Palmer business workflows live outside the runtime repo and import/register against it.

## The hard questions before implementation

### 1. Are these top-level functions magical globals?

Probably yes, but only in the narrow way `step(...)`/`workflow(...)` are already magical. During workflow execution, they find the ambient runtime context via a context variable.

Why this is acceptable:

- It keeps author code clean.
- It mirrors the way durable workflow libraries often expose orchestration primitives.
- It demotes `ctx` to an implementation handle.

Risk:

- Calling `agent(...)` outside workflow execution needs a clear error.
- Tests need a way to run helpers under a fake/real engine.

### 2. Should `agent(...)` return immediately or only when awaited?

In Python, make it awaitable. This keeps normal async workflow semantics:

```python
research = await agent("research", ...)
```

But `parallel([...])` needs to accept un-awaited calls:

```python
results = await parallel([
    agent("a", ...),
    agent("b", ...),
])
```

So `agent(...)` should return an `AgentCall[T]` awaitable object, not immediately dispatch at construction time.

### 3. How does key inference work without becoming spooky?

Use the explicit `name` as the public semantic key. For loops/fan-out, require stable item identity when index is not enough:

```python
await parallel(
    [agent("draft_section", input=section, key_by=section.slug) for section in sections]
)
```

Do not infer from source line number as the primary identity. Source-line identity rots too easily under edits. Use call name + lexical parent + item key + attempt where needed.

### 4. Do we need `step(...)` as a public peer?

Yes, but it is not the API rehaul headline.

- `agent(...)` = intelligent/session work with an explicit prompt
- prompt builders = ordinary Python helpers that format prompts from typed inputs and return call objects
- `step(...)` = local deterministic Python work
- `approve(...)` = human gate
- `parallel(...)` = fan-out/fan-in composition
- `pipeline(...)` = staged composition

### 5. How does this relate to `goal(...)`?

`goal(...)` is not the base primitive. It is a reusable higher-level pattern: agent loop → judge → continue until done/max turns.

Do not make every agent call a goal. Most workflow stages need one bounded agent output with a typed contract.

### 6. What is the first PR slice?

One honest PR, not a timid breadcrumb trail. The API rehaul deserves one coherent implementation PR containing:

1. Top-level `agent(...)` with required `prompt`, typed `input`, optional explicit `context`, `returns`, and replay-safe memoization.
2. Higher-order prompt-builder examples: ordinary Python functions/templates that return `AgentCall[T]` objects from structured inputs.
3. Top-level `ask(...)` wrappers over the existing approval substrate, with loop semantics.
4. Top-level `parallel(...)` over existing gather/outbox semantics for agent calls and steps.
5. First-pass `pipeline(...)` over parallel/stage metadata. It can be minimal, but it needs to exist in the same PR so the public model lands whole.
6. Typed replay/rehydration for saved outputs, at least for dataclasses and plain JSON-compatible returns.
7. Context/fingerprint checks so changed prompt/input/context does not silently reuse stale saved outputs.
8. Docs/examples showing no `low-level handoff plumbing` / `low-level external-work plumbing`.

If that is big, fine. The PR can be reviewable by keeping the implementation boring and test-led. Splitting the public language across several PRs is how we end up with a half-rebrand and no shared model.

### 7. What would make this API dishonest?

- If `agent(...)` can run without a real prompt.
- If higher-order prompt builders are awkward or not documented.
- If changed prompt/input/context silently reuses a stale saved output.
- If `parallel(...)` is still serial under the hood with no clear worker story and no documented limitation.
- If `agent(...)` returns untyped dict blobs despite `returns=...`.
- If the dashboard cannot show the fan-out/fan-in shape.
- If approval loops are still hand-rolled in every workflow.
- If examples still teach `low-level handoff plumbing` or `low-level external-work plumbing`.
- If issue/docs say “agent/parallel/pipeline” but runtime only ships aliases.

## Acceptance tests for the rehaul

### Authoring smell test

A contributor can write the blog workflow skeleton using only:

- `@workflow`
- `agent`
- `parallel`
- `pipeline`
- `ask`
- normal Python/dataclasses

No normal example uses:

- `low-level handoff plumbing`
- `low-level external-work plumbing`
- raw signal names
- raw wait keys
- `cast(...)` for workflow-owned values
- dict indexing for typed workflow-owned values

### Runtime replay test

Given:

```python
research = await agent(
    "research",
    prompt=research_prompt(topic),
    input={"topic": topic, "reference_files": repo.files(["docs/**/*.md"])},
    returns=ResearchPacket,
)
```

When the workflow replays after the agent result completed, `research` is a `ResearchPacket`, not a `dict`, and the runner is not called again. If the rendered prompt, structured input, or return schema changes for the same key, the runtime fails loudly or requires explicit invalidation/versioning.

### Parallel topology test

Given three agent calls in `parallel([...])`, the workflow records three child step requests before waiting and dashboard DAG shows fan-out/fan-in, not a fake linear chain.

### Pipeline topology test

Given three items and two stages, the runtime records stage/item progress in a way the dashboard can render as staged work, and replay can resume after a mid-pipeline interruption without re-running completed item stages.

### Ask feedback-loop test

Given `ask("Review outline", key="review_outline", input=outline, returns=ReviewDecision)`, rejection feedback can feed a normal Python revision loop without a special approval-loop primitive.

### Cutover test

Pre-release compatibility is intentionally removed: normal authoring uses `agent(...)`, `ask(...)`, `parallel(...)`, and `pipeline(...)`; lower-level runtime hooks stay private/advanced only where the engine itself needs them.

## Proposed docs changes once implemented

- Update README quickstart to use `agent(...)` / `approve(...)`, not `ctx`.
- Update architecture docs to call `agent(...)` substrate/internal rather than primary author API.
- Add an `examples/agent_parallel_pipeline_blog.py` smoke workflow.
- Update dashboard runtime semantics doc to explain fan-out/fan-in and pipeline stage rendering.
- Update issue #69 title/body to name the real target API.

## Decision log

- **Rejected:** `low-level handoff plumbing` as author API. Too much runtime plumbing.
- **Rejected:** `low-level external-work plumbing` as author API. Vague rename, same smell.
- **Rejected for v1:** YAML/IR/compiler authoring. Wrong product direction.
- **Accepted direction:** Python workflow harness with `agent`, `parallel`, `pipeline`, approvals, typed returns, and runtime-derived inspectability.
- **Open:** exact typed-model mechanism: dataclass annotations, `returns=...`, Pydantic-like base, or all of the above.
- **Accepted:** the API rehaul should land as one coherent PR containing `agent`, `parallel`, `pipeline`, approvals, typed replay, and context/fingerprint semantics.
- **Open:** exact fingerprint mismatch policy names (`version`, `invalidate`, `rerun_when`, etc.).
- **Open:** stage key syntax for pipeline item identity.

## Implementation status

Implemented in the one-PR branch `api-agent-parallel-pipeline`:

- Top-level `agent(...)` with required `prompt`, typed `input`, explicit `context`, `returns`, `key_by`, and runner metadata.
- `AgentCall[T]` awaitables so authors can write `await agent(...)` or hand calls to `parallel(...)`.
- Top-level `parallel(...)` over the existing durable step/outbox substrate; it enqueues every missing call before waiting.
- Top-level `pipeline(...)` for staged transformation over item lists.
- `ask(...)` helpers over existing approval signals.
- Workflow-context binding so normal author code can omit visible `ctx`; `agent(...)`, `ask(...)`, `parallel(...)`, and `pipeline(...)` are the taught surface.
- Replay safety via request fingerprints covering rendered prompt, structured input, context hashes, return schema, and runner-relevant options.

Verification on this branch: `271 passed, 2 skipped`.

## One-sentence product doctrine

Hermes Workflows should let authors write durable Python orchestration harnesses that coordinate agents, parallel fan-out, staged pipelines, and human approvals — while the ugly machinery of waits, signals, handoffs, leases, and replay stays inside the runtime.

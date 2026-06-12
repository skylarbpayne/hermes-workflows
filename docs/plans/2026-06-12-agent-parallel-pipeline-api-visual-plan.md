---
layout: page
title: Agent / parallel / pipeline API visual plan
---

# Agent / parallel / pipeline API visual plan

Status: implemented on branch `api-agent-parallel-pipeline`
Date: 2026-06-12
Companion grill doc: [Agent / parallel / pipeline API grill](../architecture/agent-parallel-pipeline-api-grill.html)
Related issue: [#69](https://github.com/skylarbpayne/hermes-workflows/issues/69)

## The picture in one sentence

Make workflow authoring look like a durable Python harness that coordinates **agents**, **parallel fan-out**, **pipelines**, and **human approvals**; keep waits, signals, handoffs, leases, replay, and outbox machinery below the floorboards.

## Implementation status

Implemented on branch `api-agent-parallel-pipeline` in one coherent PR-sized change, not split across timid fragments.

Implemented surface:

```python
research = await agent("research", prompt="Research typed workflows", input=brief, context=[...], returns=ResearchPacket)
sections = await parallel([agent("draft_section", prompt=f"Draft {s}", input=s, key_by=s.slug, returns=SectionDraft) for s in sections])
final_sections = await pipeline(sections, humanize_section, evidence_check_section, limit=4)
await approve_until("approve_final", draft, prompt="Approve final draft")
```

Durability rule: saved outputs replay only when the stored request fingerprint still matches the current rendered prompt, input, context hashes, return schema, and runner options.

Verification on this branch: `271 passed, 2 skipped`.

```mermaid
flowchart TB
    subgraph Author["Author-facing workflow language"]
        W["@workflow async def blog_post(...)"]
        A["agent('research')"]
        P["parallel([...], limit=4)"]
        L["pipeline(items, stages...)"]
        H["approve / approve_until"]
        S["step(local_python)"]
        W --> A
        W --> P
        W --> L
        W --> H
        W --> S
    end

    subgraph PublicGraph["Public run graph"]
        G1["agent step"]
        G2["fan-out block"]
        G3["pipeline stage"]
        G4["approval gate"]
        G5["local step"]
    end

    subgraph Runtime["Runtime substrate — not normal author language"]
        R1["event history"]
        R2["memoized replay"]
        R3["outbox commands"]
        R4["worker leases"]
        R5["signals / waits"]
        R6["approval provenance"]
    end

    A --> G1
    P --> G2
    L --> G3
    H --> G4
    S --> G5

    G1 --> R1
    G2 --> R1
    G3 --> R1
    G4 --> R6
    G5 --> R1
    R1 --> R2
    R3 --> R4
    R5 --> R2

    classDef author fill:#e0f2fe,stroke:#0284c7,color:#0f172a
    classDef graph fill:#dcfce7,stroke:#16a34a,color:#0f172a
    classDef runtime fill:#fee2e2,stroke:#dc2626,color:#0f172a
    class W,A,P,L,H,S author
    class G1,G2,G3,G4,G5 graph
    class R1,R2,R3,R4,R5,R6 runtime
```

## Visual vocabulary

Use these words in docs, code comments, dashboard labels, and issue titles.

```mermaid
mindmap
  root((Hermes Workflow))
    prompt builders
      typed inputs
      rendered prompts
      returns AgentCall
    agent
      required prompt
      subagent/session runner
      typed output
      durable replay
      provenance
    parallel
      fan-out
      fan-in
      concurrency limit
      failure policy
    pipeline
      staged work
      item identity
      per-stage progress
      replay-safe resume
    approval
      human gate
      feedback loop
      provenance
      side-effect boundary
    step
      deterministic local Python
      memoized
      typed return
    advanced internals
      ctx
      signal
      wait
      outbox
      lease
```

## Blog workflow topology we want authors to see

```mermaid
flowchart LR
    Start([topic]) --> Research["agent: research"]
    Research --> Angles["agent: angle_options"]
    Angles --> Choose{"approve: choose_angle"}
    Choose --> Outline["agent: outline"]
    Outline --> OutlineApproval{"approve_until: approve_outline"}

    OutlineApproval --> F["parallel: draft sections"]

    subgraph Fanout["fan-out block"]
        S1["agent: draft_section_intro"]
        S2["agent: draft_section_core"]
        S3["agent: draft_section_tradeoffs"]
        S4["agent: draft_section_close"]
    end

    F --> S1
    F --> S2
    F --> S3
    F --> S4

    S1 --> Pipe
    S2 --> Pipe
    S3 --> Pipe
    S4 --> Pipe

    subgraph Pipe["pipeline: section polish"]
        H1["agent: humanize_section"] --> E1["agent: evidence_check_section"] --> A1{"approve_until: approve_section"}
    end

    Pipe --> Assemble["agent: assemble_final_draft"]
    Assemble --> Done([markdown draft])
```

The visible shape is **research → approval → parallel drafting → staged polish → approval → final draft**.

The invisible shape is command rows, signals, and replay checkpoints. Those are diagnostics, not the authoring API.

## Primitive map

| Primitive | Author sees | Runtime records | Dashboard should show |
| --- | --- | --- | --- |
| `agent(...)` | “Run this agent step with this prompt/input/schema.” | Rendered prompt, input snapshot, context digest, output, provenance, artifacts, runner metadata. | One agent step card with prompt/context receipt, logs/artifacts/output. |
| `parallel(...)` | “Run these independent calls together.” | A fan-out/fan-in group plus child step events. | Block with child statuses and concurrency. |
| `pipeline(...)` | “Apply stages to items.” | Stage/item progress, results, failures, resumable keys. | Matrix or swimlane: items × stages. |
| `approve(...)` | “Human picks/accepts/decides.” | Approval request, decision, provenance, idempotency. | Approval card with consequence and source. |
| `approve_until(...)` | “Loop until accepted.” | Attempts, feedback, revision linkage. | Approval gate with attempts/feedback history. |
| `step(...)` | “Run local deterministic Python.” | Step request/result/error. | Plain local step card. |

## Layering plan

```mermaid
flowchart TB
    subgraph Surface["Layer 1: author surface"]
        F1["agent"]
        F2["parallel"]
        F3["pipeline"]
        F4["approve / approve_until"]
        F5["step"]
    end

    subgraph Calls["Layer 2: call objects"]
        C1["AgentCall[T]"]
        C2["StepCall[T]"]
        C3["ApprovalCall[T]"]
        C4["PipelineStage[T,U]"]
    end

    subgraph Engine["Layer 3: runtime engine"]
        E1["WorkflowContext"]
        E2["AgentStep substrate"]
        E3["gather / fan-out"]
        E4["approval client"]
        E5["event replay"]
    end

    subgraph Storage["Layer 4: persisted state"]
        DB[("SQLite history")]
        OC["outbox commands"]
        AR["artifacts / receipts"]
    end

    F1 --> C1 --> E2
    F2 --> C1
    F2 --> C2
    F2 --> E3
    F3 --> C4 --> E3
    F4 --> C3 --> E4
    F5 --> C2 --> E1
    E2 --> DB
    E3 --> DB
    E4 --> DB
    E5 --> DB
    E1 --> OC
    E2 --> AR
```

Key design decision: top-level functions create **awaitable call objects**. Awaiting a call executes through the ambient workflow runtime. `parallel(...)` and `pipeline(...)` can accept not-yet-awaited calls and launch them with durable fan-out semantics. `agent(...)` requires a prompt; higher-order prompt-builder helpers are just normal Python functions that format prompts from structured inputs and return `AgentCall[T]` objects.

## Context + memoization shape

```mermaid
flowchart LR
    I["typed input"] --> B["prompt builder"]
    T["template/version"] --> B
    B --> P["rendered prompt"]
    I --> F["fingerprint"]
    P --> F
    C["context bundle refs + hashes"] --> F
    R["returns schema"] --> F
    F --> M{"completed output for step key?"}
    M -->|"match"| O["return saved typed output"]
    M -->|"missing"| Q["enqueue agent work"]
    M -->|"mismatch"| X["fail / require explicit invalidation"]
```

Memoization rule: saved outputs are reused only when the step key and dependency fingerprint match. The fingerprint includes rendered prompt, structured input, context bundle hashes, return schema, and runner-relevant options. Changed context must not silently reuse stale output.

## Lifecycle of one `agent(...)` call

```mermaid
sequenceDiagram
    participant W as Workflow author code
    participant API as agent(...) helper
    participant E as WorkflowEngine
    participant DB as SQLite history
    participant R as Agent runner

    W->>API: await agent("research", returns=ResearchPacket)
    API->>E: request durable agent step
    E->>DB: check completed step by stable key
    alt completed in history
        DB-->>E: stored typed payload
        E-->>API: rehydrated ResearchPacket
        API-->>W: ResearchPacket
    else not completed
        E->>DB: StepRequested + outbox command
        E-->>API: suspend workflow as waiting
        R->>DB: claim command lease
        R->>R: run provider/subagent/session
        R->>DB: StepCompleted(output + provenance)
        W->>E: trusted resume/re-run workflow
        E->>DB: replay completed step
        E-->>API: rehydrated ResearchPacket
        API-->>W: ResearchPacket
    end
```

## Lifecycle of a `parallel(...)` fan-out

```mermaid
sequenceDiagram
    participant W as Workflow code
    participant P as parallel(...)
    participant E as WorkflowEngine
    participant DB as SQLite history
    participant R as Workers/runners

    W->>P: await parallel([agent A, agent B, agent C], limit=2)
    P->>E: register fan-out group
    E->>DB: record missing child A command
    E->>DB: record missing child B command
    E->>DB: record missing child C command
    E-->>W: waiting on fan-out group
    R->>DB: claim A/B up to limit
    R->>DB: StepCompleted A/B
    R->>DB: claim C
    R->>DB: StepCompleted C
    W->>E: resume/replay
    E->>DB: read A/B/C results
    E-->>P: ordered results
    P-->>W: [A, B, C]
```

## Lifecycle of a `pipeline(...)`

```mermaid
flowchart TB
    subgraph Inputs["items"]
        I1["intro"]
        I2["core"]
        I3["tradeoffs"]
    end

    subgraph Stage1["stage 1: humanize"]
        H1["humanize/intro"]
        H2["humanize/core"]
        H3["humanize/tradeoffs"]
    end

    subgraph Stage2["stage 2: evidence check"]
        E1["evidence/intro"]
        E2["evidence/core"]
        E3["evidence/tradeoffs"]
    end

    subgraph Stage3["stage 3: approve until"]
        A1{"approve/intro"}
        A2{"approve/core"}
        A3{"approve/tradeoffs"}
    end

    I1 --> H1 --> E1 --> A1
    I2 --> H2 --> E2 --> A2
    I3 --> H3 --> E3 --> A3

    A1 --> O["approved sections"]
    A2 --> O
    A3 --> O
```

The dashboard can render this as a matrix: rows are items, columns are stages. That is the visual payoff of making `pipeline(...)` first-class instead of hiding it as nested loops.

## Implementation roadmap

```mermaid
gantt
    title One honest API rehaul PR
    dateFormat  YYYY-MM-DD
    axisFormat  %m/%d

    section Language lock
    Repo grill doc                         :done, a1, 2026-06-12, 1d
    Visual artifact                        :active, a2, 2026-06-12, 1d
    Update issue #69                       :a3, after a2, 1d

    section One PR: author surface
    RED tests for top-level helpers         :b1, after a3, 1d
    AgentCall + required prompt             :b2, after b1, 1d
    prompt-builder examples                 :b3, after b2, 1d
    approve / approve_until                 :b4, after b3, 1d
    parallel fan-out/fan-in                 :b5, after b4, 2d
    first-pass pipeline                     :b6, after b5, 2d

    section One PR: replay truth
    typed return rehydration                :c1, after b6, 2d
    context bundle + fingerprint            :c2, after c1, 2d
    mismatch diagnostics / invalidation     :c3, after c2, 1d
    docs + blog workflow smoke              :c4, after c3, 1d
```

This is one implementation PR, not four product-language fragments. If it starts getting scary, shrink internals — not the shared author vocabulary.

## PR shape board

```mermaid
flowchart LR
    P0["Docs + artifact: shared language"] --> P1["One API rehaul PR"]

    P1 --> A["agent(prompt=..., input=..., context=..., returns=...)"]
    P1 --> B["prompt builders return AgentCall[T]"]
    P1 --> C["parallel([...])"]
    P1 --> D["pipeline(items, stages...)"]
    P1 --> E["approve / approve_until"]
    P1 --> F["typed replay"]
    P1 --> G["context fingerprint guard"]

    A --> T1["No visible ctx in happy-path examples"]
    C --> T2["Fan-out starts missing work before waiting"]
    D --> T3["Items × stages visible and resumable"]
    F --> T4["returns=Dataclass rehydrates as Dataclass"]
    G --> T5["Changed prompt/context cannot silently reuse output"]
```

## File touch map

```mermaid
flowchart TB
    subgraph API["Public API"]
        Init["src/hermes_workflows/__init__.py"]
        Author["src/hermes_workflows/authoring.py"]
    end

    subgraph Runtime["Runtime internals"]
        Engine["src/hermes_workflows/engine.py"]
        Values["src/hermes_workflows/workflow_values.py"]
        Runners["src/hermes_workflows/runners.py"]
        Status["src/hermes_workflows/status.py / dashboard APIs"]
    end

    subgraph Tests["Tests"]
        APItests["tests/test_authoring_api.py"]
        ParallelTests["tests/test_parallel_authoring.py"]
        PipelineTests["tests/test_pipeline_authoring.py"]
        TypedTests["tests/test_typed_replay.py"]
    end

    subgraph Docs["Docs/examples"]
        Readme["README.md"]
        Grill["docs/architecture/agent-parallel-pipeline-api-grill.md"]
        Visual["docs/plans/2026-06-12-agent-parallel-pipeline-api-visual-plan.md"]
        Example["examples/agent_parallel_pipeline_blog.py"]
    end

    Init --> Author
    Author --> Engine
    Author --> Values
    Author --> Runners
    Author --> Status
    APItests --> Author
    ParallelTests --> Engine
    PipelineTests --> Status
    TypedTests --> Values
    Readme --> Author
    Example --> Author
```

## Decision checkpoints

```mermaid
flowchart TD
    D0{"Can a normal author write the blog workflow without ctx?"}
    D0 -- no --> FixSurface["Fix authoring surface before runtime polish"]
    D0 -- yes --> D1{"Do agent calls replay as typed values?"}
    D1 -- no --> Typed["Prioritize typed replay before more demos"]
    D1 -- yes --> D2{"Does parallel show true fan-out/fan-in?"}
    D2 -- no --> Parallel["Fix fan-out semantics/topology"]
    D2 -- yes --> D3{"Does pipeline render as items × stages?"}
    D3 -- no --> Pipe["Add stage/item metadata"]
    D3 -- yes --> D4{"Can approval loops revise without terminating?"}
    D4 -- no --> Approval["Fix approve_until lifecycle"]
    D4 -- yes --> Ship["Promote API in README/examples"]
```

## Design traps to avoid

```mermaid
flowchart LR
    Bad1["Alias ctx.handoff as agent"] --> Lie["Looks nice, still wrong"]
    Bad2["parallel is serial internally"] --> Lie
    Bad3["returns=Dataclass gives dict on replay"] --> Lie
    Bad4["pipeline is just nested loops"] --> Lie
    Bad5["approval loop returns early on rejection"] --> Lie
    Lie --> Stop["Stop. Fix the substrate or document limitation honestly."]
```

## Acceptance snapshot

A first version is good enough when this code is plausible, tested, and documented:

```python
@workflow(name="blog-post")
async def blog_post(topic: str) -> str:
    research = await agent("research", input=topic, returns=ResearchPacket)
    outline = await agent("outline", input=research, returns=Outline)
    outline = await approve_until("approve_outline", outline)

    drafts = await parallel(
        [agent("draft_section", input=s, key_by=s.slug, returns=SectionDraft) for s in outline.sections],
        limit=4,
    )

    sections = await pipeline(
        drafts,
        agent("humanize", returns=SectionDraft),
        agent("evidence_check", returns=SectionDraft),
        approve_until("approve_section"),
        limit=4,
    )

    return await agent("assemble", input=sections, returns=str)
```

If the implementation cannot make this honest, it is not an API rehaul yet. It is lipstick on a runtime API, and we should call it that before it metastasizes.

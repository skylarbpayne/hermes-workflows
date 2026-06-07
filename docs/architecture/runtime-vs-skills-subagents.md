# Runtime vs Skills/Subagents Boundary

Status: accepted
Date: 2026-05-26

## Decision

Keep `hermes-workflows` as a boring durable workflow runtime: a ledger, gate, and status surface.

Do **not** turn it into a smart orchestrator brain. Planning taste, TDD doctrine, milestone review, artifact quality, and model-specific operating prompts belong in skills, Codex `/goal` prompts, and subagent review loops.

The runtime should answer:

- What happened?
- What is waiting?
- Which worker owns which command?
- What already completed and can be memoized on replay?
- Who approved the next side effect, and where is the provenance?
- What evidence should a human review before merge/deploy/landing?

If it starts trying to answer “what is the tasteful plan?” or “how should this agent behave?”, it is probably in the wrong layer.

## Why

A workflow runtime is valuable because it preserves state across crashes, model handoffs, and human pauses. That value comes from boring mechanics:

- append-only-ish event history
- replay from durable history
- pending command outbox
- worker leases and stale-claim protection
- memoized step results
- explicit human approval gates
- inspectable status/list/event surfaces
- durable artifact and PR landing packets

Those mechanics get weaker when the runtime also carries subjective agent judgment. A big custom orchestrator brain would be a worse agent with a database strapped to it. Bad trade.

Skills and subagent loops are the right place for operating taste because they can evolve quickly as the maintainer corrects us:

- implementation-plan quality bars
- TDD and red/green discipline
- spec review before quality review
- stop-on-failure behavior
- artifact design standards
- Codex or Claude prompt skeletons
- repo-specific review checklists

## Ownership boundary

### Workflow runtime owns

- Durable workflow start/run/signal semantics.
- Event history and replay.
- Step result memoization.
- `ctx.gather(...)` fan-out/fan-in semantics.
- Pending command outbox records.
- Command claiming, worker leases, attempts, and stale-owner safety.
- Human approval gates and approval provenance validation.
- Status/list/events/outbox inspection surfaces.
- Artifact and PR landing packets as durable evidence.
- Fail-closed checks before side effects such as PR creation or merge.

### Skills, Codex, and subagents own

- Planning quality and scope control.
- TDD doctrine and test-first behavior.
- Milestone decomposition.
- Spec-compliance review before code-quality review.
- Code review taste and repo conventions.
- Artifact quality and presentation standards.
- Model/tool-specific operating prompts such as Codex `/goal`.
- Lessons learned from failed runs and the maintainer corrections.

## Design rule

Put a feature in the runtime only when it improves one of these:

1. replay correctness
2. approval provenance
3. lease/worker safety
4. memoization/determinism
5. inspectability/status clarity
6. durable evidence for a human gate

Put it in a skill, prompt template, or subagent loop when it improves one of these:

1. planning judgment
2. implementation taste
3. review strictness
4. artifact polish
5. model-specific instructions
6. operating procedure

## Examples

| Need | Layer | Reason |
| --- | --- | --- |
| “Do not implement before plan approval.” | Runtime gate + skill rule | Runtime enforces the stop; skill makes the plan worth reviewing. |
| “Use TDD for every code change.” | Skill / Codex `/goal` | This is operating discipline, not workflow replay semantics. |
| “Resume after a worker crashes.” | Runtime | Requires durable events, command leases, and memoized step output. |
| “Make the plan artifact concrete enough to approve.” | Skill / artifact template | The quality bar will evolve faster than the runtime should. |
| “Show why this workflow is stuck.” | Runtime | Waiting state, outbox commands, events, claims, and errors must be inspectable. |
| “Review spec compliance before code quality.” | Subagent loop | This is a review process, not a persistence primitive. |
| “Never trust caller-supplied approval metadata.” | Runtime | Approval provenance must fail closed before side effects. |

## Codex `/goal` operating skeleton

Use this kind of prompt when an approved plan moves into implementation:

```text
/goal Implement the approved plan in small milestones.

Rules:
1. Before each milestone, restate the acceptance criteria.
2. Write or update tests first.
3. After each milestone, stop and run a spec-review subagent.
4. Do not continue until spec review passes.
5. Then run quality review.
6. If either review fails, fix and re-review.
7. Keep commits small and include validation evidence.
8. Never merge or deploy without explicit landing approval.
```

That loop should be reusable outside `hermes-workflows`. The workflow runtime should only record the durable gates, commands, outputs, approvals, and landing packets around it.

## Consequences

Good consequences:

- The runtime stays small enough to reason about.
- Approval and replay behavior remain auditable.
- Skills can improve after mistakes without changing runtime code.
- Subagents/Codex can be swapped or upgraded without migrating workflow history.
- Future runtime features have a sharper admission test.

Trade-offs:

- The runtime will not magically manage every agent decision.
- Good outcomes depend on maintaining skill hygiene and review templates.
- Some orchestration remains procedural until it proves it needs durable semantics.

That is fine. Durable state is the scarce thing here; taste belongs closer to the agents doing the work.

## Near-term direction

The next useful slices should follow this order:

1. Keep improving inspectability and provenance where workflows are already durable.
2. Dogfood one Codex/subagent milestone loop using an approved implementation plan.
3. Extract repeated review and artifact lessons into skills/templates before adding more runtime surface area.

Runtime features that fail the design rule should be deferred until the skill/subagent version proves insufficient.

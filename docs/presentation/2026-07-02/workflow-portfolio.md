# July 2 workflow portfolio

Goal: show Hermes Workflows as the product layer between agent work and real-world consequences.

Review map: `docs/presentation/2026-07-02/workflow-mermaid.md`.

Core line: agents do the work; workflows own the state, checks, receipts, and approval gates.

## Portfolio lanes

| Lane | Demo workflow | What it proves | Current state |
| --- | --- | --- | --- |
| Content development | `content-asset-lane` | One content spine becomes blogpost + slide deck + HyperFrames video: brainstorm topics → select topic → research → brainstorm/select angle → outline → per-section draft/humanize/review → combine/humanize → Gemini Nano Banana 2 blog visuals → asset adapters. | Implemented as richer demo example; first gate is `select_content_topic`; blog visual generation now has its own Gemini Nano Banana 2 plan/generation steps. |
| Code writing | `coding-review` | Bash creates worktree; agents implement, run real local validation/deploy checks, capture curl/screenshot evidence, review the diff, then carry the evidence into an approval-gated PR creation step. | Implemented example; validation is now agentic and PR creation has its own gate. |
| Communication intelligence | `email-triage-demo` now, iMessage/other channels next | Personal-infra communication workflows should use unredacted accessible context, extract useful facts about people/projects/commitments into Obsidian/Skyvault proposals, and gate sends/archives/calendar/task mutations. | Existing email demo is the fixture lane; private dogfood needs the no-redaction + Obsidian extraction version. |
| Event planning | `event-planning-demo` | Full planning timeline with due dates: venue sizing/selection, attendee target, promotion plan, direct invite list, comms, waivers, logistics, run-of-show, follow-up; no bookings/sends/spend without a gate. | Implemented deterministic demo example; reaches `approve_event_ops_packet`. |

## Meeting shape

1. **Light presentation** — 5 slides, 5 minutes.
   - The problem: agents are useful but slippery; prompts are not obligations.
   - The model: `agent()` for judgment, `bash()` for deterministic checks, `ask()` for gates.
   - The proof: workflows produce durable events, artifacts, review requests, and side-effect ledgers.
   - The demos: code, content, communication intelligence, event ops.
   - The boundary: approval gates are the product, not decoration.

2. **Live demo script** — 12 minutes.
   - Start with `reviewable-draft` to show the primitive Review Queue flow.
   - Show `coding-review` as the hero technical demo.
   - Show one ops lane: probably communication intelligence if time is tight, `event-planning-demo` if the audience is operations/product-heavy.
   - Close with status/event receipts and side-effect-zero evidence.

3. **Blogpost rewrite** — use the code-writing demo as the spine.
   - Reader pain first: the agent did work, but who owns “done,” “safe,” and “approved”?
   - Then show the workflow: worktree, implementation, real local validation evidence, diff, review, PR gate.
   - Then broaden to content/communication/event lanes as proof it is a general operating pattern, not a coding-only trick.

## What needs polish next

Priority order for July 2:

1. Use `reviewable-draft` + `dynamic-workflow-return` as the live hero path. Do **not** try to live-demo every portfolio lane.
2. If showing the dashboard live, pre-configure a temporary dashboard DB alias for `.hermes/presentation-july2/workflows.sqlite`; otherwise use the CLI/Review Queue transcript in `fallback-packet.md`.
3. Run the coding-review lane only as a backup technical demo if the audience specifically wants code-agent workflow detail.
4. Build the private communication-intelligence workflow after the talk: unredacted accessible comms → extracted people/projects/commitments → Obsidian/Skyvault proposal notes → gated sends/tasks/archives.
5. Request-changes iteration loop remains useful follow-up work, but it should not block July 2 unless the talk explicitly promises live iteration.
6. Rewrite/trim the blogpost only after Skylar approves the hero path and title.

## Side-effect rules for July 2

- No live sends, archives, calendar mutations, bookings, purchases, waiver sends, social posts, video uploads, deploys, commits, pushes, PRs, or merges during the public demo.
- Private dogfood may read accessible personal-infra comms unredacted; the point is to extract useful information into Skyvault/Obsidian, not sanitize it into useless mush.
- Local artifact generation is okay only after Review Queue approval.
- Any external action must be a visible proposed action, not executed behavior.

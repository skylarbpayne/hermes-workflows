# July 2 Hermes Workflows — Mermaid review map

Core line: **agents do the work; workflows own state, checks, receipts, gates, and side-effect boundaries.**

This replaces the editable canvas. Mermaid is the review source of truth for now because it is lower-friction, diffable, and easy to paste back.

## Content workflow — reusable template

```mermaid
flowchart TD
  c1["Brainstorm topics<br/><small>agent → TopicBrainstormPacket</small>"] -->|options| c2{"Select topic<br/><small>ask</small>"}
  c2 -->|selected topic| c3["Research topic<br/><small>agent → ResearchPacket</small>"]
  c3 -->|source notes + receipts| c4["Brainstorm angles<br/><small>agent → AnglePacket</small>"]
  c4 -->|angle options| c5{"Select angle<br/><small>ask</small>"}
  c5 -->|selected angle| c6["Draft outline<br/><small>agent → OutlinePacket</small>"]
  c6 --> c7{"Approve outline<br/><small>ask</small>"}
  c7 -->|approved| c8["Draft section(s)<br/><small>agent, parallel where safe</small>"]
  c8 --> c9["Humanize section(s)<br/><small>agent: scar tissue + evidence</small>"]
  c9 --> c10{"Review section(s)<br/><small>ask; reject loops only affected section</small>"}
  c10 -->|request changes| c8
  c10 -->|approved sections| c11["Combine approved sections<br/><small>agent → CanonicalDraft</small>"]
  c11 --> c12["Humanize full draft<br/><small>agent: transitions, repetition, voice</small>"]
  c12 --> c13{"Approve canonical draft<br/><small>ask</small>"}
  c13 -->|approved spine| c14["Plan blog visuals<br/><small>agent → VisualElementPlan, model: Gemini Nano Banana 2</small>"]
  c14 --> c15{"Approve visual plan<br/><small>ask</small>"}
  c15 -->|approved| c16["Generate blog visuals<br/><small>agent: Gemini Nano Banana 2 → local image files + receipt</small>"]
  c16 --> c17["Format adapters<br/><small>blogpost uses visuals; slide deck + HyperFrames video package</small>"]
  c17 --> c18[["Local asset packet<br/><small>artifact: manifest, visual receipts, paths, render/check receipts</small>"]]
  c18 --> c19{{"Side-effect gate<br/><small>no publish/upload without explicit approval</small>"}}

```

## Code workflow — hero demo

```mermaid
flowchart TD
  k1["Create worktree<br/><small>bash: deterministic repo mechanics</small>"] --> k2["Implement in worktree<br/><small>agent edits files</small>"]
  k2 --> k3["Validate locally<br/><small>agent: local deploy/server + curl/screenshots when applicable</small>"]
  k3 --> k4["Collect git evidence<br/><small>bash: status, untracked files, diff stat, diff tail</small>"]
  k4 --> k5["Review change<br/><small>agent reviews diff + validation evidence</small>"]
  k5 --> k6[["Review artifact<br/><small>changed files, behavior, real validation receipts, risks, full diff</small>"]]
  k6 --> k7{"Approve change?<br/><small>ask</small>"}
  k7 -->|request changes| k2
  k7 -->|approved| k8["Draft PR packet<br/><small>agent carries validation evidence into PR body</small>"]
  k8 --> k9{"Approve create PR?<br/><small>ask</small>"}
  k9 -->|approved| k10["Create PR<br/><small>agent: commit + push + gh pr create; no merge</small>"]
  k9 -->|not approved| k11{{"Stop with local receipts<br/><small>no commit / push / PR</small>"}}

```

## Communication workflow — personal-infra extraction demo

```mermaid
flowchart TD
  e1["Collect accessible comms<br/><small>email now; iMessage/other channels next; no redaction for private ops</small>"] --> e2["Classify + extract<br/><small>agent: people, projects, commitments, useful facts, waiting states</small>"]
  e2 --> e3["Write proposal notes<br/><small>Obsidian/Skyvault: CRM/project/task facts with provenance</small>"]
  e3 --> e4["Draft local actions<br/><small>agent: replies, Kanban tasks, archive candidates</small>"]
  e4 --> e5[["Comms intelligence packet<br/><small>artifact: extracted facts + proposals + non-actions</small>"]]
  e5 --> e6{"Approve actions?<br/><small>ask</small>"}
  e6 -->|request changes| e3
  e6 -->|approved| e7{{"Side-effect gate<br/><small>send / archive / draft creation / task writeback require explicit approval</small>"}}

```

## Event workflow — ops packet demo

```mermaid
flowchart TD
  v1["Gather event brief<br/><small>goals, audience, constraints, dates, budget</small>"] --> v2["Shape event strategy<br/><small>agent: attendee count, venue criteria, promotion channels, invitee segments</small>"]
  v2 --> v3["Build planning timeline<br/><small>agent: due dates / T-minus tasks / dependencies / receipts</small>"]
  v3 --> v4["Draft ops artifacts<br/><small>agent: venue, promotion, direct invites, comms, logistics, run-of-show, budget, waivers, follow-up</small>"]
  v4 --> v5["Assemble ops packet<br/><small>agent: proposed external actions made visible</small>"]
  v5 --> v6[["Event ops packet<br/><small>artifact: venue recommendation, promotion map, invite targets, owners, timeline, risks</small>"]]
  v6 --> v7{"Approve local packet?<br/><small>ask</small>"}
  v7 -->|request changes| v2
  v7 -->|approved| v8{{"Side-effect gate<br/><small>no sends, posts, scheduling, bookings, purchases, waiver requests</small>"}}

```

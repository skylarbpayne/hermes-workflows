# /workflows real-run, open-source, and blog plan

Date: 2026-06-05
Owner: The operator prepares; the maintainer approves real sends/publishing/repo visibility changes.

## Bottom line

Yes, we should run it for real — but the first real run should be a **read-only production dry run** that generates a review packet from real Hack the Valley data and performs **zero external side effects**.

Do **not** jump straight to sending or even creating Gmail drafts. The product story is stronger if the system proves it can do the boring dangerous parts safely:

1. read real sources,
2. join roster -> submissions -> prize/judging status,
3. generate per-participant drafts,
4. run agent QA,
5. stop at human approval,
6. produce receipts/audit artifacts,
7. only then allow draft creation/send behind explicit gates.

That is the blog post: not “agents can write emails,” but “agents can do operational joins, generate workflow code, and stop before the irreversible part.”

## Current state

Repo: `hermes-workflows`
Remote: `<owner>/hermes-workflows`
Current GitHub visibility: private
License: Apache-2.0
Demo artifact: private share link redacted

Latest verified synthetic demo run:

- targeted demo tests: `5 passed`
- workflow agent calls: `7`
- approval gates:
  - `generated_workflow_execution`
  - `agent_email_quality_approval`
  - `human_email_batch_approval`
- audit events: `41`
- live share smoke: no-share `401`, share `200`
- Playwright clicked all three approvals to final cleared state
- full suite is clean after updating the path-redaction expectation: `110 passed, 2 skipped`.

Latest real-data dry run, using private local snapshots and no external side effects:

- registration rows exported from Sheets: `62`
- submissions exported from D1: `16`
- checked-in participant drafts generated: `28`
- unchecked rows skipped by default: `19`
- withdrawn rows skipped: `12`
- duplicate-email rows skipped: `3`
- participants without confident project match: `15`
- prize-specific claims: `0` because no reviewed prize JSON was supplied
- agent calls: `7`
- approvals: `generated_workflow_execution`, `agent_email_quality_approval`, `human_email_batch_approval`
- side effects: `gmail_drafts_created=0`, `emails_sent=0`
- public-safe redacted review packet: `docs/output/hackathon-redacted-packet-2026-06-05/index.html`
- private local review packet: generated under a local `/tmp/workflows-real-run/...` path and not committed

The real run surfaced the right blocker: the workflow can run, but the campaign should not move to Gmail draft creation until unmatched participant/project rows and reviewed prize data are handled.

## Real-run slice

### Inputs to use, read-only

Use the existing Hack the Valley sources as production-like inputs:

- Registration Sheet: `Hack the Valley 2026 Registration (Responses)`
  - known summary: 62 response rows, 59 unique emails, 33 checked in, 12 withdrawn
- Submission database: Cloudflare D1 `hack-the-valley-submissions`
  - known summary: 16 submitted records
- Existing follow-up campaign state:
  - Resend segment imported with 59 deduped contacts
  - Resend broadcast exists as draft, not sent
  - Gmail fallback draft exists as draft, not sent
- Recap page and by-the-numbers asset already live/reviewed.

### Real-run mode

The real-run path accepts explicit input files/snapshots rather than reaching into live systems by default:

```bash
PYTHONPATH=src:. python examples/build_hackathon_email_snapshot.py \
  --registration-csv /path/to/redacted-registration-export.csv \
  --submissions-json /path/to/redacted-submissions-export.json \
  --prizes-json /path/to/prizes-reviewed.json \
  --out /tmp/workflows-real-run/snapshot.json

HERMES_WORKFLOWS_HACKATHON_SNAPSHOT=/tmp/workflows-real-run/snapshot.json \
PYTHONPATH=src:. python examples/workflows_demo_2026_06_05.py \
  --db /tmp/workflows-real-run/workflow.sqlite \
  --id wf_htv_real_snapshot_dry_run \
  --artifact /tmp/workflows-real-run/review-packet/index.html \
  --receipt-json /tmp/workflows-real-run/receipt.json
```

The harness produces:

- `review-packet/index.html` — private artifact UI for the maintainer review
- `receipt.json` — full private workflow receipt; do not commit or paste into chat
- CLI summary — redacted counts, approvals, side effects, and generated workflow hash
- `snapshot.json` — joined private run input
- `workflow.sqlite` — durable run history

### Side-effect policy

Default mode: `--dry-run` and read-only.

Allowed without additional approval:

- read local exported CSV/JSON snapshots
- generate drafts into local/private artifact files
- create a protected artifact review packet
- run agent QA
- create Kanban/Skyvault receipts

Blocked without explicit approval:

- sending email
- scheduling Resend broadcast
- creating Gmail drafts
- importing/modifying contacts
- mutating Sheets/Drive/D1/R2
- publishing repo/blog/site/social
- making repo public

### Approval ladder

1. **Generated workflow approval** — generated Python must be inspectable, hashed, and approved before execution.
2. **Agent QA approval** — agent reviewer must approve roster coverage, prize accuracy, tone, and side-effect safety before human review.
3. **Human draft approval** — the maintainer approves creating drafts or sending/scheduling.
4. **Send approval** — separate explicit send/schedule gate. Draft approval is not send approval.

## Open-source prep

### Must fix before public repo

- Add license. Default recommendation: Apache-2.0 if we want broad commercial usage with patent grant; MIT if we want maximum simplicity.
- Cleanly separate demo/synthetic data from real HTV data.
- Add a public-safe demo README section: deterministic runner, no network/auth needed, exact commands, expected receipts.
- Add `SECURITY.md` with generated-code/approval/sandbox caveat: approval is a gate, not a sandbox.
- Add `CONTRIBUTING.md` with TDD expectations and narrow runtime boundary.
- Add `.gitignore` coverage for SQLite DBs, generated artifacts, local run snapshots, secrets, CSV exports, and share tokens.
- Fix or intentionally document the full-suite path-redaction test failure before calling the repo healthy.
- Decide package identity: `hermes-workflows` as standalone runtime vs experimental subproject under Hermes Agent.
- Create an examples page for:
  - approval request primitive
  - subprocess `agent(...)`
  - dynamic Python workflow returns
  - hackathon participant email workflow

### Nice-to-have before first public announcement

- One polished architecture diagram.
- One short screencast/gif of the approval click-through artifact.
- `pip install -e '.[dev]' && pytest -q` clean from a fresh clone.
- `python examples/workflows_demo_2026_06_05.py ...` command in README.
- Release tag `v0.1.0-alpha` or similar.

## Blog thesis

Working title:

> Agents Should Stop Before the Dangerous Part

Sharper alternate:

> The Interesting Part of Agent Workflows Is the Stop Sign

Core claim:

Most agent demos optimize for magic: give an agent a goal and watch it act. Real operations need the opposite: let agents gather context, generate code, draft outputs, and run checks — then make them stop at explicit approval gates before generated code execution or external side effects.

The Hack the Valley email workflow is a good spine because the danger is obvious:

- wrong recipient list,
- wrong project/prize mapping,
- accidental mass email,
- fake personalization,
- no audit trail,
- no human approval moment.

The post should show the system solving those exact risks, not hand-waving “human in the loop.”

## Blog outline

1. **The failure mode**
   - The easy demo: agent writes and sends follow-up emails.
   - The real problem: who exactly gets what, based on which source, with what approval, and what proof?

2. **The workflow we actually want**
   - roster -> projects -> prizes -> personalized drafts -> generated workflow -> agent QA -> human approval -> draft/send packet.
   - Include the command-center screenshot/artifact link.

3. **Code-first workflows, not YAML swamp**
   - Python is the workflow language.
   - Generated workflows are values with source, symbol, hash, provenance, and approval key.
   - Metadata can be exported, but humans review code.

4. **Agents as bounded workers**
   - `agent(...)` calls out through a subprocess runner.
   - Deterministic demo runner for reliability; same boundary as real provider-backed agents.
   - The runtime records request/response/provenance and fails closed.

5. **Approval gates are product features**
   - generated code approval
   - agent QA approval
   - human side-effect approval
   - separate send gate

6. **Receipts beat vibes**
   - event log, input hashes, generated source hash, approval decisions, side-effect counts.
   - Demo proof: zero Gmail drafts created, zero emails sent.

7. **What this is not**
   - not Zapier cosplay
   - not arbitrary generated code execution as a product
   - not a replacement for judgment
   - not a promise that approval equals sandboxing

8. **What comes next**
   - real dry-run against HTV data
   - package/eval loop
   - open-source alpha
   - real provider-backed agent runner

## Launch order

1. Fix repo hygiene and full-suite blocker.
2. Add real-run dry-run harness using exported snapshots.
3. Run against HTV snapshots and produce protected review packet.
4. Have the maintainer review packet and decide whether to create drafts/send.
5. Add README/license/security/contributing docs.
6. Make repo public only after explicit approval.
7. Publish blog post after the maintainer approves final draft.

## Decision needed from the maintainer

Before the real run, pick the source mode:

- **Snapshot mode** — export Sheets/D1/Resend state into local CSV/JSON and run on snapshots. Safest and best for open-source reproducibility.
- **Live read-only mode** — connect directly to Sheets/D1/Resend read APIs. More impressive, higher auth/blast-radius complexity.

Recommendation: snapshot mode first. It is less sexy and much less dumb.

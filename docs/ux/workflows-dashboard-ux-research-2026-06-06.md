# /workflows UX research and product direction

Date: 2026-06-06
Task: Palmer Kanban `t_9f597139`
Scope: dashboard UX for Hermes Workflows: runnable workflow catalog, run initiation, run status, run history, outputs/artifacts, active approvals, and high-trust single approval review.

## Bottom line

The current `/workflows` tab is an observability card pile. That was fine for proving the plugin path, but it is not a product UX.

The right model is a **workflow operator console** with three primary objects:

1. **Workflow definitions** — things Skylar can run.
2. **Runs** — execution instances with status, logs, events, approvals, and artifacts.
3. **Approvals** — pending human decisions with risk, consequence, evidence, and provenance.

Do not make Skylar start from SQLite DB aliases or raw workflow IDs. Those are backend details. The default screen should answer: **what can I run, what is running, what needs me, and what changed?**

## JTBD mapping

| Job to be done | Recommended UX |
|---|---|
| See workflows I can run | `Workflows` catalog: searchable cards/table grouped by domain, owner, safety class, last run health, required inputs, side effects, and schedule/manual availability. |
| Run a workflow | Workflow detail + schema-driven `Run workflow` drawer: inputs, defaults, dry-run/test option, capability/side-effect preview, approval gates, and expected artifacts. |
| See status of a workflow | Run detail page: status header, step timeline, pending waits/approvals, command state, recent events, logs, elapsed time, and next action. |
| See history of runs | Per-workflow `Runs` tab plus global `Runs` page: filters by status/date/workflow/initiator/version, rerun/replay lineage, duration/failure trends. |
| See outputs/artifacts from a run | Run `Artifacts` tab: rendered Markdown/report previews, links, files, tables/images, receipts, artifact lineage/version history, redaction status. |
| See active approvals needed | `Approvals` queue: `Needs my approval`, `Waiting on others`, `All pending`, `Approved`, `Rejected/expired`; sorted by risk/age/SLA. |
| Great UX on a single approval | Dedicated approval detail page/drawer: exact action, consequence, policy trigger, risk/blast radius, diff/artifact preview, requester/provenance, alternatives, comments, audit timeline, and record-only/resume boundary. |

## What good products do

### 1. They separate definitions from runs

Temporal, Airflow, Dagster, GitHub Actions, n8n, Zapier, and Pipedream all distinguish the reusable workflow/job from an execution instance. This matters because “run email triage” and “run #wf_123 failed on step 4” are different user jobs.

Design call for Hermes:

- `Workflows` = reusable things I can launch.
- `Runs` = every invocation, including manual, scheduled, webhook, agent, and resumed runs.
- `Run detail` = the canonical execution receipt.

### 2. They make filters first-class

Temporal exposes workflow execution filters by status, workflow ID/type, start/end time, and search attributes, plus saved views. Airflow has DAG list filters/search/tags and a run grid. Pipedream and Zapier make status/time/workflow filters central in event/run history.

Design call for Hermes:

- Global filters: status, workflow, domain, initiator, time, approval state, risk, artifact type.
- Saved views later: `Needs approval`, `Failed last 24h`, `Email ops`, `Long-running`, `Agent blocked`.

### 3. They make “run” a guided flow, not a button taped to a row

GitHub Actions manual runs use `workflow_dispatch` with branch and input fields. Prefect deployments expose parameter schemas. n8n distinguishes manual/test execution from active production execution. Pipedream/Retool use trigger-event testing for event-driven workflows.

Design call for Hermes:

`Run workflow` should open a drawer/modal with:

- workflow name + one-sentence purpose
- input schema with labels, descriptions, examples, defaults, required/optional markers
- source/context selectors when relevant
- run mode: `Dry run`, `Run now`, later `Schedule/activate`
- capability/side-effect preview
- approvals that may be requested
- expected outputs/artifacts
- final confirmation only when side effects or costly actions are possible

### 4. They make status visual and drillable

Airflow’s Grid/Graph views show status across recent DAG runs and tasks. GitHub Actions shows a run visualization graph and job/step logs. Temporal exposes event history, pending activities, workers, relationships, queries, and metadata. Pipedream run/event details show steps, configuration, performance, results, and errors.

Design call for Hermes:

Run detail needs three layers:

1. **Header:** status, workflow, version, run ID, started by, elapsed, next action.
2. **Timeline/steps:** running/waiting/failed/completed nodes with human-readable labels.
3. **Inspector:** logs, events, commands, payload previews, diagnostics, artifacts.

Avoid raw JSON as the primary UI. Keep JSON behind `Details` / `Raw`.

### 5. They treat artifacts as first-class outputs

Prefect artifacts can be links, Markdown, progress, images, and tables; keyed artifacts show lineage on a global Artifacts page and inside run/task tabs. GitHub exposes logs and downloadable artifacts from run summary. Temporal shows workflow inputs/results and downloadable event history.

Design call for Hermes:

Artifacts should have:

- title
- type: receipt, Markdown, file, link, table, image, JSON, diff, generated draft
- run/workflow linkage
- created time
- redaction/sensitivity flag
- preview renderer
- open/download/copy actions
- lineage/version key for repeated outputs, e.g. `email-triage-draft`, `morning-rounds-brief`

### 6. Approvals need their own queue

Stripe Approvals, Ramp approvals, Slack app requests, and Linear PR reviews all provide a dedicated “needs review” surface instead of relying only on notifications.

Design call for Hermes:

Add `/workflows/approvals` with tabs:

- `Needs my approval`
- `Waiting on others`
- `All pending`
- `Approved`
- `Rejected / expired`

Each row should show:

- risk/severity
- requested action
- target workflow/run
- requester/agent
- policy triggered
- age/SLA/expiration
- consequence preview
- quick actions only for low-risk obvious cases

### 7. The single approval screen is the most important UX

Good approval UX puts consequence, policy, risk, evidence, and provenance in the operator’s face. GitHub deployment approvals explicitly say approval lets the job proceed and access environment secrets; rejection fails the workflow. Stripe says approved actions complete automatically and denied actions are discarded. GitHub bypass requires selecting environments, commenting, and clicking “I understand the consequences.”

Design call for Hermes:

A single approval view should use this layout:

#### Sticky header

- `Approve: <specific action>`
- status + risk level
- consequence sentence: “Approving records this decision only; trusted local resume is still required.” or “Approving will send X.”
- expiration/SLA
- primary actions: Approve, Reject, Request changes / Ask for more info

#### Main panel

1. **What is being approved** — structured object, not prose.
2. **Artifact / diff preview** — draft email, generated code, payment payload, config diff, etc.
3. **Checks and evidence** — validations, known failures, missing context, confidence.
4. **Discussion / notes** — required reason for reject/bypass/high-risk.
5. **Raw details** — collapsed JSON/event payload.

#### Right sidebar

- policy triggered and matched conditions
- required approver(s)
- requester / agent / source channel
- affected resources and blast radius
- rollback/cancel availability
- sensitive data touched
- related runs/issues/artifacts

#### Bottom timeline

- request created
- policy matched
- notifications sent
- comments
- decision recorded
- resume attempted / not attempted
- final outcome

## Proposed information architecture

```text
/workflows
  Overview
    - run catalog highlights
    - active runs
    - approvals needing me
    - recent failures
    - recent artifacts

  Workflows
    - catalog of runnable workflow definitions
    - workflow detail
      - overview
      - run form
      - run history
      - expected inputs/outputs
      - approval policy
      - code/source/provenance

  Runs
    - global run history
    - run detail
      - status/timeline
      - steps/commands
      - logs/events
      - approvals
      - artifacts
      - raw receipt

  Approvals
    - active approval queue
    - approval detail

  Artifacts
    - global artifact library
    - artifact detail/lineage

  Settings / Diagnostics
    - DB aliases, workers, plugin health, retention, redaction
```

## MVP implementation sequence

### Phase 1 — Product IA over current data

Goal: make the existing plugin stop feeling like a debug dump.

- Split current page into `Overview`, `Runs`, `Approvals` sections.
- Rename “workflow card” to “run card” unless it is a definition.
- Add status/risk/action summary at top.
- Add approval queue extracted from current run payloads.
- Add a dedicated approval detail drawer.
- Keep all actions record-only.

Acceptance:

- Skylar can open `/workflows` and immediately see pending approvals without reading workflow IDs.
- One approval opens into a detail view with consequence, policy, artifact preview, and provenance.

### Phase 2 — Add runnable workflow catalog

Goal: satisfy “see workflows I can run” and “run a workflow.”

Backend needs a registry endpoint:

```http
GET /api/plugins/hermes-workflows-approvals/definitions
GET /api/plugins/hermes-workflows-approvals/definitions/{name}
POST /api/plugins/hermes-workflows-approvals/runs
```

Definition payload should include:

```json
{
  "id": "email-triage-dry-run",
  "name": "Email triage dry run",
  "description": "Find action-bearing email without sending or archiving.",
  "owner": "palmer",
  "domain": "email",
  "version": "0.1.0",
  "run_modes": ["dry_run", "manual"],
  "input_schema": {},
  "capabilities": {"reads": [], "writes": [], "external_side_effects": []},
  "approval_policy": [],
  "expected_artifacts": []
}
```

Run creation should emit a durable receipt immediately, even if execution is async.

### Phase 3 — Real artifacts and history

Goal: make outputs inspectable.

- Add artifact table/API if not already canonical.
- Link artifacts to run ID + workflow definition + version.
- Render Markdown, links, tables, files, JSON receipts, generated drafts, diffs.
- Add global artifacts page and run-level artifacts tab.

### Phase 4 — Great approval UX

Goal: make approvals trustworthy enough for real side effects later.

- Approval detail route: `/workflows/approvals/:approval_id`.
- Structured approval object schema.
- Policy/risk/consequence panels.
- Diff/artifact preview renderers.
- Request changes / ask question path.
- Bypass as separate high-risk action if ever allowed.
- Explicit record-only vs resume semantics.

## Visual direction

Use a **Linear / Vercel / Stripe** posture:

- fast, dense, quiet UI
- table/list-first, not giant cards everywhere
- one accent color for action/risk
- strong command/search affordances
- plain English consequence copy
- monospace only for IDs, code, receipts
- JSON collapsed by default

Avoid the “developer demo dashboard” look: giant status cards, raw IDs as headings, huge pre blocks, and approval buttons detached from context.

## Source log

1. Temporal Web UI docs — workflow execution list, filters, saved views, execution history, pending activities/workers/metadata.
   https://docs.temporal.io/web-ui
2. Airflow UI docs — DAG list, trigger action, grid/graph views, run history, logs, XComs, code/details.
   https://airflow.apache.org/docs/apache-airflow/stable/ui.html
3. Prefect artifacts docs — link/Markdown/progress/image/table artifacts, keyed lineage, run/task association.
   https://docs.prefect.io/v3/how-to-guides/workflows/artifacts
4. GitHub Actions manual workflow docs — Run workflow button, branch/ref, inputs, CLI/API trigger.
   https://docs.github.com/actions/managing-workflow-runs/manually-running-a-workflow
5. GitHub deployment review docs — approve/reject consequences, environment secrets access, self-approval prevention, bypass confirmation/comment.
   https://docs.github.com/actions/managing-workflow-runs/reviewing-deployments
6. n8n executions docs — manual vs production executions, workflow-level vs all executions, execution data redaction.
   https://docs.n8n.io/workflows/executions/
7. Pipedream Event History docs — centralized event history, filters, detail panel, replay/delete bulk actions.
   https://pipedream.com/docs/workflows/event-history
8. Zapier Zap history docs — run history filters, run detail, version info, replay, retention limits.
   https://help.zapier.com/hc/en-us/articles/8496291148685-View-and-manage-your-Zap-history
9. Stripe Approvals docs — action paused, request created, reviewers approve/deny, approved actions complete, denied changes discarded, approval request metadata.
   https://docs.stripe.com/account/approvals
10. Linear Pull Request Reviews docs — dedicated Reviews section, responsibility grouping, PR details, checks, comments, notifications.
   https://linear.app/docs/pull-request-reviews

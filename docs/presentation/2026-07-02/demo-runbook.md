# July 2 demo runbook

Purpose: run a no-side-effect demo that proves the public model: `run` records/replays, `worker` owns continuation, `ask(...)` produces a typed Review Queue request, and dynamic workflows can generate child runs.

## Preflight

```bash
cd /Users/skylarpayne/code/hermes-workflows
export PYTHONPATH=src:.
git status --short --branch
python -m pip install -e '.[dev]'
python -m pytest -q tests/test_launch_examples.py
```

Expected: launch example tests pass from the current checkout before the demo. Do not trust stale commit notes.

## Demo registry

Use:

```bash
docs/presentation/2026-07-02/workflows.registry.example.json
```

The registry writes demo state to `.hermes/presentation-july2/workflows.sqlite` inside the checkout. Delete that file before a fresh presentation run:

```bash
rm -f .hermes/presentation-july2/workflows.sqlite
```

## Demo 1: reviewable draft reaches Review Queue

```bash
hermes-workflows run reviewable-draft   --config docs/presentation/2026-07-02/workflows.registry.example.json   --project-root .   --db default   --id wf_july2_reviewable_draft   --input-json '{"topic":"Hermes Workflows July 2 demo"}'

hermes-workflows worker   --config docs/presentation/2026-07-02/workflows.registry.example.json   --db default   --worker-id july2-demo-worker   --max-commands 5   --idle-exit-after 0.1

hermes-workflows status   --db .hermes/presentation-july2/workflows.sqlite   --id wf_july2_reviewable_draft   --recent-events 20
```

What to point at:

- `status: waiting`
- `waiting_on: signal:operator.response:review_draft_packet`
- `review_requests[0].request_type: human_input`
- `input_surface.actions: Approve / Request changes`
- artifact title: `Review packet: Hermes Workflows July 2 demo`

Presenter line: the workflow did useful work, then stopped at the exact human decision instead of silently continuing.

## Demo 2: dynamic workflow return completes child workflows

```bash
hermes-workflows run dynamic-workflow-return   --config docs/presentation/2026-07-02/workflows.registry.example.json   --project-root .   --db default   --id wf_july2_dynamic_return   --input-json '{}'

hermes-workflows worker   --config docs/presentation/2026-07-02/workflows.registry.example.json   --db default   --worker-id july2-demo-worker   --max-commands 20   --idle-exit-after 0.1

hermes-workflows status   --db .hermes/presentation-july2/workflows.sqlite   --id wf_july2_dynamic_return   --recent-events 30
```

What to point at:

- `status: completed`
- `generated_workflow.symbol: process_launch_item`
- `generated_workflow.source_sha256` exists
- processed IDs: `dynamic-examples`, `subworkflow-ui`
- event types include `ChildWorkflowRequested` and `ChildWorkflowCompleted`

Presenter line: the workflow can treat generated workflow code as a durable value, then run child workflows with receipts.

## Demo 3: coding-review workflow reaches human gate

```bash
hermes-workflows run coding-review \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --project-root . \
  --db default \
  --id wf_july2_coding_review \
  --input-json '{"repo_path":"/Users/skylarpayne/code/hermes-workflows","base_ref":"HEAD","worktree_path":"/tmp/hermes-workflows-july2-coding-worktree","branch_name":"demo/july2-coding-review","task":"Make a small, reviewable code change; do not commit, push, PR, or merge.","validation_command":"python -m py_compile examples/advanced/coding_review_demo.py && python -m pytest -q tests/test_launch_examples.py"}'

hermes-workflows worker \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --db default \
  --worker-id july2-coding-worker \
  --max-commands 20 \
  --idle-exit-after 0.1 \
  --agent-command "$HERMES_WORKFLOWS_AGENT_COMMAND" \
  --agent-request-stdin json

hermes-workflows status \
  --db .hermes/presentation-july2/workflows.sqlite \
  --id wf_july2_coding_review \
  --recent-events 40
```

What to point at:

- `create_worktree` is bash-only repo mechanics.
- `implement_in_worktree` is agent-owned implementation.
- `validate_locally_with_evidence` is agent-owned validation: local deploy/server where applicable, curl request/response or screenshots, stdout/stderr/exit code.
- diff/status/untracked-file collection is bash evidence.
- `code_review` is agent-owned review of diff + validation evidence.
- `review_worktree_change` gates the change; `approve_create_pr` separately gates commit/push/PR creation. Evidence is carried into the PR draft/body.

## Demo 4: content asset lane reaches editorial gate

```bash
hermes-workflows run content-asset-lane \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --project-root . \
  --db default \
  --id wf_july2_content_assets \
  --input-json '{}'

hermes-workflows worker \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --db default \
  --worker-id july2-content-worker \
  --max-commands 10 \
  --idle-exit-after 0.1

hermes-workflows status \
  --db .hermes/presentation-july2/workflows.sqlite \
  --id wf_july2_content_assets \
  --recent-events 30
```

What to point at:

- workflow keeps one approved content spine, then adapts it into blogpost, slide deck, and HyperFrames video.
- after canonical draft approval, the blog lane plans visual elements and generates local image assets using Gemini Nano Banana 2, with model/prompt/path receipts carried into the content packet.
- first stop is `select_content_topic`; later gates cover angle, outline, sections, canonical draft, asset plan, blog visual plan, and local packet.
- publish/send/upload/merge flags stay false.

## Demo 5: communication intelligence stays no-side-effect

```bash
hermes-workflows run email-triage-demo \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --project-root . \
  --db default \
  --id wf_july2_email_triage \
  --input-json '{"fixture":"synthetic"}'

hermes-workflows worker \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --db default \
  --worker-id july2-email-worker \
  --max-commands 10 \
  --idle-exit-after 0.1

hermes-workflows status \
  --db .hermes/presentation-july2/workflows.sqlite \
  --id wf_july2_email_triage \
  --recent-events 30
```

What to point at:

- public demo may use synthetic/provided threads; private dogfood should use accessible personal-infra comms unredacted.
- the useful output is not redaction — it is extracted people/projects/commitments/tasks written into Obsidian/Skyvault proposal notes, plus draft replies/actions.
- `approve_email_triage_packet` approves local proposal artifacts only.
- no sends, archives, deletes, drafts, calendar/account/payment/credential mutations.

## Demo 6: event planning packet reaches approval gate

```bash
hermes-workflows run event-planning-demo \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --project-root . \
  --db default \
  --id wf_july2_event_planning \
  --input-json '{}'

hermes-workflows worker \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --db default \
  --worker-id july2-event-worker \
  --max-commands 20 \
  --idle-exit-after 0.1

hermes-workflows status \
  --db .hermes/presentation-july2/workflows.sqlite \
  --id wf_july2_event_planning \
  --recent-events 40
```

What to point at:

- agent first shapes attendee count, venue criteria, promotion channels, and specific invitee/segment targets.
- timeline covers venue, promotion, direct outreach, comms, waivers, logistics, run-of-show, follow-up, and due dates/T-minus markers.
- `approve_event_ops_packet` gates the local packet.
- no emails, invites, posts, bookings, purchases, or waiver requests happen.

## Known footgun

If the registry file lives outside the source checkout, pass `--project-root /Users/skylarpayne/code/hermes-workflows`. Otherwise `hermes-workflows run` may invoke `uv run` from the registry directory and fail to import `hermes_workflows`. This runbook keeps the registry in the repo and still passes `--project-root .` to be explicit.

For source-tree examples under `examples/`, keep `PYTHONPATH=src:.` exported in the shell before running `hermes-workflows worker`; otherwise the installed console script may not be able to import the repo-local `examples.*` modules.

## Safe fallback if `run` misbehaves live

Use the lower-level source-checkout path. This is not the primary product demo, but it proves the same runtime state without the `uv run` wrapper:

```bash
PYTHONPATH=src:. python -m hermes_workflows start   hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow   --db .hermes/presentation-july2/workflows.sqlite   --id wf_july2_reviewable_draft   --input-json '{"topic":"Hermes Workflows July 2 demo"}'

PYTHONPATH=src:. python -m hermes_workflows worker   hermes_workflows.examples.reviewable_draft:reviewable_draft_workflow   --db .hermes/presentation-july2/workflows.sqlite   --id wf_july2_reviewable_draft   --worker-id july2-demo-worker   --max-commands 5   --idle-exit-after 0.1
```

## Do not do during the presentation

- Do not use live send/archive/calendar/payment/social/publish/deploy workflows.
- Do not demo with private Palmer DB state.
- Do not approve anything that performs external side effects.
- Do not route through an unverified dashboard source alias.

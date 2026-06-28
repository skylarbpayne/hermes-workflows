# July 2 live-demo fallback packet

Use this when the live demo gets weird. Do not debug in front of the room unless the bug itself teaches the product. Show the verified transcript receipts, say the line below, and keep the story moving.

## If the live demo dies, say this

> The important part is not that this terminal behaved perfectly on stage. The important part is that the workflow state is inspectable. Here is the verified run from the same clean source-checkout path: it reached a typed Review Queue gate, recorded the artifact and action schema, and did zero external side effects.

Then show:

1. Reviewable draft receipt: waiting on `signal:operator.response:review_draft_packet`.
2. Review Queue card shape: `Approve` / `Request changes`, feedback required for request-changes.
3. Dynamic workflow receipt: completed, generated `process_launch_item`, child items `dynamic-examples` and `subworkflow-ui` completed.
4. Side-effect boundary: no sends, publishes, deploys, PRs, merges, calendar changes, payments, or archives.

## Verified run transcript summary

Verified from `/Users/skylarpayne/code/hermes-workflows` on `main` at `a866a0141d0846333ca5e1ed14ff08b1349a25b8`.

```text
python -m pytest -q tests/test_launch_examples.py
# 7 passed in 0.33s

python -m pytest -q tests/test_artifacts.py
# 7 passed in 0.02s

hermes-workflows run reviewable-draft --config docs/presentation/2026-07-02/workflows.registry.example.json --project-root . --db default --id wf_july2_reviewable_draft --input-json '{"topic":"Hermes Workflows July 2 demo"}'
# status=running

hermes-workflows worker --config docs/presentation/2026-07-02/workflows.registry.example.json --db default --worker-id july2-demo-worker --max-commands 5 --idle-exit-after 0.1
# executed=3; final status=waiting; waiting_on=signal:operator.response:review_draft_packet

hermes-workflows status --db .hermes/presentation-july2/workflows.sqlite --id wf_july2_reviewable_draft --recent-events 20
# review_requests[0].request_type=human_input
# input_surface.actions=Approve, Request changes
# artifact.title="Review packet: Hermes Workflows July 2 demo"

hermes-workflows run dynamic-workflow-return --config docs/presentation/2026-07-02/workflows.registry.example.json --project-root . --db default --id wf_july2_dynamic_return --input-json '{}'
# status=running

hermes-workflows worker --config docs/presentation/2026-07-02/workflows.registry.example.json --db default --worker-id july2-demo-worker --max-commands 20 --idle-exit-after 0.1
# executed=7; final status=completed

hermes-workflows status --db .hermes/presentation-july2/workflows.sqlite --id wf_july2_dynamic_return --recent-events 30
# generated_workflow.symbol=process_launch_item
# generated_workflow.source_sha256=2ed46d957d89af961f45818c6a467d53eb8fbba1842f21beaee26a364da84d20
# processed ids=dynamic-examples, subworkflow-ui
# events include ChildWorkflowRequested and ChildWorkflowCompleted
```

Review Queue tool smoke against the demo DB:

```text
workflow_review_requests_list(db="/Users/skylarpayne/code/hermes-workflows/.hermes/presentation-july2/workflows.sqlite", status="waiting")
# count=1
# key=review_draft_packet
# schema=hermes_workflows.examples.reviewable_draft:ReviewDecision
# actions=approve, request_changes
```

## Exact command sequence for a fresh live run

```bash
cd /Users/skylarpayne/code/hermes-workflows
export PYTHONPATH=src:.
rm -f .hermes/presentation-july2/workflows.sqlite .hermes/presentation-july2/workflows.sqlite-*
python -m pytest -q tests/test_launch_examples.py
python -m pytest -q tests/test_artifacts.py

hermes-workflows run reviewable-draft \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --project-root . \
  --db default \
  --id wf_july2_reviewable_draft \
  --input-json '{"topic":"Hermes Workflows July 2 demo"}'

hermes-workflows worker \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --db default \
  --worker-id july2-demo-worker \
  --max-commands 5 \
  --idle-exit-after 0.1

hermes-workflows status \
  --db .hermes/presentation-july2/workflows.sqlite \
  --id wf_july2_reviewable_draft \
  --recent-events 20

hermes-workflows run dynamic-workflow-return \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --project-root . \
  --db default \
  --id wf_july2_dynamic_return \
  --input-json '{}'

hermes-workflows worker \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --db default \
  --worker-id july2-demo-worker \
  --max-commands 20 \
  --idle-exit-after 0.1

hermes-workflows status \
  --db .hermes/presentation-july2/workflows.sqlite \
  --id wf_july2_dynamic_return \
  --recent-events 30
```

## Known failure recovery

| Symptom | Use this recovery |
| --- | --- |
| `ModuleNotFoundError` for repo-local examples | Re-run from repo root with `export PYTHONPATH=src:.` and keep `--project-root .`. |
| `uv run` appears to execute from the wrong directory | Keep the registry inside `docs/presentation/2026-07-02/` and pass `--project-root .`; fallback to `PYTHONPATH=src:. python -m hermes_workflows ...`. |
| Demo DB has stale waiting cards | `rm -f .hermes/presentation-july2/workflows.sqlite .hermes/presentation-july2/workflows.sqlite-*` and rerun the sequence. |
| Dashboard does not show the demo DB | Do not switch to the private Palmer DB on stage. Either add a pre-approved temporary dashboard alias for `.hermes/presentation-july2/workflows.sqlite` before the talk, or show CLI status + Review Queue transcript. |
| Review Queue response path is tempting live | Do not approve any workflow that would publish/send/deploy/merge. For this no-side-effect demo, approving the reviewable draft is okay only if the room needs to see continuation. |
| Terminal gets noisy | Stop after the first `status` JSON and point to the fields: `status`, `waiting_on`, `review_requests`, `input_surface.actions`, `artifact.title`. |

## What to cut if time is short

Keep:

1. Opening pain: instructions are not durable obligations.
2. Reviewable draft demo to Review Queue.
3. Dynamic workflow return receipt.
4. Close: use agents for judgment; use workflows for obligations.

Cut:

- Full content/email/event/coding portfolio walkthrough.
- Blogpost discussion.
- Internal Palmer worker/DB cleanup details.
- Runtime architecture refactor history.

## Approval / non-action boundary

This packet is safe to show as a local source-checkout/demo receipt. It does **not** approve public publishing, package release, announcement, email/social send, external scheduling, production deploy, commit/push/PR/merge, or use of private Palmer workflow artifacts in the public repo.

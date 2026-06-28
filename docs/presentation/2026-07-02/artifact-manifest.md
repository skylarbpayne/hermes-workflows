# July 2 artifact manifest

Updated on 2026-06-24 from `/Users/skylarpayne/code/hermes-workflows` after rerunning the July 2 hero demo path.

## Deliverables in this packet

- Light presentation: `slides.html`
- Demo speaking script: `speaking-script.md`
- Rewritten launch blogpost: `launch-blogpost-draft.md`
- Blog visual elements: `content-studio/visuals/*` generated locally with Gemini Nano Banana 2 when the visual plan is approved
- Public example workflows map: `public-examples-map.md`
- Demo runbook: `demo-runbook.md`
- Fallback packet: `fallback-packet.md`

## Verified demo evidence

Live state was rechecked on 2026-06-24:

- Public repo `main` equals `origin/main` at `a866a0141d0846333ca5e1ed14ff08b1349a25b8`; GitHub docs/tests/Pages checks for that head are successful.
- Palmer runtime/dashboard live state was separately verified and written to Kanban/Skyvault. This public packet intentionally does not include private workflow artifacts or private DB receipts.
- Presentation packet directory contains 12 files, including this manifest and `fallback-packet.md`.

Local smoke commands were run from repo `main` at commit `a866a0141d0846333ca5e1ed14ff08b1349a25b8`.

### Tests

- `python -m pytest -q tests/test_launch_examples.py` → `7 passed in 0.33s`.
- `python -m pytest -q tests/test_artifacts.py` → `7 passed in 0.02s`.

### Reviewable draft

- Workflow id: `wf_july2_reviewable_draft`
- Command shape verified: `hermes-workflows run reviewable-draft --config docs/presentation/2026-07-02/workflows.registry.example.json --project-root . --db default ...`
- Worker command verified: `hermes-workflows worker --config docs/presentation/2026-07-02/workflows.registry.example.json --db default --worker-id july2-demo-worker --max-commands 5 --idle-exit-after 0.1`
- Final status: `waiting`
- Waiting key: `signal:operator.response:review_draft_packet`
- Review request: `review_draft_packet`
- Schema-derived actions: `approve`, `request_changes`

### Dynamic workflow return

- Workflow id: `wf_july2_dynamic_return`
- Final status: `completed`
- Generated workflow symbol: `process_launch_item`
- Generated source SHA-256: `2ed46d957d89af961f45818c6a467d53eb8fbba1842f21beaee26a364da84d20`
- Processed items: `dynamic-examples`, `subworkflow-ui`
- Event proof: `ChildWorkflowRequested` and `ChildWorkflowCompleted` appeared for both children.
- Presentation registry verification also passed with `PYTHONPATH=src:. hermes-workflows run dynamic-workflow-return ...` followed by the registry worker.

## Review checklist

- [ ] Skylar approves the presentation angle.
- [ ] Skylar chooses whether the blogpost title is acceptable or needs a title reset.
- [ ] Skylar approves the blog visual plan before Gemini Nano Banana 2 generation.
- [ ] Demo commands are rerun on the presentation machine before July 2.
- [ ] If using the dashboard live, verify the dashboard DB alias points at `.hermes/presentation-july2/workflows.sqlite` and browser-smoke `/workflows`.
- [ ] Any public docs navigation or website placement is approved separately.
- [ ] Any publish, merge, schedule, social post, or external presentation action is explicitly approved.

## Known issue / footgun

`hermes-workflows run` shells through `uv run`. If the registry is outside the source checkout and `--project-root` is omitted, `uv` can run from the wrong directory and fail to import `hermes_workflows`. The runbook uses `--project-root .` deliberately.

Source-tree example aliases also require `PYTHONPATH=src:.` in the presentation shell so the worker can import `examples.*` modules from the checkout.

## Non-actions

- No external publish.
- No PR opened unless the private coding workflow explicitly passes `approve_create_pr`.
- No merge.
- No live email/calendar/social/payment/deploy side effects.
- No visual assets published/uploaded; Gemini Nano Banana 2 outputs stay local until a separate publish/upload gate.

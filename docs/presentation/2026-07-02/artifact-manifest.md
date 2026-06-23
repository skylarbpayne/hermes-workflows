# July 2 artifact manifest

Generated on 2026-06-22 08:59 PDT from `/Users/skylarpayne/code/hermes-workflows`.

## Deliverables in this packet

- Light presentation: `slides.html`
- Demo speaking script: `speaking-script.md`
- Rewritten launch blogpost: `launch-blogpost-draft.md`
- Blog visual elements: `content-studio/visuals/*` generated locally with Gemini Nano Banana 2 when the visual plan is approved
- Public example workflows map: `public-examples-map.md`
- Demo runbook: `demo-runbook.md`
- Presentation demo registry: `workflows.registry.example.json`

## Verified demo evidence

Local smoke commands were run from repo `main` at commit `6bc8233`.

### Reviewable draft

- Workflow id: `wf_july2_run_command`
- Command shape verified: `hermes-workflows run reviewable-draft --config /tmp/hermes-workflows-july2-demo/workflows.registry.json --project-root /Users/skylarpayne/code/hermes-workflows --db default ...`
- Worker command verified: `hermes-workflows worker --config /tmp/hermes-workflows-july2-demo/workflows.registry.json --db default --worker-id july2-run-worker --max-commands 5 --idle-exit-after 0.1`
- Final status: `waiting`
- Waiting key: `signal:operator.response:review_draft_packet`
- Review request: `review_draft_packet`
- Schema-derived actions: `approve`, `request_changes`

### Dynamic workflow return

- Workflow id: `wf_july2_dynamic_return`
- Final status: `completed`
- Generated workflow symbol: `process_launch_item`
- Generated source SHA-256: `835faa76ecc050011ff2f02149d701cc196ff30066555ad590d85f7afb0a1b43`
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

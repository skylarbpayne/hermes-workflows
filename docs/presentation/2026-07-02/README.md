# Hermes Workflows July 2 presentation kit

Local review packet for the July 2 Hermes Workflows presentation. This directory packages the story, demo, blog draft, public examples, and evidence into one place so the presentation is not scattered across chat, issues, and old PR receipts.

Status: review-ready local artifact. Nothing here has been published, merged into public docs navigation, or presented externally.

## Files

- `slides.html` — lightweight self-contained slide deck for the talk.
- `speaking-script.md` — speaker notes and transitions for a 12-18 minute version.
- `demo-runbook.md` — exact demo commands, fallback path, what to show, and failure handling.
- `launch-blogpost-draft.md` — rewritten launch blogpost candidate.
- `public-examples-map.md` — curated public example workflows and which product primitive each proves.
- `workflows.registry.example.json` — presentation/demo registry for the public examples.
- `artifact-manifest.md` — review checklist, evidence receipts, and remaining approval gates.

## Recommended presentation shape

1. Start with the failure: the instruction existed, but the system could not enforce or remember it.
2. Show the move: promote important requirements into workflow state, typed review, deterministic checks, and receipts.
3. Demo a tiny no-side-effect run reaching Review Queue.
4. Show dynamic workflow composition as the advanced proof.
5. End with the boundary: prompts/skills/subagents remain useful; workflows own the obligations that matter.

## Verified local evidence

On 2026-06-22, the source-checkout demo was run locally from repo `main` at `6bc8233`:

- `reviewable-draft` reached `status=waiting`, `waiting_on=signal:operator.response:review_draft_packet`, with one `review_requests` card.
- `dynamic-workflow-return` reached `status=completed`, generated workflow `process_launch_item`, and completed two child workflow items.
- `hermes-workflows run ... --config /tmp/... --project-root /Users/skylarpayne/code/hermes-workflows` worked; without `--project-root`, a config outside the repo made `uv run` execute outside the checkout and fail to import `hermes_workflows`. The runbook uses `--project-root .` to avoid that footgun.

## Approval boundary

These are local/repo artifacts for Skylar review. Do not publish, merge, announce, schedule, or present externally from this packet without explicit approval.

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
- `fallback-packet.md` — live-demo recovery script, exact commands, verified transcript, and what to cut.

## Recommended presentation shape

Hero path, not feature buffet:

1. Start with the failure: the instruction existed, but the system could not enforce or remember it.
2. Show the move: promote important requirements into workflow state, typed review, deterministic checks, and receipts.
3. Demo `reviewable-draft` reaching Review Queue with zero external side effects.
4. Show `dynamic-workflow-return` as the advanced proof: generated workflow value, source hash, child workflow receipts.
5. End with the boundary: prompts/skills/subagents remain useful; workflows own the obligations that matter.

If time is short, cut content/email/event/coding portfolio details. Mention them as follow-on lanes only after the core obligation story lands.

## Verified local evidence

On 2026-06-24, the source-checkout demo path was rerun locally from repo `main` at `a866a0141d0846333ca5e1ed14ff08b1349a25b8`:

- Public repo `main` matches `origin/main`; GitHub checks for `a866a0141d0846333ca5e1ed14ff08b1349a25b8` are green: docs, tests, and Pages deployment completed successfully.
- `python -m pytest -q tests/test_launch_examples.py` passed: `7 passed in 0.33s`.
- `python -m pytest -q tests/test_artifacts.py` passed: `7 passed in 0.02s`.
- `reviewable-draft` reached `status=waiting`, `waiting_on=signal:operator.response:review_draft_packet`, with one `review_requests` card.
- Review Queue tool smoke against `.hermes/presentation-july2/workflows.sqlite` returned `count=1`, key `review_draft_packet`, typed schema `ReviewDecision`, and actions `approve` / `request_changes`.
- `dynamic-workflow-return` reached `status=completed`, generated workflow `process_launch_item`, source SHA `2ed46d957d89af961f45818c6a467d53eb8fbba1842f21beaee26a364da84d20`, and completed `dynamic-examples` plus `subworkflow-ui`.
- Artifact render contracts are covered by `tests/test_artifacts.py`; render modes include inline markdown/json/html/diff, media references, file references, external links, and generated workflow Python source.
- Current dashboard caveat: Palmer dashboard is running and the plugin is enabled, but its configured DB alias points at a separate clean runtime DB, not the repo-local presentation demo DB. For a live dashboard demo, add a pre-approved temporary alias for `.hermes/presentation-july2/workflows.sqlite` before the talk; otherwise use the CLI/Review Queue transcript fallback.

## Approval boundary

These are local/repo artifacts for Skylar review. Do not publish, merge, announce, schedule, or present externally from this packet without explicit approval.

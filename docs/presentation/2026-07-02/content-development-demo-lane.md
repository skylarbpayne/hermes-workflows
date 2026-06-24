# July 2 content-development demo lane

Goal: demo Hermes Workflows as the way to produce launch content from one approved content spine — topic → research → angle → outline → section draft/humanize/review → canonical draft → Gemini Nano Banana 2 blog visuals → blog/deck/video adapters — without letting an agent publish, email, merge, or upload until a typed human gate says so.

## Demo story

1. Start `content-asset-lane` with a launch brief and requested formats: blogpost, slide deck, HyperFrames video.
2. `agent(...)` does the substantive editorial work: brainstorm topics, research, angles, outline, per-section drafting/humanizing, whole-draft pass, Gemini Nano Banana 2 visual generation, and format adapters.
3. `ask(...)` gates topic, angle, outline, section reviews, canonical draft, asset plan, blog visual plan, and final local packet in Review Queue.
4. Final `ask(...)` approves only a local content packet. Separate publish/social/video/upload gates remain unapproved by default.

Live narrative line: "The agent can write the launch assets, but the workflow owns the spine, receipts, typed decisions, and no external side effects."

## Primary workflow example

Target file: `examples/content_asset_lane.py`. The code block below is historical shape notes; the runnable example is the source of truth.

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hermes_workflows import agent, ask, bash, parallel, workflow


@dataclass
class ContentLaneInput:
    repo_path: str = "/Users/skylarpayne/code/hermes-workflows"
    output_dir: str = "docs/presentation/2026-07-02/content-lane"
    launch_date: str = "2026-07-02"
    audience: str = "developers evaluating Hermes Workflows"
    thesis: str = "code-first workflows coordinate agents, deterministic checks, and review gates"


@dataclass
class AssetBrief:
    asset_id: Literal["blogpost", "deck", "video_script", "demo_script"]
    title: str
    objective: str
    required_points: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


@dataclass
class ContentPlan:
    positioning: str
    assets: list[AssetBrief]
    demo_arc: list[str]
    non_goals: list[str]


@dataclass
class AssetDraft:
    asset_id: str
    path: str
    title: str
    body: str
    review_notes: list[str] = field(default_factory=list)


@dataclass
class EditorialReview:
    action: Literal["approve", "request_changes", "drop"]
    feedback: str | None = None


@dataclass
class PackageDecision:
    action: Literal["approve_local_packet", "request_changes"]
    publish_ok: bool = False
    feedback: str | None = None


@workflow
async def content_asset_lane(inputs: ContentLaneInput) -> dict:
    req = inputs if isinstance(inputs, ContentLaneInput) else ContentLaneInput(**dict(inputs or {}))

    preflight = await bash(
        "set -euo pipefail\n"
        "git status --short\n"
        "python -m pytest -q tests/test_launch_examples.py\n"
        "printf '\\n--- july2 files ---\\n'\n"
        "python - <<'PY'\nfrom pathlib import Path\nfor p in sorted(Path('docs/presentation/2026-07-02').glob('*')):\n    print(p)\nPY",
        key="content_preflight",
        cwd=req.repo_path,
        timeout_seconds=180,
    )

    plan = await agent(
        "plan_content_lane",
        prompt=(
            "Create a July 2 content plan for Hermes Workflows. Be concrete: "
            "four assets only (blogpost, deck, video_script, demo_script), code-first, no publish side effects."
        ),
        input={"request": req, "preflight_stdout": preflight.stdout[-6000:]},
        returns=ContentPlan,
        mock_output={
            "positioning": "Hermes Workflows turns agent work into durable, reviewable workflow state.",
            "assets": [
                {"asset_id": "blogpost", "title": "Hermes Workflows: agents with receipts", "objective": "Launch narrative", "required_points": ["agent()", "bash()", "ask()", "Review Queue"]},
                {"asset_id": "deck", "title": "Code-first workflows", "objective": "12-minute presentation spine", "required_points": ["problem", "architecture", "demo", "boundaries"]},
                {"asset_id": "video_script", "title": "Two-minute product walkthrough", "objective": "Recordable VO + shots", "required_points": ["terminal", "Review Queue", "status receipts"]},
                {"asset_id": "demo_script", "title": "Live demo runbook", "objective": "Commands and fallback", "required_points": ["preflight", "reviewable-draft", "dynamic workflow", "fallback"]},
            ],
            "demo_arc": ["draft", "deterministic check", "Review Queue", "child workflows", "final local packet"],
            "non_goals": ["publish", "merge", "send email", "upload video"],
        },
    )

    plan_decision = await ask(
        "Select the content topic before research, outline, or drafting.",
        key="select_content_topic",
        input={"plan": plan, "preflight": preflight},
        returns=EditorialReview,
    )
    if plan_decision.action != "approve":
        return {"status": "needs_topic_selection", "plan": plan, "decision": plan_decision}

    drafts = await parallel(
        [draft_asset(req, brief) for brief in plan.assets],
        limit=4,
    )

    package = await agent(
        "assemble_content_packet",
        prompt="Assemble the reviewed assets into a concise manifest with demo order and unresolved risks.",
        input={"plan": plan, "drafts": drafts},
        returns=dict,
    )

    final_decision = await ask(
        "Approve this local July 2 content packet. This does not approve publishing or external distribution.",
        key="approve_local_content_packet",
        input={"package": package, "drafts": drafts},
        returns=PackageDecision,
    )

    return {
        "status": "local_packet_approved" if final_decision.action == "approve_local_packet" else "needs_changes",
        "package": package,
        "drafts": drafts,
        "final_decision": final_decision,
        "side_effects": {"published": False, "merged": False, "emailed": False, "uploaded": False},
    }


def draft_asset(req: ContentLaneInput, brief: AssetBrief):
    return agent(
        f"draft_{brief.asset_id}",
        prompt=(
            "Draft this launch asset as production-ready Markdown/HTML/script text. "
            "Use Hermes Workflows vocabulary exactly: agent(), bash(), ask(), Review Queue. "
            "No claims about unpublished features unless marked as demo-only."
        ),
        input={"request": req, "brief": brief},
        key_by=brief.asset_id,
        returns=AssetDraft,
        files=[
            "docs/presentation/2026-07-02/README.md",
            "docs/presentation/2026-07-02/demo-runbook.md",
            "docs/presentation/2026-07-02/public-examples-map.md",
        ],
        mock_output={
            "asset_id": brief.asset_id,
            "path": f"{req.output_dir}/{brief.asset_id}.md",
            "title": brief.title,
            "body": f"# {brief.title}\n\nDraft asset for {req.launch_date}: {brief.objective}.\n",
            "review_notes": ["Mock output; remove mock_output for live agent drafting."],
        },
    )
```

## Asset-specific child workflows to implement

Use these when the demo wants child-workflow receipts instead of a single parent fan-out. Each child returns `{path, title, body, checks, decision}` and waits on its own Review Queue card.

### Blogpost workflow

```python
@workflow
async def blogpost_asset(inputs: dict) -> dict:
    draft = await agent("draft_blogpost", prompt="Write the launch blogpost.", input=inputs, returns=AssetDraft)
    fact_check = await agent("fact_check_blogpost", prompt="Find unsupported claims and missing caveats.", input=draft, returns=dict)
    lint = await bash("python - <<'PY'\nfrom pathlib import Path\np=Path('docs/presentation/2026-07-02/launch-blogpost-draft.md')\nprint(p.exists(), p.stat().st_size if p.exists() else 0)\nPY", key="blogpost_file_check", cwd=inputs["repo_path"])
    decision = await ask("Editorial review: blogpost draft.", key="review_blogpost", input={"draft": draft, "fact_check": fact_check, "lint": lint}, returns=EditorialReview)
    return {"asset": draft, "decision": decision}
```

### Deck workflow

```python
@workflow
async def deck_asset(inputs: dict) -> dict:
    slides = await agent("draft_deck", prompt="Create a 10-slide HTML deck for the July 2 demo.", input=inputs, returns=AssetDraft)
    check = await bash("python - <<'PY'\nfrom pathlib import Path\np=Path('docs/presentation/2026-07-02/slides.html')\ntext=p.read_text() if p.exists() else ''\nprint({'exists': p.exists(), 'slides': text.count('<section')})\nPY", key="deck_structure_check", cwd=inputs["repo_path"])
    decision = await ask("Review deck before it is used live.", key="review_deck", input={"slides": slides, "check": check}, returns=EditorialReview)
    return {"asset": slides, "decision": decision}
```

### Video script workflow

```python
@workflow
async def video_script_asset(inputs: dict) -> dict:
    script = await agent("draft_video_script", prompt="Write a 2-minute VO script plus shot list; no recording/uploading.", input=inputs, returns=AssetDraft)
    timing = await agent("estimate_video_timing", prompt="Estimate duration and cut lines to stay under 2:15.", input=script, returns=dict)
    decision = await ask("Approve video script for recording only; not upload/publish.", key="review_video_script", input={"script": script, "timing": timing}, returns=EditorialReview)
    return {"asset": script, "decision": decision, "side_effects": {"recorded": False, "uploaded": False}}
```

### Live demo script workflow

```python
@workflow
async def demo_script_asset(inputs: dict) -> dict:
    runbook = await agent("draft_live_demo_script", prompt="Write exact commands, expected outputs to point at, fallback path, and no-side-effect constraints.", input=inputs, returns=AssetDraft)
    smoke = await bash("python -m pytest -q tests/test_launch_examples.py", key="demo_smoke_tests", cwd=inputs["repo_path"], timeout_seconds=180)
    decision = await ask("Approve live demo script and fallback path.", key="review_demo_script", input={"runbook": runbook, "smoke": smoke}, returns=EditorialReview)
    return {"asset": runbook, "decision": decision}
```

## Artifacts generated

Write local files only under `docs/presentation/2026-07-02/content-lane/`:

- `content-plan.json` — approved plan, asset briefs, non-goals.
- `blogpost.md` — publish candidate, not published.
- `deck.html` — self-contained presentation variant or patch to `slides.html`.
- `video-script.md` — voiceover, shot list, b-roll, duration estimate.
- `visuals/` — Gemini Nano Banana 2 local blog visuals: hero, diagram/figure, social card candidates.
- `visual-generation-receipts.json` — model, prompt, output path, and QA notes for each visual.
- `demo-script.md` — exact commands, presenter callouts, failure fallback.
- `artifact-manifest.md` — generated file list, hashes, approval keys, visual-generation receipts, smoke-test receipts.
- `review-packet.json` — Review Queue payload with all decisions and side-effect flags.

## Approval gates

| Gate key | Blocks | Allowed actions | Side effects allowed after approval |
| --- | --- | --- | --- |
| `select_content_topic` | Research/angle/outlining beyond topic selection | selected topic / feedback | none |
| `approve_content_outline` | Section drafting beyond outline | `approve`, `request_changes`, `drop` | none |
| `approve_content_section_*` | Section accepted into canonical draft | `approve`, `request_changes`, `drop` | none |
| `approve_canonical_content_draft` | Format adaptation beyond approved spine | `approve`, `request_changes`, `drop` | none |
| `approve_content_asset_plan` | Blog/deck/video adapter planning | `approve`, `request_changes`, `drop` | none |
| `approve_blog_visual_elements_plan` | Gemini Nano Banana 2 visual generation | `approve`, `request_changes`, `drop` | local image generation only; publish/upload separate |
| `approve_local_content_packet` | Final packet considered ready | `approve_local_packet`, `request_changes` | none if `publish_ok=False` |
| `publish_content_packet` | External publish/schedule/social/upload | `approve_publish`, `request_changes`, `cancel` | only the exact listed external action |

Hard rule for the demo: do not implement `publish_content_packet` with real adapters before July 2 unless Skylar explicitly asks. Keep it as an `ask(...)` card showing the boundary.

## Likely repo files to implement

- `examples/content_asset_lane.py` — main runnable demo with mock outputs.
- `tests/test_launch_examples.py` — asserts the workflow reaches `select_content_topic`; deeper approval-path tests can cover outline/section/packet gates when needed.
- `docs/presentation/2026-07-02/workflows.registry.example.json` — add alias `content-asset-lane` with `python_paths: ["src", "."]`.
- `docs/presentation/2026-07-02/demo-runbook.md` — add optional Demo 3 commands for the content lane.
- `docs/presentation/2026-07-02/public-examples-map.md` — add row for `examples/content_asset_lane.py` proving multi-asset content workflows.
- `docs/presentation/2026-07-02/artifact-manifest.md` — add content-lane generated artifacts and approval receipts after smoke run.
- Optional after public API polish: `src/hermes_workflows/examples/content_asset_lane.py` if this should ship as an installed quickstart, not just a July 2 repo example.

## Demo commands after implementation

```bash
cd /Users/skylarpayne/code/hermes-workflows
export PYTHONPATH=src:.
rm -f .hermes/presentation-july2/workflows.sqlite
hermes-workflows run content-asset-lane \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --project-root . \
  --db default \
  --id wf_july2_content_lane \
  --input-json '{}'
hermes-workflows worker \
  --config docs/presentation/2026-07-02/workflows.registry.example.json \
  --db default \
  --worker-id july2-content-worker \
  --max-commands 20 \
  --idle-exit-after 0.1
hermes-workflows status \
  --db .hermes/presentation-july2/workflows.sqlite \
  --id wf_july2_content_lane \
  --recent-events 40
```

Expected first live stop: `status=waiting`, `waiting_on=signal:operator.response:select_content_topic`, one Review Queue card, no files published/merged/uploaded.

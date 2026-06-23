from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hermes_workflows import agent, ask, parallel, workflow


@dataclass
class ContentStudioInput:
    launch_date: str = "2026-07-02"
    audience: str = "developers evaluating Hermes Workflows"
    seed: str = "Hermes Workflows launch narrative for the July 2 demo"
    output_dir: str = "docs/presentation/2026-07-02/content-studio"
    approver: str = "human:operator"
    visual_model: str = "gemini-nano-banana-2"
    formats: tuple[Literal["blogpost", "slide_deck", "hyperframes_video"], ...] = (
        "blogpost",
        "slide_deck",
        "hyperframes_video",
    )


@dataclass
class TopicCandidate:
    id: str
    title: str
    why_now: str
    evidence_needed: list[str] = field(default_factory=list)


@dataclass
class TopicBrainstormPacket:
    summary: str
    topics: list[TopicCandidate]
    rejected_lanes: list[str] = field(default_factory=list)


@dataclass
class SelectionDecision:
    action: str
    selected_id: str | None = None
    feedback: str | None = None


@dataclass
class ResearchPacket:
    selected_topic: str
    thesis_pressure: str
    claims: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


@dataclass
class AngleOption:
    id: str
    title: str
    angle: str
    reader_promise: str


@dataclass
class AnglePacket:
    selected_topic: str
    options: list[AngleOption]
    notes: str = ""


@dataclass
class OutlineSection:
    id: str
    title: str
    job: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class OutlinePacket:
    title: str
    thesis: str
    sections: list[OutlineSection]


@dataclass
class SectionPacket:
    section_id: str
    title: str
    draft_markdown: str
    humanized_markdown: str
    humanizer_notes: list[str] = field(default_factory=list)


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes", "drop"]
    feedback: str | None = None


@dataclass
class CanonicalDraft:
    title: str
    markdown: str
    approved_sections: list[str]
    humanizer_notes: list[str] = field(default_factory=list)


@dataclass
class AssetSpec:
    asset_id: Literal["blogpost", "slide_deck", "hyperframes_video"]
    title: str
    source: str
    output_path: str
    acceptance_checks: list[str] = field(default_factory=list)


@dataclass
class AssetPlan:
    principle: str
    assets: list[AssetSpec]
    side_effect_boundary: list[str] = field(default_factory=list)


@dataclass
class VisualElementSpec:
    element_id: str
    title: str
    purpose: str
    placement: str
    prompt: str
    output_path: str
    acceptance_checks: list[str] = field(default_factory=list)


@dataclass
class VisualElementPlan:
    model: str
    source_asset_id: str
    elements: list[VisualElementSpec]
    style_constraints: list[str] = field(default_factory=list)
    side_effect_boundary: list[str] = field(default_factory=list)


@dataclass
class GeneratedVisualElement:
    element_id: str
    path: str
    model: str
    prompt: str
    receipt: str
    local_only: bool = True
    qa_notes: list[str] = field(default_factory=list)


@dataclass
class VisualElementSet:
    model: str
    asset_id: str
    visuals: list[GeneratedVisualElement]
    receipt_summary: str
    side_effects: dict[str, bool] = field(default_factory=dict)


@dataclass
class GeneratedAsset:
    asset_id: str
    path: str
    title: str
    notes: str
    supporting_visuals: list[str] = field(default_factory=list)
    local_only: bool = True


@dataclass
class PackageDecision:
    action: Literal["approve_local_packet", "request_changes"]
    feedback: str | None = None


@workflow
async def content_asset_lane_workflow(inputs: ContentStudioInput) -> dict:
    """Content studio demo: one elegant writing spine, multiple asset adapters.

    Shape:
    brainstorm topics -> select topic -> research -> brainstorm angles -> select angle
    -> outline -> approve outline -> draft/humanize/review each section -> combine/humanize
    -> approve canonical draft -> plan/generate blog visuals with Gemini Nano Banana 2
    -> adapt into blogpost, slide deck, and HyperFrames video.
    """

    req = inputs if isinstance(inputs, ContentStudioInput) else ContentStudioInput(**dict(inputs or {}))

    topics = await agent(
        "brainstorm_content_topics",
        prompt=(
            "Brainstorm content topics for the requested launch/demo. Return a small set of durable, "
            "evidence-backed topics. Avoid slogan topics; prefer scar-tissue topics a developer would recognize."
        ),
        input=req,
        returns=TopicBrainstormPacket,
        mock_output={
            "summary": "Pick one concrete pain the July 2 demo can prove, then generate all assets from that spine.",
            "topics": [
                {
                    "id": "requirements-stop-being-suggestions",
                    "title": "When requirements live in prompts, they become suggestions",
                    "why_now": "The coding workflow demo shows a requirement promoted into workflow state, checks, and gates.",
                    "evidence_needed": ["worktree diff", "validation output", "Review Queue gate"],
                },
                {
                    "id": "agents-with-receipts",
                    "title": "Agents need receipts, not just better prompts",
                    "why_now": "The demo can show durable state and review packets instead of chat archaeology.",
                    "evidence_needed": ["run DAG", "approval card", "artifact packet"],
                },
                {
                    "id": "side-effects-deserve-a-door",
                    "title": "Side effects deserve a door, not a vibe check",
                    "why_now": "Email/event/code demos all pause before sends, bookings, PRs, and publishing.",
                    "evidence_needed": ["typed ask schema", "zero-side-effect ledger"],
                },
            ],
            "rejected_lanes": ["generic AI automation launch post", "abstract workflow philosophy"],
        },
    )

    topic_decision = await ask(
        "Select the content topic to develop.",
        key="select_content_topic",
        input={"topics": topics, "side_effects": _zero_side_effects()},
        returns=SelectionDecision,
        approver=req.approver,
    )
    selected_topic = _selected_topic(topics, topic_decision)
    if selected_topic is None:
        return {"status": "needs_topic_selection", "topics": topics, "decision": topic_decision, "side_effects": _zero_side_effects()}

    research = await agent(
        "research_content_topic",
        prompt=(
            "Research the selected topic across source notes, prior sessions, web/practitioner language, "
            "and product receipts. Return claims, evidence, and gaps. Do not invent receipts."
        ),
        input={"request": req, "topic": selected_topic},
        returns=ResearchPacket,
    )

    angles = await agent(
        "brainstorm_content_angles",
        prompt=(
            "Generate a few angle/title options from the research. Keep them blunt and practitioner-shaped. "
            "No slogan pools, no corporate launch-post scaffolding."
        ),
        input={"request": req, "topic": selected_topic, "research": research},
        returns=AnglePacket,
    )
    angle_decision = await ask(
        "Select the content angle before outlining.",
        key="select_content_angle",
        input={"angles": angles, "research": research, "side_effects": _zero_side_effects()},
        returns=SelectionDecision,
        approver=req.approver,
    )
    selected_angle = _selected_angle(angles, angle_decision)
    if selected_angle is None:
        return {"status": "needs_angle_selection", "angles": angles, "decision": angle_decision, "side_effects": _zero_side_effects()}

    outline = await agent(
        "draft_content_outline",
        prompt="Draft the outline from the approved topic and angle. Each section needs one job and evidence hooks.",
        input={"request": req, "topic": selected_topic, "research": research, "angle": selected_angle},
        returns=OutlinePacket,
    )
    outline_decision = await ask(
        "Approve the content outline before any prose drafting.",
        key="approve_content_outline",
        input={"outline": outline, "research": research, "side_effects": _zero_side_effects()},
        returns=ReviewDecision,
        approver=req.approver,
    )
    if outline_decision.action != "approve":
        return {"status": "needs_outline_changes", "outline": outline, "decision": outline_decision, "side_effects": _zero_side_effects()}

    section_packets = await parallel([_draft_humanize_section(req, outline, section, index) for index, section in enumerate(outline.sections, start=1)], limit=3)
    section_decisions = await parallel(
        [
            ask(
                f"Review content section {index}: {packet.title}",
                key=f"approve_content_section_{index}",
                input={"section": packet, "side_effects": _zero_side_effects()},
                returns=ReviewDecision,
                approver=req.approver,
            )
            for index, packet in enumerate(section_packets, start=1)
        ],
        limit=3,
    )
    rejected = [
        {"section": packet, "decision": decision}
        for packet, decision in zip(section_packets, section_decisions)
        if decision.action != "approve"
    ]
    if rejected:
        return {"status": "needs_section_changes", "rejected": rejected, "sections": section_packets, "side_effects": _zero_side_effects()}

    canonical = await agent(
        "combine_and_humanize_canonical_draft",
        prompt=(
            "Combine the approved, humanized sections into one canonical draft. Run a whole-draft humanizer pass: "
            "remove repetition, smooth transitions, preserve scar tissue, cut AI-smelly symmetry."
        ),
        input={"request": req, "outline": outline, "sections": section_packets},
        returns=CanonicalDraft,
    )
    canonical_decision = await ask(
        "Approve the canonical draft before adapting it into blog/deck/video assets.",
        key="approve_canonical_content_draft",
        input={"canonical": canonical, "side_effects": _zero_side_effects()},
        returns=ReviewDecision,
        approver=req.approver,
    )
    if canonical_decision.action != "approve":
        return {"status": "needs_canonical_draft_changes", "canonical": canonical, "decision": canonical_decision, "side_effects": _zero_side_effects()}

    asset_plan = await agent(
        "plan_asset_adapters",
        prompt=(
            "Plan format adapters from the canonical draft. Preserve one spine; do not let each asset invent a new thesis. "
            "Include blogpost, slide deck, and HyperFrames video source/render packet."
        ),
        input={"request": req, "canonical": canonical, "formats": req.formats},
        returns=AssetPlan,
        mock_output={
            "principle": "One approved content spine; separate format adapters for prose, presentation, and local video render.",
            "assets": [
                {
                    "asset_id": "blogpost",
                    "title": "Launch blogpost",
                    "source": "canonical draft",
                    "output_path": f"{req.output_dir}/blogpost.md",
                    "acceptance_checks": ["voice pass", "evidence map", "publish gate separate"],
                },
                {
                    "asset_id": "slide_deck",
                    "title": "July 2 light deck",
                    "source": "canonical draft + demo spine",
                    "output_path": f"{req.output_dir}/slides.md",
                    "acceptance_checks": ["short intro", "demo-first", "fallback slide"],
                },
                {
                    "asset_id": "hyperframes_video",
                    "title": "HyperFrames demo video package",
                    "source": "canonical draft + visual storyboard",
                    "output_path": f"{req.output_dir}/hyperframes-video/",
                    "acceptance_checks": ["hyperframes doctor", "npm run check", "render mp4", "ffprobe", "thumbnail QA"],
                },
            ],
            "side_effect_boundary": ["No publish", "No upload", "No social scheduling", "No PR merge"],
        },
    )
    asset_plan_decision = await ask(
        "Approve the multi-format asset plan before generating assets.",
        key="approve_content_asset_plan",
        input={"asset_plan": asset_plan, "canonical": canonical, "side_effects": _zero_side_effects()},
        returns=ReviewDecision,
        approver=req.approver,
    )
    if asset_plan_decision.action != "approve":
        return {"status": "needs_asset_plan_changes", "asset_plan": asset_plan, "decision": asset_plan_decision, "side_effects": _zero_side_effects()}

    visual_plan = await agent(
        "plan_blog_visual_elements",
        prompt=(
            "Plan visual elements for the blogpost from the approved canonical draft. Use Gemini Nano Banana 2 as the "
            "generation model. The plan should include a hero image, one explanatory diagram/figure, and a social/card "
            "variant when useful. Keep prompts concrete, non-generic, and tied to the article thesis."
        ),
        input={"request": req, "canonical": canonical, "asset_plan": asset_plan, "model": req.visual_model},
        returns=VisualElementPlan,
        mock_output={
            "model": req.visual_model,
            "source_asset_id": "blogpost",
            "elements": [
                {
                    "element_id": "blog_hero",
                    "title": "Blog hero visual",
                    "purpose": "Open the post with the core metaphor: agent work needs durable receipts and gates.",
                    "placement": "top of blogpost",
                    "prompt": "A clean editorial hero image: an AI agent handing a stamped receipt through a workflow gate, with subtle code/worktree/review-queue motifs; no text in image.",
                    "output_path": f"{req.output_dir}/visuals/blog-hero.png",
                    "acceptance_checks": ["no fake UI text", "fits blog header crop", "matches article thesis"],
                },
                {
                    "element_id": "workflow_receipts_diagram",
                    "title": "Workflow receipts diagram",
                    "purpose": "Explain the flow from agent work to receipts to human approval.",
                    "placement": "middle of blogpost near workflow explanation",
                    "prompt": "Minimal diagram-style illustration of agent -> deterministic checks -> receipt packet -> human approval gate -> side-effect boundary; crisp shapes, no tiny unreadable labels.",
                    "output_path": f"{req.output_dir}/visuals/workflow-receipts-diagram.png",
                    "acceptance_checks": ["legible at blog width", "shows gate/boundary clearly", "not generic AI stock art"],
                },
                {
                    "element_id": "social_card",
                    "title": "Social share card",
                    "purpose": "Provide a shareable visual derived from the hero concept.",
                    "placement": "social preview / OG image candidate",
                    "prompt": "Wide editorial social card about workflows turning agent output into reviewable receipts and approvals; strong composition, no embedded text.",
                    "output_path": f"{req.output_dir}/visuals/social-card.png",
                    "acceptance_checks": ["16:9 crop works", "no embedded text", "visually related to hero"],
                },
            ],
            "style_constraints": ["editorial/product illustration", "no text baked into images", "avoid glossy AI-stock look", "consistent palette across variants"],
            "side_effect_boundary": ["local files only", "no publish", "no upload", "no social scheduling"],
        },
    )
    visual_plan_decision = await ask(
        "Approve the blog visual element plan before generating images with Gemini Nano Banana 2.",
        key="approve_blog_visual_elements_plan",
        input={"visual_plan": visual_plan, "canonical": canonical, "side_effects": _zero_side_effects()},
        returns=ReviewDecision,
        approver=req.approver,
    )
    if visual_plan_decision.action != "approve":
        return {"status": "needs_visual_plan_changes", "visual_plan": visual_plan, "decision": visual_plan_decision, "side_effects": _zero_side_effects()}

    visuals = await agent(
        "generate_blog_visual_elements_with_gemini_nano_banana_2",
        prompt=(
            "Generate the approved blog visual elements using Gemini Nano Banana 2. Write local image files and a receipt "
            "with model, prompt, output path, and QA notes for each visual. If credentials or the model are unavailable, "
            "return a clear gap in the receipt instead of pretending images were generated. Do not publish, upload, post, "
            "schedule, or modify external services."
        ),
        input={"request": req, "canonical": canonical, "visual_plan": visual_plan, "model": req.visual_model},
        returns=VisualElementSet,
        tools=["file", "image_gen"],
        isolation="none",
        timeout=900,
        mock_output={
            "model": req.visual_model,
            "asset_id": "blogpost",
            "visuals": [
                {
                    "element_id": element.element_id if hasattr(element, "element_id") else element["element_id"],
                    "path": element.output_path if hasattr(element, "output_path") else element["output_path"],
                    "model": req.visual_model,
                    "prompt": element.prompt if hasattr(element, "prompt") else element["prompt"],
                    "receipt": "Mock local visual-generation receipt; real run should call Gemini Nano Banana 2 and store output metadata.",
                    "local_only": True,
                    "qa_notes": ["Demo mock output; no external publish/upload."],
                }
                for element in visual_plan.elements
            ],
            "receipt_summary": "Blog visual elements are part of the local content packet and remain behind publish/upload gates.",
            "side_effects": _zero_side_effects(),
        },
    )

    assets = await parallel([_generate_asset(req, canonical, spec, visuals) for spec in asset_plan.assets], limit=3)
    packet = await agent(
        "assemble_multiformat_content_packet",
        prompt="Assemble local paths, visual-generation receipts, unresolved risks, and exact side-effect boundaries for the content packet.",
        input={"request": req, "canonical": canonical, "asset_plan": asset_plan, "visual_plan": visual_plan, "visuals": visuals, "assets": assets},
        returns=dict,
        mock_output={
            "manifest_path": f"{req.output_dir}/manifest.md",
            "assets": [asset.path for asset in assets],
            "visuals": [visual.path for visual in visuals.visuals],
            "visual_model": req.visual_model,
            "side_effects": _zero_side_effects(),
            "next_gate": "approve_local_content_packet",
        },
    )
    packet_decision = await ask(
        "Approve the local content packet. This does not approve publishing, uploading, posting, scheduling, or PR merge.",
        key="approve_local_content_packet",
        input={"packet": packet, "assets": assets, "visuals": visuals, "side_effects": _zero_side_effects()},
        returns=PackageDecision,
        approver=req.approver,
    )

    return {
        "status": "local_packet_approved" if packet_decision.action == "approve_local_packet" else "needs_packet_changes",
        "topic": selected_topic,
        "research": research,
        "angle": selected_angle,
        "outline": outline,
        "canonical": canonical,
        "asset_plan": asset_plan,
        "visual_plan": visual_plan,
        "visuals": visuals,
        "assets": assets,
        "packet": packet,
        "final_decision": packet_decision,
        "side_effects": _zero_side_effects(),
    }


async def _draft_humanize_section(req: ContentStudioInput, outline: OutlinePacket, section: OutlineSection, index: int) -> SectionPacket:
    draft = await agent(
        f"draft_content_section_{index}",
        prompt="Draft only this outline section. Use the section job and evidence; do not draft the whole article.",
        input={"request": req, "outline": outline, "section": section, "index": index},
        returns=dict,
    )
    humanized = await agent(
        f"humanize_content_section_{index}",
        prompt=(
            "Humanize this section in Skylar's scar-tissue technical voice. Preserve claims and evidence; "
            "remove AI tells, slogan endings, and symmetric fake structure."
        ),
        input={"request": req, "section": section, "draft": draft},
        returns=dict,
    )
    return SectionPacket(
        section_id=section.id,
        title=section.title,
        draft_markdown=str(draft.get("markdown") or draft.get("draft") or ""),
        humanized_markdown=str(humanized.get("humanized_markdown") or humanized.get("markdown") or draft.get("markdown") or ""),
        humanizer_notes=[str(item) for item in humanized.get("humanizer_notes", [])],
    )


def _generate_asset(req: ContentStudioInput, canonical: CanonicalDraft, spec: AssetSpec, visuals: VisualElementSet):
    prompts = {
        "blogpost": "Adapt the canonical draft into the launch blogpost markdown. Preserve the approved thesis and evidence map, and place the generated Gemini Nano Banana 2 visual elements where they help the reader.",
        "slide_deck": "Adapt the canonical draft into a light demo-first slide deck. Use speaker notes; keep slides sparse.",
        "hyperframes_video": (
            "Create a HyperFrames video package plan: design.md, storyboard, source-file plan, check/render commands, "
            "and thumbnail QA criteria. Do not upload or publish the video."
        ),
    }
    return agent(
        f"generate_{spec.asset_id}_asset",
        prompt=prompts[spec.asset_id],
        input={"request": req, "canonical": canonical, "spec": spec, "visuals": visuals if spec.asset_id == "blogpost" else None},
        key_by=spec.asset_id,
        returns=GeneratedAsset,
        mock_output={
            "asset_id": spec.asset_id,
            "path": spec.output_path,
            "title": spec.title,
            "notes": "Local asset adapter output; external publishing/upload remains gated.",
            "supporting_visuals": [visual.path for visual in visuals.visuals] if spec.asset_id == "blogpost" else [],
            "local_only": True,
        },
    )


def _selected_topic(packet: TopicBrainstormPacket, decision: SelectionDecision) -> TopicCandidate | None:
    selected = decision.selected_id or decision.action
    for topic in packet.topics:
        if topic.id == selected:
            return topic
    return None


def _selected_angle(packet: AnglePacket, decision: SelectionDecision) -> AngleOption | None:
    selected = decision.selected_id or decision.action
    for angle in packet.options:
        if angle.id == selected:
            return angle
    return None


def _zero_side_effects() -> dict[str, bool]:
    return {
        "published": False,
        "posted": False,
        "scheduled": False,
        "uploaded": False,
        "emailed": False,
        "merged": False,
    }


if __name__ == "__main__":
    raise SystemExit(content_asset_lane_workflow.run())  # type: ignore[attr-defined]

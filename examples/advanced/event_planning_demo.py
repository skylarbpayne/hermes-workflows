from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hermes_workflows import agent, ask, parallel, workflow


@dataclass
class EventPlanningDemoInput:
    event_name: str = "Hermes Workflows Ops Preview"
    date_window: str = "July 2026"
    expected_attendees: int = 40
    budget_cap_usd: int = 2500
    requires_waivers: bool = True
    output_dir: str = "docs/presentation/2026-07-02/event-planning-lane"


@dataclass
class EventTimelineTask:
    task_id: str
    title: str
    due: str
    owner: str
    dependencies: list[str] = field(default_factory=list)
    channel_or_surface: str | None = None
    acceptance_receipt: str | None = None


@dataclass
class EventStrategy:
    recommended_attendee_count: int
    venue_criteria: list[str] = field(default_factory=list)
    promotion_channels: list[str] = field(default_factory=list)
    specific_invitees_or_segments: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


@dataclass
class EventArtifact:
    artifact_id: str
    path: str
    title: str
    summary: str
    external_actions_proposed: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    due: str | None = None


@dataclass
class EventOpsPacket:
    title: str
    summary: str
    strategy: EventStrategy
    planning_timeline: list[EventTimelineTask]
    artifacts: list[EventArtifact]
    budget_status: Literal["within_cap", "over_cap", "unknown"]
    external_actions: list[str]
    side_effect_ledger: dict[str, int]


@dataclass
class EventOpsDecision:
    action: Literal["approve_local_packet", "request_changes", "reject"]
    feedback: str | None = None


@workflow
async def event_planning_demo_workflow(inputs: EventPlanningDemoInput) -> dict:
    """Create an event ops packet with a full planning timeline and gated real-world actions."""

    req = inputs if isinstance(inputs, EventPlanningDemoInput) else EventPlanningDemoInput(**dict(inputs or {}))

    strategy = await agent(
        "shape_event_strategy",
        prompt=(
            "Shape the event before drafting artifacts. Recommend the audience size, venue criteria, promotion channels, "
            "and specific invitees or invitee segments. Use the event goal, date window, budget, and attendee target; "
            "flag questions that block exact venue/date decisions. Do not contact anyone or book anything."
        ),
        input=req,
        returns=EventStrategy,
        mock_output={
            "recommended_attendee_count": req.expected_attendees,
            "venue_criteria": [
                "capacity for target attendees plus 20% buffer",
                "reliable Wi-Fi and A/V",
                "easy transit/parking access",
                "layout that supports short demo plus discussion",
                f"total cost compatible with ${req.budget_cap_usd} cap",
            ],
            "promotion_channels": ["direct invite list", "community Slack/Discord", "LinkedIn/X post draft", "partner/newsletter mention"],
            "specific_invitees_or_segments": [
                "existing Hermes/agent-tooling collaborators",
                "local builders likely to give sharp product feedback",
                "operators who feel the email/event/code pain directly",
                "venue/community partners who can amplify without paid spend",
            ],
            "open_questions": ["exact date", "city/neighborhood", "must-have attendee names"],
        },
    )

    timeline = await agent(
        "build_event_planning_timeline",
        prompt=(
            "Create a planning timeline with due dates or T-minus due markers that covers the full event lifecycle: "
            "venue selection, budget, promotion, direct outreach, speaker/demo prep, comms, waivers if needed, logistics, "
            "run-of-show, day-of operations, follow-up, and post-event receipts. Include dependencies and acceptance receipts."
        ),
        input={"request": req, "strategy": strategy},
        returns=list[EventTimelineTask],
        mock_output=_mock_timeline(req),
    )

    artifacts = await parallel(
        [
            _draft_event_artifact(req, strategy, "venue_strategy", "Venue shortlist and logistics criteria", "T-6 weeks"),
            _draft_event_artifact(req, strategy, "promotion_plan", "Promotion plan and channel calendar", "T-5 weeks"),
            _draft_event_artifact(req, strategy, "direct_invite_list", "Specific invitee / segment reach-out plan", "T-4 weeks"),
            _draft_event_artifact(req, strategy, "participant_comms", "Participant comms drafts", "T-3 weeks"),
            _draft_event_artifact(req, strategy, "waiver_checklist", "Waiver checklist", "T-3 weeks"),
            _draft_event_artifact(req, strategy, "run_of_show", "Run of show", "T-1 week"),
            _draft_event_artifact(req, strategy, "budget_options", "Budget options", "T-6 weeks"),
            _draft_event_artifact(req, strategy, "post_event_followup", "Post-event follow-up and receipt plan", "T+2 days"),
        ],
        limit=5,
    )

    packet = await agent(
        "assemble_event_ops_packet",
        prompt=(
            "Assemble the event strategy, timeline, and artifacts into one approval packet. Make every proposed external "
            "action explicit. The packet should help decide venue type, attendee count, promotion surfaces, and named/direct "
            "outreach targets. Do not book venues, send comms, collect waivers, schedule calendars, or spend money."
        ),
        input={"request": req, "strategy": strategy, "timeline": timeline, "artifacts": artifacts},
        returns=EventOpsPacket,
        mock_output={
            "title": f"Event ops packet: {req.event_name}",
            "summary": f"Local-only planning packet for ~{strategy.recommended_attendee_count} attendees in {req.date_window}.",
            "strategy": strategy.__dict__ if hasattr(strategy, "__dict__") else strategy,
            "planning_timeline": [task.__dict__ if hasattr(task, "__dict__") else task for task in timeline],
            "artifacts": [artifact.__dict__ if hasattr(artifact, "__dict__") else artifact for artifact in artifacts],
            "budget_status": "within_cap",
            "external_actions": [
                "send direct invitations and follow-ups",
                "post promotion to selected channels",
                "send waiver request",
                "create calendar invite/hold",
                "book venue or pay deposit",
                "purchase catering/supplies",
            ],
            "side_effect_ledger": _zero_ledger(),
        },
    )

    decision = await ask(
        "Approve this local event planning packet. Approval does not send, schedule, book, buy, post, or collect signatures.",
        key="approve_event_ops_packet",
        input={"packet": packet, "side_effect_ledger": _zero_ledger()},
        returns=EventOpsDecision,
    )

    return {
        "status": "local_packet_approved" if decision.action == "approve_local_packet" else "needs_changes",
        "strategy": strategy,
        "timeline": timeline,
        "packet": packet,
        "decision": decision,
        "side_effect_ledger": _zero_ledger(),
    }


def _draft_event_artifact(req: EventPlanningDemoInput, strategy: EventStrategy, artifact_id: str, title: str, due: str):
    return agent(
        f"draft_{artifact_id}",
        prompt=(
            "Draft a local-only event planning artifact. Produce useful operational detail tied to the event timeline, "
            "venue/audience/promotion strategy, and acceptance receipts. Keep all real-world actions as proposals requiring "
            "a later human approval gate."
        ),
        input={"request": req, "strategy": strategy, "artifact_id": artifact_id, "title": title, "due": due},
        key_by=artifact_id,
        returns=EventArtifact,
        mock_output={
            "artifact_id": artifact_id,
            "path": f"{req.output_dir}/{artifact_id}.md",
            "title": title,
            "summary": f"Draft {title.lower()} for {req.event_name}; due {due}; local artifact only.",
            "external_actions_proposed": _external_actions_for(artifact_id),
            "risk_notes": ["No external commitment has been made."],
            "due": due,
        },
    )


def _mock_timeline(req: EventPlanningDemoInput) -> list[dict[str, object]]:
    return [
        {
            "task_id": "lock_event_goal_and_size",
            "title": "Lock event goal, target attendee count, and success receipt",
            "due": "T-8 weeks or immediately if inside the window",
            "owner": "organizer",
            "dependencies": [],
            "channel_or_surface": "planning note",
            "acceptance_receipt": "one-page brief with goal, target count, budget, and approval gates",
        },
        {
            "task_id": "venue_shortlist",
            "title": "Shortlist 3 venue options and identify preferred venue type",
            "due": "T-6 weeks",
            "owner": "organizer",
            "dependencies": ["lock_event_goal_and_size"],
            "channel_or_surface": "venue research artifact",
            "acceptance_receipt": "shortlist with capacity/cost/location/A-V fit and recommended pick",
        },
        {
            "task_id": "promotion_map",
            "title": "Choose promotion channels and direct outreach segments",
            "due": "T-5 weeks",
            "owner": "organizer",
            "dependencies": ["lock_event_goal_and_size"],
            "channel_or_surface": "promotion plan",
            "acceptance_receipt": "channel calendar plus specific invitee/segment list",
        },
        {
            "task_id": "venue_hold_or_booking_gate",
            "title": "Approve venue hold/booking/deposit if needed",
            "due": "T-5 weeks",
            "owner": "Skylar approval",
            "dependencies": ["venue_shortlist"],
            "channel_or_surface": "Review Queue",
            "acceptance_receipt": "explicit booking/deposit approval or rejection",
        },
        {
            "task_id": "send_invitations_gate",
            "title": "Approve and send initial invitations/promotional posts",
            "due": "T-4 weeks",
            "owner": "Skylar approval + organizer",
            "dependencies": ["promotion_map", "venue_hold_or_booking_gate"],
            "channel_or_surface": "email/social/community channels",
            "acceptance_receipt": "approved copy variants and send/post receipt",
        },
        {
            "task_id": "logistics_and_waivers",
            "title": "Finalize logistics, waivers, supplies, and staffing",
            "due": "T-2 weeks",
            "owner": "organizer",
            "dependencies": ["venue_hold_or_booking_gate"],
            "channel_or_surface": "ops checklist",
            "acceptance_receipt": "day-of checklist with owners and outstanding risks",
        },
        {
            "task_id": "reminders_and_run_of_show",
            "title": "Send reminders and lock run-of-show",
            "due": "T-1 week / T-1 day",
            "owner": "organizer",
            "dependencies": ["send_invitations_gate", "logistics_and_waivers"],
            "channel_or_surface": "participant comms + run-of-show",
            "acceptance_receipt": "reminder receipts and final run-of-show",
        },
        {
            "task_id": "post_event_followup",
            "title": "Send follow-up, collect feedback, archive receipts",
            "due": "T+2 days",
            "owner": "organizer",
            "dependencies": ["event_complete"],
            "channel_or_surface": "email/forms/notes",
            "acceptance_receipt": "follow-up sent, feedback summarized, next actions captured",
        },
    ]


def _external_actions_for(artifact_id: str) -> list[str]:
    return {
        "participant_comms": ["send invitation email", "send reminder email", "send follow-up email"],
        "waiver_checklist": ["send waiver request", "collect signatures"],
        "venue_strategy": ["book venue", "pay deposit", "confirm A/V"],
        "run_of_show": ["assign staff", "publish schedule"],
        "budget_options": ["purchase supplies", "pay vendor deposit"],
        "promotion_plan": ["post on social/community channels", "request partner amplification"],
        "direct_invite_list": ["send direct invites", "send follow-ups"],
        "post_event_followup": ["send thank-you email", "send feedback form"],
    }.get(artifact_id, [])


def _zero_ledger() -> dict[str, int]:
    return {
        "emails_sent": 0,
        "calendar_mutations": 0,
        "venue_bookings": 0,
        "payments_or_purchases": 0,
        "waiver_requests_sent": 0,
        "social_or_community_posts": 0,
        "external_http_requests": 0,
    }


if __name__ == "__main__":
    raise SystemExit(event_planning_demo_workflow.run())  # type: ignore[attr-defined]

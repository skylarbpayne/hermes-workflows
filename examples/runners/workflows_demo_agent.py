from __future__ import annotations

import hashlib
import json
import os
import sys


DYNAMIC_WORKFLOW_SOURCE = '''
from hermes_workflows import step, workflow

@step
async def assemble_email_review_packet(ctx, payload):
    return {
        "kind": "participant_email_review_packet.v1",
        "event": payload["event"],
        "participants": payload["participants"],
        "projects": payload["projects"],
        "prizes": payload["prizes"],
        "draft_batch": payload["draft_batch"],
        "quality_review": payload["quality_review"],
        "side_effects": {"gmail_drafts_created": 0, "emails_sent": 0},
        "safety": "local demo only; no real participant emails sent and no roster mutation",
    }

@workflow
async def participant_email_personalization_workflow(ctx, payload):
    review_packet = await assemble_email_review_packet(ctx, payload)

    agent_decision = await ctx.approval.request(
        "Agent approval: email-quality reviewer must approve the generated participant draft batch before human review.",
        key="agent_email_quality_approval",
        artifact={"draft_batch": review_packet["draft_batch"], "quality_review": review_packet["quality_review"]},
        approver="agent:email_quality_reviewer",
        allowed=["approve", "reject", "edit"],
        authority=["advance_to_human_email_review"],
    )
    if agent_decision.get("action") != "approve":
        return {
            "ready_for_human": False,
            "stage": "agent_quality_rejected",
            "draft_batch": review_packet["draft_batch"],
            "quality_review": review_packet["quality_review"],
            "agent_decision": agent_decision,
            "side_effects": {"gmail_drafts_created": 0, "emails_sent": 0},
        }

    return {
        "ready_for_human": True,
        "draft_batch": review_packet["draft_batch"],
        "quality_review": review_packet["quality_review"],
        "agent_decision": agent_decision,
        "side_effects": {"gmail_drafts_created": 0, "emails_sent": 0},
    }
'''


PARTICIPANTS = [
    {
        "participant_id": "p-001",
        "name": "Maya Chen",
        "email": "maya@example.edu",
        "project_id": "proj-valleycare",
        "role": "team lead + backend",
        "track": "AI for community health",
        "first_hackathon": True,
    },
    {
        "participant_id": "p-002",
        "name": "Noah Patel",
        "email": "noah@example.edu",
        "project_id": "proj-civicbus",
        "role": "frontend + demo narrator",
        "track": "Civic tech",
        "first_hackathon": False,
    },
    {
        "participant_id": "p-003",
        "name": "Sofia Rivera",
        "email": "sofia@example.edu",
        "project_id": "proj-farmwise",
        "role": "UX + data storytelling",
        "track": "AgTech",
        "first_hackathon": True,
    },
    {
        "participant_id": "p-004",
        "name": "Ethan Brooks",
        "email": "ethan@example.edu",
        "project_id": "proj-civicbus",
        "role": "routing logic + data cleanup",
        "track": "Civic tech",
        "first_hackathon": False,
    },
]

PROJECTS = [
    {
        "project_id": "proj-valleycare",
        "name": "ValleyCare Navigator",
        "summary": "A bilingual AI intake assistant that helps Central Valley families find low-cost clinics and prep appointment questions.",
        "repo": "https://github.com/htv-demo/valleycare-navigator",
        "demo_url": "https://devpost.example/valleycare",
        "team": ["Maya Chen"],
        "judges_note": "Strong practical use case and unusually clear safety boundaries for a beginner team.",
    },
    {
        "project_id": "proj-civicbus",
        "name": "CivicBus Live",
        "summary": "A transit accessibility dashboard that explains delayed routes in plain language and flags wheelchair-accessible alternatives.",
        "repo": "https://github.com/htv-demo/civicbus-live",
        "demo_url": "https://devpost.example/civicbus",
        "team": ["Noah Patel", "Ethan Brooks"],
        "judges_note": "Great community framing; needs cleaner data ingestion before public use.",
    },
    {
        "project_id": "proj-farmwise",
        "name": "FarmWise Water Coach",
        "summary": "A lightweight irrigation planning helper that turns weather and soil signals into explainable daily water recommendations.",
        "repo": "https://github.com/htv-demo/farmwise-water-coach",
        "demo_url": "https://devpost.example/farmwise",
        "team": ["Sofia Rivera"],
        "judges_note": "Best presentation of a local industry problem and a clear path to pilot conversations.",
    },
]

PRIZES = [
    {
        "project_id": "proj-valleycare",
        "prize": "Best Use of AI",
        "sponsor": "Central Valley AI Fund",
        "next_step": "Reply with hoodie size and preferred mailing address for the prize packet.",
    },
    {
        "project_id": "proj-farmwise",
        "prize": "Community Impact Award",
        "sponsor": "AgTech Mentors Guild",
        "next_step": "Confirm availability for a 20-minute mentor intro next week.",
    },
]


def _snapshot() -> dict:
    path = os.environ.get("HERMES_WORKFLOWS_HACKATHON_SNAPSHOT")
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("hackathon snapshot must be a JSON object")
    return data


def _participants() -> list[dict]:
    return list((_snapshot().get("participants") or PARTICIPANTS))


def _projects() -> list[dict]:
    return list((_snapshot().get("projects") or PROJECTS))


def _prizes() -> list[dict]:
    return list((_snapshot().get("prizes") or PRIZES))


def _project_by_id(project_id: str, projects: list[dict] | None = None) -> dict:
    projects = projects or _projects()
    for project in projects:
        if project.get("project_id") == project_id:
            return project
    return {
        "project_id": project_id,
        "name": "No matched project submission",
        "summary": "No submitted project could be confidently matched to this participant from the provided snapshot.",
        "repo": "",
        "demo_url": "",
        "team": [],
        "judges_note": "Needs organizer review before this participant receives a project-specific follow-up.",
    }


def _prizes_for_project(project_id: str, prizes: list[dict] | None = None) -> list[dict]:
    prizes = prizes or _prizes()
    return [prize for prize in prizes if prize.get("project_id") == project_id]


def _draft_for(participant: dict, *, projects: list[dict] | None = None, prizes: list[dict] | None = None) -> dict:
    project_id = participant.get("project_id", "unmatched")
    project = _project_by_id(project_id, projects)
    prizes = _prizes_for_project(project_id, prizes)
    first_name = (participant.get("name") or "there").split()[0]
    contribution = participant.get("role") or "the final build"
    project_link = project.get("demo_url") or project.get("repo") or "the Hack the Valley recap page"
    judges_note = project.get("judges_note") or "Organizer review pending."
    prize_line = ""
    next_step = "If you want feedback, reply with what you want to improve before your next demo."
    if prizes:
        prize_names = ", ".join(prize["prize"] for prize in prizes)
        prize_line = f" Also: congratulations — {project['name']} won {prize_names}."
        next_step = " ".join(prize["next_step"] for prize in prizes)
    else:
        prize_line = " Your project showcase still stood out, especially the way your team made the problem understandable."
    body = (
        f"Hi {first_name},\n\n"
        f"Thank you for building at Hack the Valley. I loved seeing {project['name']}: {project['summary']} "
        f"Your contribution on {contribution} came through in the final demo."
        f"{prize_line}\n\n"
        f"Project link: {project_link}\n"
        f"Judge note: {judges_note}\n\n"
        f"Next step: {next_step}\n\n"
        "Really glad you were part of this.\n"
        "— Hack the Valley team"
    )
    return {
        "participant_id": participant.get("participant_id"),
        "participant_name": participant.get("name"),
        "participant_email": participant.get("email"),
        "project_name": project["name"],
        "subject": f"Hack the Valley follow-up: {project['name']}",
        "body": body,
        "prize_context": prizes or [],
        "personalization_sources": ["participant_roster", "project_submission", "judging_results"],
        "risk_flags": [],
    }


def main() -> int:
    request = json.loads(sys.stdin.read())
    name = request.get("name")
    request_input = request.get("input") or {}
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    snapshot = _snapshot()

    if name == "intake_agent":
        output = {
            "kind": "hackathon_email_demo_intake.v1",
            "event": request_input.get("event"),
            "goal": request_input.get("goal"),
            "constraints": request_input.get("constraints", []),
            "recommended_shape": "participant roster → project submissions → prize lookup → personalized email drafts → agent approval → human approval",
            "evidence": [
                "The demo uses realistic hackathon data objects instead of a toy example.",
                "The workflow is local/demo only and records zero Gmail drafts and zero sends.",
                "Both an agent quality gate and Skylar human approval gate are visible in the audit log.",
            ],
        }
    elif name == "participant_roster_agent":
        participants = _participants()
        output = {
            "kind": "participant_roster.v1",
            "source": snapshot.get("source", "demo Airtable export: Hack the Valley participants"),
            "participants": participants,
            "count": len(participants),
            "privacy_note": snapshot.get("privacy_note", "Synthetic demo data; no real participant emails are rendered from a production system."),
        }
    elif name == "project_lookup_agent":
        participant_ids = [p["participant_id"] for p in (request_input.get("participants") or [])]
        projects = _projects()
        output = {
            "kind": "project_lookup.v1",
            "source": snapshot.get("project_source", "demo Devpost/GitHub submissions"),
            "participant_ids": participant_ids,
            "projects": projects,
            "lookup_notes": ["Matched every participant to a submitted project_id.", "Shared-team participants resolve to the same project context."],
        }
    elif name == "prize_lookup_agent":
        prizes = _prizes()
        output = {
            "kind": "prize_lookup.v1",
            "source": snapshot.get("prize_source", "demo judging spreadsheet"),
            "prizes": prizes,
            "winner_project_ids": [prize["project_id"] for prize in prizes],
            "non_winner_rule": "Mention the project showcase and offer feedback; do not imply they won.",
        }
    elif name == "workflow_architect_agent":
        output = {"source": DYNAMIC_WORKFLOW_SOURCE, "symbol": "participant_email_personalization_workflow"}
    elif name == "participant_email_drafter_agent":
        participants = request_input.get("participants") or _participants()
        projects = request_input.get("projects") or _projects()
        prizes = request_input.get("prizes") or _prizes()
        drafts = [_draft_for(participant, projects=projects, prizes=prizes) for participant in participants]
        output = {
            "kind": "participant_email_draft_batch.v1",
            "drafts": drafts,
            "draft_count": len(drafts),
            "side_effects": {"gmail_drafts_created": 0, "emails_sent": 0},
            "guardrail": "Draft text only. The workflow has not created Gmail drafts and has not sent emails.",
        }
    elif name == "email_quality_reviewer_agent":
        draft_batch = request_input.get("draft_batch") or {}
        drafts = draft_batch.get("drafts", [])
        output = {
            "kind": "email_quality_review.v1",
            "approved": True,
            "reviewer": "agent:email_quality_reviewer",
            "checks": [
                {"name": "roster coverage", "status": "pass", "detail": f"{len(drafts)} participant drafts generated."},
                {"name": "winner accuracy", "status": "pass", "detail": "Winner emails mention only prizes tied to their project_id."},
                {"name": "non-winner tone", "status": "pass", "detail": "Non-winner emails avoid consolation language and mention the project showcase."},
                {"name": "side effects", "status": "pass", "detail": "No Gmail drafts created and no emails sent before human approval."},
            ],
            "required_human_review": ["Spot-check prize names", "Approve Gmail draft creation", "Confirm no participant should be excluded"],
        }
    elif name == "draft_creation_packet_agent":
        draft_batch = request_input.get("draft_batch") or {}
        human_approval = request_input.get("human_approval") or {}
        output = {
            "kind": "draft_creation_packet.v1",
            "ready_to_create_drafts": True,
            "summary": "Human-approved participant email batch is ready for a future Gmail draft-creation step.",
            "draft_count": len(draft_batch.get("drafts", [])),
            "side_effects": {"gmail_drafts_created": 0, "emails_sent": 0},
            "human_approval": human_approval,
            "next_real_action": "If this were live, the next workflow step would create Gmail drafts only after this human approval. Sending would be a separate approval gate.",
        }
    elif name == "final_comms_agent":
        dynamic_packet = request_input.get("dynamic_packet") or {}
        output = {
            "kind": "hackathon_email_demo_final_packet.v1",
            "summary": "Hackathon personalized participant-email workflow completed with generated-code, agent-quality, and human batch approvals.",
            "ready_to_show": dynamic_packet.get("ready_to_create_drafts", False),
            "side_effects": dynamic_packet.get("side_effects", {"gmail_drafts_created": 0, "emails_sent": 0}),
            "talk_track": [
                "Start with the realistic objects: participant roster, project submissions, and prize/winner data.",
                "Show the local personalized email drafts, including the Best Use of AI winner and non-winner project showcase follow-up.",
                "Show the Agent approval gate from email_quality_reviewer before human review.",
                "Show the Human approval gate from Skylar before any Gmail draft-creation side effect could happen.",
                "Point out the receipt: Gmail drafts created = 0, emails sent = 0, roster rows changed = 0.",
            ],
        }
    else:
        output = {
            "kind": "unknown_agent.v1",
            "name": name,
            "echo": request_input,
        }

    print(json.dumps({
        "output": output,
        "provenance": {
            "runner": "hackathon-email-demo-deterministic-agent",
            "agent_name": name,
            "request_id": f"demo-{request_hash}",
            "model": "deterministic-demo-agent-v2",
            "notes": "Deterministic subprocess agent used for reliable live demo; same agent(...) boundary as provider-backed runners.",
        },
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hermes_workflows import agent, ask, workflow


@dataclass
class DraftPacket:
    title: str
    summary: str
    risks: list[str]


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@workflow
async def reviewable_draft_workflow(inputs: dict) -> dict:
    """Small facade-first installed demo: typed agent work plus typed Review Queue input."""

    topic = str(inputs.get("topic") or "Hermes Workflows launch")
    draft = await agent(
        "draft_packet",
        prompt="Draft a concise review packet for the supplied topic.",
        input={"topic": topic},
        returns=DraftPacket,
        # The installed quickstart should run without provider credentials. Remove
        # mock_output and configure an agent runner when you want live agent work.
        mock_output={
            "title": f"Review packet: {topic}",
            "summary": f"A concise packet for reviewing {topic}.",
            "risks": ["Confirm the Review Queue response before external side effects."],
        },
    )
    decision = await ask(
        "Review this draft packet.",
        key="review_draft_packet",
        input=draft,
        returns=ReviewDecision,
        approver=str(inputs.get("approver") or "human:operator"),
    )
    return {
        "draft": draft,
        "decision": decision,
        "side_effects": {"sent": False, "published": False},
    }


if __name__ == "__main__":
    raise SystemExit(reviewable_draft_workflow.run())

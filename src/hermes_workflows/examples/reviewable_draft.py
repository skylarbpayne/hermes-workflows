from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from hermes_workflows import agent, ask, workflow


@dataclass
class ReviewableDraftInput:
    topic: str = "Hermes Workflows launch"
    approver: str = "human:operator"

    @classmethod
    def from_value(cls, value: object) -> "ReviewableDraftInput":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                topic=str(value.get("topic") or "Hermes Workflows launch"),
                approver=str(value.get("approver") or "human:operator"),
            )
        return cls()


@dataclass
class DraftPacketRequest:
    topic: str


@dataclass
class DraftPacket:
    title: str
    summary: str
    risks: list[str]


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@dataclass
class SideEffects:
    sent: bool = False
    published: bool = False


@dataclass
class ReviewableDraftResult:
    draft: DraftPacket
    decision: ReviewDecision
    side_effects: SideEffects


@workflow
async def reviewable_draft_workflow(inputs: ReviewableDraftInput) -> ReviewableDraftResult:
    """Small facade-first installed demo: typed agent work plus typed Review Queue input."""

    request = ReviewableDraftInput.from_value(inputs)
    draft = await agent(
        "draft_packet",
        prompt="Draft a concise review packet for the supplied topic.",
        input=DraftPacketRequest(topic=request.topic),
        returns=DraftPacket,
        # The installed quickstart should run without provider credentials. Remove
        # mock_output and configure an agent runner when you want live agent work.
        mock_output={
            "title": f"Review packet: {request.topic}",
            "summary": f"A concise packet for reviewing {request.topic}.",
            "risks": ["Confirm the Review Queue response before external side effects."],
        },
    )
    decision = await ask(
        "Review this draft packet.",
        key="review_draft_packet",
        input=draft,
        returns=ReviewDecision,
        approver=request.approver,
    )
    return ReviewableDraftResult(
        draft=draft,
        decision=decision,
        side_effects=SideEffects(),
    )


if __name__ == "__main__":
    raise SystemExit(reviewable_draft_workflow.run())

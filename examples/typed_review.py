from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hermes_workflows import MarkdownArtifact, ask, workflow


@dataclass(frozen=True)
class DraftInput:
    topic: str = "Hermes Workflows"
    audience: str = "workflow authors"


@dataclass(frozen=True)
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@workflow
async def typed_review_workflow(inputs: DraftInput) -> dict:
    draft = MarkdownArtifact(
        title="Draft brief",
        markdown=f"# {inputs.topic}\n\nAudience: {inputs.audience}\n",
        source={"example": "typed_review"},
    )
    decision = await ask(
        "Review the draft brief",
        key="review_draft_brief",
        input=draft,
        returns=ReviewDecision,
    )
    return {"action": decision.action, "feedback": decision.feedback}


if __name__ == "__main__":
    raise SystemExit(typed_review_workflow.run())  # type: ignore[attr-defined]

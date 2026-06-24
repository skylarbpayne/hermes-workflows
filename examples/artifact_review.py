from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hermes_workflows import JsonArtifact, ask, workflow


@dataclass(frozen=True)
class ArtifactReviewDecision:
    action: Literal["approve", "request_changes"]
    note: str | None = None


@workflow
async def artifact_review_workflow(inputs: dict) -> dict:
    artifact = JsonArtifact(
        title="Launch checklist",
        data={"checks": ["typed input", "artifact", "review queue"], "ready": True},
        source={"example": "artifact_review"},
    )
    decision = await ask(
        "Review the launch checklist artifact",
        key="review_launch_checklist",
        input=artifact,
        returns=ArtifactReviewDecision,
    )
    return {"action": decision.action, "note": decision.note}


if __name__ == "__main__":
    raise SystemExit(artifact_review_workflow.run())  # type: ignore[attr-defined]

"""Canonical typed quickstart.

Run with serialized input:

    uv run python typed_quickstart.py --id wf_typed_quickstart \
        --input-json '{"change":"Expose typed workflow contracts."}'

Exact start JSON:

    {"error":null,"result":null,"status":"running","waiting_on":null,"workflow_id":"wf_typed_quickstart"}

After a runner processes the credential-free mock agent call, exact waiting JSON:

    {"error":null,"result":null,"status":"waiting","waiting_on":"signal:operator.response:review_release_note","workflow_id":"wf_typed_quickstart"}

After an ``approve`` response with feedback ``Ready to ship.``, exact result JSON:

    {"error":null,"result":{"decision":{"action":"approve","feedback":"Ready to ship."},"draft":{"text":"Release note: Expose typed workflow contracts."},"side_effects":{"published":false}},"status":"completed","waiting_on":null,"workflow_id":"wf_typed_quickstart"}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from hermes_workflows import agent, ask, workflow


@dataclass(frozen=True)
class ReleaseNoteInput:
    change: str


@dataclass(frozen=True)
class Draft:
    text: str


@dataclass(frozen=True)
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: Optional[str] = None


@dataclass(frozen=True)
class SideEffects:
    published: bool = False


@dataclass(frozen=True)
class ReleaseNoteResult:
    draft: Draft
    decision: ReviewDecision
    side_effects: SideEffects


@workflow
async def release_note_workflow(inputs: ReleaseNoteInput) -> ReleaseNoteResult:
    draft = await agent(
        "writer",
        prompt="Draft a release note for the supplied change.",
        input=inputs,
        returns=Draft,
        # The canonical quickstart must reach typed review without credentials.
        mock_output={"text": f"Release note: {inputs.change}"},
    )
    decision = await ask(
        "Review this release note.",
        key="review_release_note",
        input=draft,
        returns=ReviewDecision,
    )
    return ReleaseNoteResult(
        draft=draft,
        decision=decision,
        side_effects=SideEffects(),
    )


if __name__ == "__main__":
    raise SystemExit(release_note_workflow.run())  # type: ignore[attr-defined]

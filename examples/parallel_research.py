from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hermes_workflows import agent, ask, parallel, workflow


@dataclass
class ResearchNote:
    source: str
    finding: str
    risk: str


@dataclass
class ResearchDecision:
    action: Literal["use", "revise"]
    feedback: str | None = None


TOPICS = ["authoring API", "Review Queue", "Workflow Worker"]


@workflow
async def parallel_research_workflow(inputs: dict) -> dict:
    """Fan out independent agent calls, then ask for one aggregate review."""

    topics = list(inputs.get("topics") or TOPICS)
    notes = await parallel(
        [
            agent(
                "research_topic",
                prompt="Research this launch topic and return one finding plus one risk.",
                input={"topic": topic},
                key_by=topic,
                returns=ResearchNote,
                mock_output={
                    "source": topic,
                    "finding": f"{topic} needs a crisp launch example.",
                    "risk": "If the example teaches internals first, the public model feels harder than it is.",
                },
            )
            for topic in topics
        ],
        limit=3,
    )
    decision = await ask(
        "Pick whether this research packet is ready to use.",
        key="review_research_packet",
        input={"notes": notes},
        returns=ResearchDecision,
    )
    return {"notes": notes, "decision": decision}


if __name__ == "__main__":
    raise SystemExit(parallel_research_workflow.run())

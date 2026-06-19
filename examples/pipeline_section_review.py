from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hermes_workflows import agent, ask, pipeline, workflow


@dataclass
class SectionDraft:
    section_id: str
    title: str
    text: str


@dataclass
class CheckedSection:
    section_id: str
    title: str
    text: str
    caveat: str


@dataclass
class SectionReview:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


def draft_section(section: dict) -> object:
    return agent(
        "draft_section",
        prompt="Draft this section in a concise launch-doc voice.",
        input=section,
        key_by=section["id"],
        returns=SectionDraft,
        mock_output={"section_id": section["id"], "title": section["title"], "text": f"Draft for {section['title']}."},
    )


def fact_check_section(draft: SectionDraft) -> object:
    return agent(
        "fact_check_section",
        prompt="Fact-check this section and return a caveat if needed.",
        input=draft,
        key_by=draft.section_id,
        returns=CheckedSection,
        mock_output={
            "section_id": draft.section_id,
            "title": draft.title,
            "text": draft.text,
            "caveat": "Uses deterministic mock output; configure an agent runner for live fact-checking.",
        },
    )


def review_section(section: CheckedSection) -> object:
    return ask(
        f"Review section: {section.title}",
        key=f"review_section_{section.section_id}",
        input=section,
        returns=SectionReview,
    )


@workflow
async def pipeline_section_review_workflow(inputs: dict) -> dict:
    """Run sections through draft -> fact-check -> human review stages."""

    sections = list(
        inputs.get("sections")
        or [
            {"id": "api", "title": "Authoring API"},
            {"id": "worker", "title": "Workflow Worker"},
        ]
    )
    reviews = await pipeline(sections, draft_section, fact_check_section, review_section, limit=2)
    return {"reviews": reviews}


if __name__ == "__main__":
    raise SystemExit(pipeline_section_review_workflow.run())

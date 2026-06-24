from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hermes_workflows import agent, prompt_file, workflow


@dataclass(frozen=True)
class PromptFileInput:
    topic: str = "durable workflow prompts"


@workflow
async def prompt_file_workflow(inputs: PromptFileInput) -> dict:
    template = prompt_file(Path(__file__).with_name("prompts") / "starter_research.md")
    rendered = template.render(topic=inputs.topic)
    return await agent(
        "research_topic",
        prompt=rendered,
        input={"topic": inputs.topic},
        mock_output={"summary": f"Prompt rendered for {inputs.topic}"},
    )


if __name__ == "__main__":
    raise SystemExit(prompt_file_workflow.run())  # type: ignore[attr-defined]

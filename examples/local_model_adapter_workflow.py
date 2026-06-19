from __future__ import annotations

from dataclasses import dataclass

from hermes_workflows import agent, workflow


@dataclass
class LocalSummary:
    summary: str
    model: str


@workflow
async def local_model_adapter_workflow(inputs: dict) -> dict:
    """Show the `agent(..., model=...)` shape used with a configured local/Hermes CLI runner."""

    model = str(inputs.get("model") or "local/demo-model")
    packet = inputs.get("packet") or {"change": "Launch-facing docs and examples polish"}
    summary = await agent(
        "local_summarizer",
        prompt="Summarize this packet as JSON for a launch review.",
        input={"packet": packet},
        model=model,
        returns=LocalSummary,
        # Keep this example runnable in CI/docs without provider credentials. Pass
        # {"mock_output": null} and configure --agent-model-arg for a live model.
        mock_output=inputs.get("mock_output", {"summary": "Local runner wiring is configured.", "model": model}),
    )
    return {"summary": summary, "requested_model": model}


if __name__ == "__main__":
    raise SystemExit(local_model_adapter_workflow.run())

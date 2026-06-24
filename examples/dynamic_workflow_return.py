from __future__ import annotations

from hermes_workflows import Workflow, agent, map_workflow, workflow


GENERATED_ITEM_PROCESSOR_SOURCE = '''
from hermes_workflows import step, workflow

@step
async def normalize_launch_item(item):
    title = str(item.get("title", "")).strip()
    owner = str(item.get("owner", "unassigned")).strip() or "unassigned"
    risk = str(item.get("risk", "unknown")).strip() or "unknown"
    return {
        "id": item["id"],
        "title": title,
        "slug": title.lower().replace(" ", "-"),
        "owner": owner,
        "launch_risk": risk,
    }

@workflow
async def process_launch_item(item):
    normalized = await normalize_launch_item(item)
    return {
        "id": normalized["id"],
        "summary": f"{normalized['title']} -> {normalized['owner']} ({normalized['launch_risk']})",
        "normalized": normalized,
    }
'''


DEFAULT_ITEMS = [
    {
        "id": "dynamic-examples",
        "title": "Dynamic workflow examples",
        "owner": "docs",
        "risk": "Readers may think workflows are static scripts only.",
    },
    {
        "id": "subworkflow-ui",
        "title": "Subworkflow UI inspection",
        "owner": "dashboard",
        "risk": "Child runs are hard to understand if they are just flat event rows.",
    },
]


@workflow
async def dynamic_workflow_return_workflow(inputs: dict) -> dict:
    """Generate a workflow at runtime, then run it as durable child workflows.

    This is the compact launch example for dynamic workflows: an agent returns a
    `Workflow` value, the parent calls that value for each item, and the runtime
    records each call as a child workflow with its own events, waits, and result.
    """

    items = list(inputs.get("items") or DEFAULT_ITEMS)
    processor = await agent(
        "build_item_processor",
        prompt="Write a small Hermes workflow that normalizes and summarizes one launch item.",
        input={
            "item_count": len(items),
            "required_output": "{id, summary, normalized}",
        },
        returns=Workflow,
        # Deterministic so docs, tests, and CI do not need provider credentials.
        # With a live runner, omit mock_output and the returned Python source will
        # be validated, stored, and run through the same child-workflow path.
        mock_output={"source": GENERATED_ITEM_PROCESSOR_SOURCE, "symbol": "process_launch_item"},
    )

    processed = await map_workflow(
        processor,
        items,
        key_fn=lambda item: item["id"],
        concurrency=4,
    )

    return {
        "generated_workflow": {
            "symbol": processor.symbol,
            "source_sha256": processor.source_sha256,
        },
        "processed": processed,
    }


if __name__ == "__main__":
    raise SystemExit(getattr(dynamic_workflow_return_workflow, "run")())

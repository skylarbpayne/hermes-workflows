from __future__ import annotations

from hermes_workflows import Workflow, agent, step, workflow


GENERATED_PROCESSOR_SOURCE = '''
from hermes_workflows import step, workflow

@step
async def process_label(ctx, item):
    return {"item_id": item["id"], "label": item["label"].upper()}

@workflow
async def process_item(ctx, item):
    processed = await process_label(ctx, item)
    return {"processed": processed}
'''


WAITING_CHILD_SOURCE = '''
from hermes_workflows import workflow

@workflow
async def waiting_child(ctx, item):
    payload = await ctx.wait_for("dynamic.ready", key=item["id"])
    return {"payload": payload}
'''


@step
async def producing_items(ctx, inputs):
    return inputs["items"]


@workflow
async def dynamic_item_workflow_example(ctx, inputs):
    items = await producing_items(ctx, inputs)

    processor = await agent(
        "build_item_workflow",
        prompt="Write executable Python defining a @workflow named process_item for one item.",
        returns=Workflow,
        mock_output={"source": GENERATED_PROCESSOR_SOURCE, "symbol": "process_item"},
    )

    results = []
    for item in items:
        results.append(await processor(item, key=item["id"]))

    return {"items": results}


@workflow
async def dynamic_item_map_example(ctx, inputs):
    items = await producing_items(ctx, inputs)

    processor = await agent(
        "build_item_workflow",
        prompt="Write executable Python defining a @workflow named process_item for one item.",
        returns=Workflow,
        mock_output={"source": GENERATED_PROCESSOR_SOURCE, "symbol": "process_item"},
    )

    return {
        "items": await ctx.map_workflow(
            processor,
            items,
            key_fn=lambda item: item["id"],
            concurrency=4,
        )
    }


@workflow
async def dynamic_waiting_child_pipeline(ctx, inputs):
    child = await agent(
        "build_waiting_child",
        prompt="Write executable Python defining a @workflow named waiting_child that waits for a signal.",
        returns=Workflow,
        mock_output={"source": WAITING_CHILD_SOURCE, "symbol": "waiting_child"},
    )

    return await child(inputs["item"], key=inputs["item"]["id"])

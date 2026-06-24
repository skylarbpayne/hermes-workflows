from __future__ import annotations

from hermes_workflows import approve, step, workflow


@step
async def draft_trip_options(inputs):
    destination = inputs.get("destination", "NYC")
    return {
        "destination": destination,
        "options": [
            f"Fly to {destination} and stay near the main venue",
            f"Take the cheaper route to {destination} and keep one flexible day",
        ],
    }


@workflow
async def trip_planning_workflow(inputs):
    """Small installed demo: draft options, wait for human approval, return receipt."""

    options = await draft_trip_options(inputs)
    decision = await approve(
        "Approve this trip plan?",
        key="approve_trip_plan",
        artifact=options,
        approver=inputs.get("approver", "human:operator"),
        allowed=["approve", "reject"],
        authority=["book_travel"],
    )
    if decision.get("action") != "approve":
        return {"approved": False, "decision": decision, "options": options}
    return {
        "approved": True,
        "approved_by": decision.get("by"),
        "approval_source": decision.get("source"),
        "options": options,
    }

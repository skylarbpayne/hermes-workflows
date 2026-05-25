from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_workflows import WorkflowEngine, step, workflow


@step
async def collect_trip_context(ctx, inputs):
    return {
        "destination": inputs["destination"],
        "constraints": ["protect deep work", "avoid red-eye flights"],
        "gesture_required": True,
    }


@step
async def draft_trip_plan(ctx, context):
    return {
        "summary": f"Draft plan for {context['destination']}",
        "hotel_bias": "walkable boutique hotel",
        "jacqueline_gesture": "book one calm dinner and buy a small gift before travel day",
        "risks": ["do not book or spend without human approval"],
    }


@step
async def package_after_approval(ctx, plan, decision):
    return {
        "ready_for_booking_prep": True,
        "approved_by": decision["by"],
        "plan": plan,
        "next_action": "prepare booking checklist; still do not purchase without a separate authority gate",
    }


@workflow
async def first_real_trip_workflow(ctx, inputs):
    context = await collect_trip_context(ctx, inputs)
    plan = await draft_trip_plan(ctx, context)
    decision = await ctx.approval.request(
        "Approve this trip plan for packaging?",
        key="approve_trip_plan",
        artifact=plan,
        approver="human:skylar",
        allowed=["approve", "reject", "edit", "rerun"],
        authority=["schedule_external", "spend_money"],
    )
    if decision["action"] != "approve":
        return {"ready_for_booking_prep": False, "decision": decision, "plan": plan}
    return await package_after_approval(ctx, plan, decision)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "workflow.sqlite"
        engine = WorkflowEngine(db)

        result = engine.run_until_idle(
            first_real_trip_workflow,
            {"destination": "NYC"},
            workflow_id="wf_first_real_trip",
        )
        print("after run_until_idle", result)
        print("approval command", [c for c in engine.pending_commands("wf_first_real_trip") if c["type"] == "notify_approval"])

        # New engine instance simulates a process restart while waiting for human approval.
        engine = WorkflowEngine(db)
        result = engine.signal(
            "wf_first_real_trip",
            "approval.decision",
            key="approve_trip_plan",
            payload={"action": "approve", "by": "skylar"},
            idempotency_key="demo-approval-message-1",
        )
        print("after approval signal", result)
        print("events", [event["type"] for event in engine.events("wf_first_real_trip")])


if __name__ == "__main__":
    main()

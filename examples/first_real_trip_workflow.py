from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_workflows import WorkflowEngine, approve, step, workflow


@step
async def collect_trip_context(inputs):
    return {
        "destination": inputs["destination"],
        "constraints": ["protect deep work", "avoid red-eye flights"],
        "gesture_required": True,
    }


@step
async def draft_trip_plan(context):
    return {
        "summary": f"Draft plan for {context['destination']}",
        "hotel_bias": "walkable boutique hotel",
        "jacqueline_gesture": "book one calm dinner and buy a small gift before travel day",
        "risks": ["do not book or spend without human approval"],
    }


@step
async def package_after_approval(plan, decision):
    return {
        "ready_for_booking_prep": True,
        "approved_by": decision["by"],
        "plan": plan,
        "next_action": "prepare booking checklist; still do not purchase without a separate approval gate",
    }


@workflow
async def first_real_trip_workflow(inputs):
    context = await collect_trip_context(inputs)
    plan = await draft_trip_plan(context)
    decision = await approve(
        "Approve this trip plan for packaging?",
        key="approve_trip_plan",
        artifact=plan,
        allowed=["approve", "reject", "edit", "rerun"],
    )
    if decision["action"] != "approve":
        return {"ready_for_booking_prep": False, "decision": decision, "plan": plan}
    return await package_after_approval(plan, decision)


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
            source={"kind": "human", "id": "skylar", "channel": "demo", "event_id": "demo-approval-message-1"},
            idempotency_key="demo-approval-message-1",
        )
        print("after approval signal", result)
        print("events", [event["type"] for event in engine.events("wf_first_real_trip")])


if __name__ == "__main__":
    main()

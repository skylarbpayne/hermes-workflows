from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_workflows import WorkflowEngine, step, workflow


@step
async def collect_constraints(ctx, inputs):
    raise AssertionError("decider should enqueue this step, not run it")


@step
async def draft_options(ctx, constraints):
    raise AssertionError("decider should enqueue this step, not run it")


@workflow
async def trip_planning(ctx, inputs):
    constraints = await collect_constraints(ctx, inputs)
    options = await draft_options(ctx, constraints)
    approval = await ctx.wait_for("approval.granted", key="approve_trip_plan")
    return {"options": options, "approved_by": approval["by"]}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "workflow.sqlite"

        engine = WorkflowEngine(db)
        print("start", engine.start(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip"))
        print("commands", engine.pending_commands("wf_trip"))

        # New engine instances simulate restart between every transition.
        engine = WorkflowEngine(db)
        print(
            "complete constraints",
            engine.complete_step("wf_trip", "step:collect_constraints:0", {"hard": ["no red eyes"]}),
        )

        engine = WorkflowEngine(db)
        print(
            "complete draft",
            engine.complete_step("wf_trip", "step:draft_options:0", {"summary": "NYC plan"}),
        )

        engine = WorkflowEngine(db)
        print(
            "signal approval",
            engine.signal(
                "wf_trip",
                "approval.granted",
                key="approve_trip_plan",
                payload={"by": "skylar", "decision": "approved"},
                idempotency_key="demo-approval-1",
            ),
        )
        print("events", [event["type"] for event in engine.events("wf_trip")])


if __name__ == "__main__":
    main()

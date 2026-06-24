from hermes_workflows import WorkflowEngine, approve, step, workflow
from hermes_workflows.status_projection import StatusProjection


@step
async def status_projection_step(value):
    return {"value": value}


@workflow
async def status_projection_workflow(inputs):
    decision = await approve(
        "Approve status projection?",
        key="approve_status_projection",
        artifact={"value": inputs["value"]},
    )
    if not decision.approved:
        return {"approved": False}
    return await status_projection_step(inputs["value"])


def _human_source(message_id="projection-approval"):
    return {"channel": "test", "message_id": message_id}


def test_status_projection_matches_workflow_engine_facade_read_models(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(status_projection_workflow, {"value": "green"}, workflow_id="wf_projection")

    assert first.status == "waiting"
    projection = StatusProjection(engine)
    assert projection.workflow_status("wf_projection") == engine.workflow_status("wf_projection")
    assert projection.list_workflows(status="waiting") == engine.list_workflows(status="waiting")
    assert projection.outbox_commands(workflow_id="wf_projection", status="pending") == engine.outbox_commands(
        workflow_id="wf_projection",
        status="pending",
    )

    approved = engine.signal(
        "wf_projection",
        "approval.decision",
        key="approve_status_projection",
        payload={"action": "approve", "by": "skylar"},
        source=_human_source(),
        idempotency_key="projection-approval",
    )
    done = engine.drain("wf_projection", initial=approved)

    assert done.status == "completed"
    reader = WorkflowEngine(db, read_only=True)
    read_projection = StatusProjection(reader)
    completed_status = read_projection.workflow_status("wf_projection", recent_events=100, command_history="all")

    assert completed_status == reader.workflow_status("wf_projection", recent_events=100, command_history="all")
    assert completed_status["status"] == "completed"
    assert completed_status["result"] == {"value": "green"}
    assert [command["status"] for command in completed_status["command_history"]] == [
        "completed",
        "completed",
        "completed",
    ]
    assert read_projection.list_workflows(status="completed") == [
        {
            "workflow_id": "wf_projection",
            "workflow_name": "status_projection_workflow",
            "status": "completed",
            "waiting_on": None,
        }
    ]

from hermes_workflows import WorkflowEngine, step, workflow


RUNS = []


@step
async def gather_left(ctx, value):
    RUNS.append(("left", value))
    return {"side": "left", "value": value}


@step
async def gather_right(ctx, value):
    RUNS.append(("right", value))
    return {"side": "right", "value": value}


@workflow
async def fanout_workflow(ctx, inputs):
    left, right = await ctx.gather(
        gather_left(ctx, inputs["left"]),
        gather_right(ctx, inputs["right"]),
    )
    return {"left": left, "right": right}


def test_gather_enqueues_all_missing_steps_before_waiting(tmp_path):
    RUNS.clear()
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    result = engine.start(fanout_workflow, {"left": 1, "right": 2}, workflow_id="wf_gather")

    assert result.status == "waiting"
    assert result.waiting_on == "gather:0"
    assert RUNS == []
    commands = engine.pending_commands("wf_gather")
    assert [command["key"] for command in commands] == ["step:gather_left:0", "step:gather_right:0"]
    events = engine.events("wf_gather")
    assert [event["key"] for event in events if event["type"] == "StepRequested"] == [
        "step:gather_left:0",
        "step:gather_right:0",
    ]


def test_gather_drain_executes_all_steps_and_resolves_in_order(tmp_path):
    RUNS.clear()
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    result = engine.run_until_idle(fanout_workflow, {"left": 1, "right": 2}, workflow_id="wf_gather")

    assert result.status == "completed"
    assert result.result == {
        "left": {"side": "left", "value": 1},
        "right": {"side": "right", "value": 2},
    }
    assert RUNS == [("left", 1), ("right", 2)]

    restarted = WorkflowEngine(tmp_path / "workflow.sqlite")
    assert restarted.run_until_idle(fanout_workflow, {"left": 1, "right": 2}, workflow_id="wf_gather").result == result.result
    assert RUNS == [("left", 1), ("right", 2)]

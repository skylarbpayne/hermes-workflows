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


def test_gather_resumes_after_each_worker_child_completes(tmp_path):
    RUNS.clear()
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    first = engine.start(fanout_workflow, {"left": 1, "right": 2}, workflow_id="wf_gather")

    assert first.status == "waiting"
    assert first.waiting_on == "gather:0"
    assert RUNS == []
    assert [command["key"] for command in engine.workflow_status("wf_gather")["pending_commands"]] == [
        "step:gather_left:0",
        "step:gather_right:0",
    ]

    after_left = engine.worker_once("wf_gather", worker_id="worker-left")

    assert after_left.status == "waiting"
    assert after_left.waiting_on == "gather:0"
    assert RUNS == [("left", 1)]
    assert [command["key"] for command in engine.workflow_status("wf_gather")["pending_commands"]] == [
        "step:gather_right:0"
    ]

    after_right = engine.worker_once("wf_gather", worker_id="worker-right")

    assert after_right.status == "completed"
    assert after_right.result == {
        "left": {"side": "left", "value": 1},
        "right": {"side": "right", "value": 2},
    }
    assert RUNS == [("left", 1), ("right", 2)]
    assert engine.workflow_status("wf_gather")["pending_commands"] == []

    restarted = WorkflowEngine(tmp_path / "workflow.sqlite")
    replay = restarted.run_until_idle(fanout_workflow, {"left": 1, "right": 2}, workflow_id="wf_gather")
    assert replay.result == after_right.result
    assert RUNS == [("left", 1), ("right", 2)]

    events = engine.events("wf_gather")
    assert [(event["type"], event["key"]) for event in events[:4]] == [
        ("WorkflowStarted", "workflow:start"),
        ("StepRequested", "step:gather_left:0"),
        ("StepRequested", "step:gather_right:0"),
        ("GatherWaiting", "gather:0"),
    ]
    assert [event["key"] for event in events if event["type"] == "StepRequested"] == [
        "step:gather_left:0",
        "step:gather_right:0",
    ]
    assert [event["key"] for event in events if event["type"] == "GatherWaiting"] == ["gather:0"]
    assert [event["key"] for event in events if event["type"] == "CommandClaimed"] == [
        "step:gather_left:0",
        "step:gather_right:0",
    ]
    assert [event["key"] for event in events if event["type"] == "StepCompleted"] == [
        "step:gather_left:0",
        "step:gather_right:0",
    ]
    assert [event["key"] for event in events if event["type"] == "WorkflowCompleted"] == ["workflow:completed"]


def test_gather_results_follow_call_order_when_children_complete_out_of_order(tmp_path):
    RUNS.clear()
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    first = engine.start(fanout_workflow, {"left": 1, "right": 2}, workflow_id="wf_gather")
    assert first.status == "waiting"
    assert [command["key"] for command in engine.workflow_status("wf_gather")["pending_commands"]] == [
        "step:gather_left:0",
        "step:gather_right:0",
    ]

    after_right = engine.complete_step("wf_gather", "step:gather_right:0", {"side": "right", "value": 2})
    assert after_right.status == "waiting"
    assert after_right.waiting_on == "gather:0"

    after_left = engine.complete_step("wf_gather", "step:gather_left:0", {"side": "left", "value": 1})

    assert after_left.status == "completed"
    assert after_left.result == {
        "left": {"side": "left", "value": 1},
        "right": {"side": "right", "value": 2},
    }
    assert RUNS == []
    assert [event["key"] for event in engine.events("wf_gather") if event["type"] == "StepCompleted"] == [
        "step:gather_right:0",
        "step:gather_left:0",
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


def test_gather_does_not_rerun_completed_children_after_restart(tmp_path):
    RUNS.clear()
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    result = engine.run_until_idle(fanout_workflow, {"left": 1, "right": 2}, workflow_id="wf_gather")
    assert result.status == "completed"
    assert RUNS == [("left", 1), ("right", 2)]

    restarted = WorkflowEngine(tmp_path / "workflow.sqlite")
    assert restarted.run_until_idle(fanout_workflow, {"left": 1, "right": 2}, workflow_id="wf_gather").result == result.result
    assert RUNS == [("left", 1), ("right", 2)]


def test_gather_rejects_non_step_inputs(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    @workflow
    async def bad_gather_workflow(ctx, inputs):
        async def plain_coroutine():
            return "not durable"

        return await ctx.gather(plain_coroutine())

    result = engine.run_until_idle(bad_gather_workflow, {}, workflow_id="wf_bad_gather")

    assert result.status == "failed"
    assert "ctx.gather only supports @step calls" in result.error

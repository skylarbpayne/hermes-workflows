from __future__ import annotations

import sqlite3

from hermes_workflows import Workflow, WorkflowEngine, agent, step, workflow
from hermes_workflows.engine import JsonCodec


@step
async def produce_items(ctx, inputs):
    return inputs["items"]


@step
async def analyze_generated_item(ctx, item):
    return {"item_id": item["id"], "label": "STATIC"}


@workflow
async def process_item(ctx, item):
    analysis = await analyze_generated_item(ctx, item)
    return {"static": analysis}


GENERATED_PROCESSOR_SOURCE = '''
from hermes_workflows import step, workflow

RUNS = []

@step
async def analyze_generated_item(ctx, item):
    RUNS.append(item["id"])
    return {"item_id": item["id"], "label": item["label"].upper()}

@workflow
async def process_item(ctx, item):
    analysis = await analyze_generated_item(ctx, item)
    return {"processed": analysis}
'''


WAITING_CHILD_SOURCE = '''
from hermes_workflows import workflow

@workflow
async def waiting_child(ctx, item):
    payload = await ctx.wait_for("dynamic.ready", key=item["id"])
    return {"payload": payload}
'''


SIGNAL_THEN_FAIL_CHILD_SOURCE = '''
from hermes_workflows import workflow

@workflow
async def signal_then_fail_child(ctx, item):
    await ctx.wait_for("dynamic.ready", key=item["id"])
    raise RuntimeError("child exploded")
'''


@workflow
async def dynamic_processor_pipeline(ctx, inputs):
    items = await produce_items(ctx, inputs)
    processor = await agent(
        "build_processor",
        prompt="Write a Python workflow that processes one discovered item.",
        input={"purpose": "process one discovered item"},
        returns=Workflow,
        mock_output={"source": GENERATED_PROCESSOR_SOURCE, "symbol": "process_item"},
    )

    results = []
    for item in items:
        results.append(await processor(item, key=item["id"]))
    return {"processed": results, "workflow_symbol": processor.symbol}


@workflow
async def dynamic_processor_map_pipeline(ctx, inputs):
    items = await produce_items(ctx, inputs)
    processor = await agent(
        "build_processor",
        prompt="Write a Python workflow that processes one discovered item.",
        input={"purpose": "process one discovered item"},
        returns=Workflow,
        mock_output={"source": GENERATED_PROCESSOR_SOURCE, "symbol": "process_item"},
    )

    results = await ctx.map_workflow(
        processor,
        items,
        key_fn=lambda item: item["id"],
        concurrency=4,
    )
    return {"processed": results}


@workflow
async def dynamic_waiting_child_pipeline(ctx, inputs):
    processor = await agent(
        "build_waiting_child",
        prompt="Write a Python workflow that waits for a signal.",
        input={"purpose": "wait for signal"},
        returns=Workflow,
        mock_output={"source": WAITING_CHILD_SOURCE, "symbol": "waiting_child"},
    )
    return await processor(inputs["item"], key=inputs["item"]["id"])


@workflow
async def dynamic_waiting_child_map_pipeline(ctx, inputs):
    processor = await agent(
        "build_waiting_child_map",
        prompt="Write a Python workflow that waits for a signal.",
        input={"purpose": "wait for signal"},
        returns=Workflow,
        mock_output={"source": WAITING_CHILD_SOURCE, "symbol": "waiting_child"},
    )
    results = await ctx.map_workflow(
        processor,
        inputs["items"],
        key_fn=lambda item: item["id"],
        concurrency=4,
    )
    return {"processed": results}


@workflow
async def dynamic_signal_then_fail_child_pipeline(ctx, inputs):
    processor = await agent(
        "build_signal_then_fail_child",
        prompt="Write a Python workflow that fails after a signal.",
        input={"purpose": "fail after signal"},
        returns=Workflow,
        mock_output={"source": SIGNAL_THEN_FAIL_CHILD_SOURCE, "symbol": "signal_then_fail_child"},
    )
    return await processor(inputs["item"], key=inputs["item"]["id"])


@workflow
async def live_generated_waiting_child_pipeline(ctx, inputs):
    processor = await agent(
        "build_live_waiting_child",
        prompt="Write a Python workflow that waits for a signal.",
        input={"purpose": "wait for signal"},
        returns=Workflow,
    )
    return await processor(inputs["item"], key=inputs["item"]["id"])


def test_agent_can_return_workflow_and_call_it_for_each_item(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        dynamic_processor_pipeline,
        {"items": [{"id": "a", "label": "alpha"}, {"id": "b", "label": "beta"}]},
        workflow_id="wf_dynamic",
    )

    assert result.status == "completed"
    assert result.result == {
        "processed": [
            {"processed": {"item_id": "a", "label": "ALPHA"}},
            {"processed": {"item_id": "b", "label": "BETA"}},
        ],
        "workflow_symbol": "process_item",
    }

    events = engine.events("wf_dynamic")
    completed_agent = [event for event in events if event["type"] == "StepCompleted" and event["key"] == "agent:build_processor:0"][0]
    workflow_value = completed_agent["payload"]["output"]
    assert isinstance(workflow_value, Workflow)
    assert workflow_value.symbol == "process_item"
    assert workflow_value.source_sha256
    assert workflow_value.with_base_dir(db.parent).path.endswith(f"{workflow_value.source_sha256}.py")

    child_requests = [event for event in events if event["type"] == "ChildWorkflowRequested"]
    child_group = f"process_item:{workflow_value.source_sha256[:12]}"
    assert [event["key"] for event in child_requests] == [f"child:{child_group}:a", f"child:{child_group}:b"]
    assert [event["payload"]["child_workflow_id"] for event in child_requests] == [
        f"wf_dynamic.child.{child_group}.a",
        f"wf_dynamic.child.{child_group}.b",
    ]

    static = engine.run_until_idle(process_item, {"id": "z", "label": "zulu"}, workflow_id="wf_static_after_generated")
    assert static.status == "completed"
    assert static.result == {"static": {"item_id": "z", "label": "STATIC"}}


def test_returned_workflow_survives_replay_without_regenerating_or_duplicating_children(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(
        dynamic_processor_pipeline,
        {"items": [{"id": "a", "label": "alpha"}]},
        workflow_id="wf_dynamic_replay",
    )
    assert first.status == "completed"

    restarted = WorkflowEngine(db)
    replayed = restarted.run_until_idle(
        dynamic_processor_pipeline,
        {"items": [{"id": "a", "label": "alpha"}]},
        workflow_id="wf_dynamic_replay",
    )

    assert replayed.status == "completed"
    assert replayed.result == first.result
    events = restarted.events("wf_dynamic_replay")
    assert [event["type"] for event in events].count("StepRequested") == 2  # produce_items + agent once each
    assert [event["type"] for event in events].count("ChildWorkflowRequested") == 1

    with sqlite3.connect(db) as con:
        child_count = con.execute(
            "SELECT COUNT(*) FROM workflow_instances WHERE id LIKE 'wf_dynamic_replay.child.%'"
        ).fetchone()[0]
    assert child_count == 1


def test_map_workflow_starts_generated_workflow_for_items_and_preserves_order(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        dynamic_processor_map_pipeline,
        {"items": [{"id": "b", "label": "beta"}, {"id": "a", "label": "alpha"}]},
        workflow_id="wf_dynamic_map",
    )

    assert result.status == "completed"
    assert result.result == {
        "processed": [
            {"processed": {"item_id": "b", "label": "BETA"}},
            {"processed": {"item_id": "a", "label": "ALPHA"}},
        ]
    }
    workflow_value = [
        event for event in engine.events("wf_dynamic_map") if event["type"] == "StepCompleted" and event["key"] == "agent:build_processor:0"
    ][0]["payload"]["output"]
    child_group = f"map:0:process_item:{workflow_value.source_sha256[:12]}"
    assert [
        event["key"] for event in engine.events("wf_dynamic_map") if event["type"] == "ChildWorkflowRequested"
    ] == [f"child:{child_group}:b", f"child:{child_group}:a"]


def test_child_workflow_keys_are_collision_resistant_after_sanitizing(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        dynamic_processor_pipeline,
        {"items": [{"id": "a/b", "label": "slash"}, {"id": "a b", "label": "space"}]},
        workflow_id="wf_dynamic_collision_keys",
    )

    assert result.status == "completed"
    assert result.result["processed"] == [
        {"processed": {"item_id": "a/b", "label": "SLASH"}},
        {"processed": {"item_id": "a b", "label": "SPACE"}},
    ]
    child_keys = [event["key"] for event in engine.events("wf_dynamic_collision_keys") if event["type"] == "ChildWorkflowRequested"]
    assert len(child_keys) == len(set(child_keys)) == 2


def test_workflow_json_decode_is_lazy_and_validates_source_hash():
    source = '''
from definitely_missing_generated_dependency import nope
from hermes_workflows import workflow

@workflow
async def unreachable(ctx, inputs):
    return inputs
'''
    import hashlib

    payload = {
        "__hermes_type__": "Workflow",
        "source": source,
        "symbol": "unreachable",
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "path": "/tmp/hermes-workflows-test-lazy/generated_workflows/lazy.py",
        "module_name": "untrusted.module",
    }
    # Decoding should not import/execute generated Python; load would fail because
    # the generated module imports a missing dependency.
    decoded = JsonCodec.loads(JsonCodec.dumps({"workflow": Workflow.from_json(payload)}))["workflow"]
    assert isinstance(decoded, Workflow)

    tampered = dict(payload)
    tampered["source"] = tampered["source"] + "\n# changed"
    try:
        Workflow.from_json(tampered)
    except ValueError as exc:
        assert "source_sha256" in str(exc)
    else:
        raise AssertionError("tampered Workflow source must fail hash validation")


def test_generated_source_validation_rejects_import_time_execution_shapes(tmp_path):
    unsafe_class_source = '''
from hermes_workflows import workflow

class Boom:
    open("/tmp/hermes-workflows-should-not-exist", "w").write("boom")

@workflow
async def process(ctx, inputs):
    return inputs
'''
    try:
        Workflow.from_source(unsafe_class_source, symbol="process", base_dir=tmp_path)
    except ValueError as exc:
        assert "top-level ClassDef" in str(exc)
    else:
        raise AssertionError("class bodies must be rejected because they execute at import time")

    unsafe_decorator_source = '''
from hermes_workflows import workflow

def make_decorator():
    raise RuntimeError("decorator ran")

@make_decorator()
async def process(ctx, inputs):
    return inputs

@workflow
async def fallback(ctx, inputs):
    return inputs
'''
    try:
        Workflow.from_source(unsafe_decorator_source, symbol="fallback", base_dir=tmp_path)
    except ValueError as exc:
        assert "decorators" in str(exc)
    else:
        raise AssertionError("decorator calls must be rejected before import")


def test_child_workflow_waits_without_failing_parent(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        dynamic_waiting_child_pipeline,
        {"item": {"id": "needs-signal"}},
        workflow_id="wf_waiting_child",
    )

    assert result.status == "waiting"
    assert result.waiting_on is not None
    assert result.waiting_on.startswith("child:")
    assert "waiting children are not supported" not in (result.error or "")
    assert not [event for event in engine.events("wf_waiting_child") if event["type"] == "ChildWorkflowFailed"]

    child_requested = [
        event for event in engine.events("wf_waiting_child") if event["type"] == "ChildWorkflowRequested"
    ][0]
    child_id = child_requested["payload"]["child_workflow_id"]
    child_status = engine.workflow_status(child_id, recent_events=1)
    assert child_status["status"] == "waiting"
    assert child_status["waiting_on"] == "signal:dynamic.ready:needs-signal"

    parent_status = engine.workflow_status("wf_waiting_child", recent_events=1)
    assert parent_status["child_workflows"] == [
        {
            "key": child_requested["key"],
            "child_workflow_id": child_id,
            "status": "waiting",
            "waiting_on": "signal:dynamic.ready:needs-signal",
            "diagnostic_label": "child_workflow_waiting",
            "diagnostic_message": "Parent is waiting on child workflow output.",
        }
    ]


def test_parent_completes_after_waiting_child_is_signaled_and_reconciled(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        dynamic_waiting_child_pipeline,
        {"item": {"id": "needs-signal"}},
        workflow_id="wf_waiting_child_resume",
    )
    assert first.status == "waiting"

    child_requested = [
        event for event in engine.events("wf_waiting_child_resume") if event["type"] == "ChildWorkflowRequested"
    ][0]
    child_key = child_requested["key"]
    child_id = child_requested["payload"]["child_workflow_id"]

    child_after_signal = engine.signal(
        child_id,
        "dynamic.ready",
        key="needs-signal",
        payload={"ok": True},
        source={"kind": "test", "id": "unit"},
    )
    child_after_signal = engine.drain(child_id, initial=child_after_signal)
    assert child_after_signal.status == "completed"
    parent_status_before_reconcile = engine.workflow_status("wf_waiting_child_resume", recent_events=1)
    assert parent_status_before_reconcile["status"] == "waiting"
    assert parent_status_before_reconcile["child_workflows"] == [
        {
            "key": child_key,
            "child_workflow_id": child_id,
            "status": "completed",
            "waiting_on": None,
            "diagnostic_label": "child_workflow_terminal_unreconciled",
            "diagnostic_message": "Child workflow is terminal; parent has not reconciled it yet.",
        }
    ]

    final = engine.reconcile_child_result("wf_waiting_child_resume", child_key)
    final = engine.drain("wf_waiting_child_resume", initial=final)

    assert final.status == "completed"
    assert final.result == {"payload": {"ok": True}}
    assert engine.workflow_status("wf_waiting_child_resume", recent_events=1)["child_workflows"] == []
    completed = [
        event for event in engine.events("wf_waiting_child_resume") if event["type"] == "ChildWorkflowCompleted"
    ]
    assert completed
    assert completed[0]["payload"]["child_workflow_id"] == child_id


def test_reconcile_children_replays_parent_after_terminal_child_crash_window(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    first = engine.run_until_idle(dynamic_waiting_child_pipeline, {"item": {"id": "crash-window"}}, workflow_id="wf_child_terminal_crash_window")
    assert first.status == "waiting"
    child_requested = [event for event in engine.events("wf_child_terminal_crash_window") if event["type"] == "ChildWorkflowRequested"][0]
    child_key = child_requested["key"]
    child_id = child_requested["payload"]["child_workflow_id"]
    child_after_signal = engine.signal(child_id, "dynamic.ready", key="crash-window", payload={"ok": True}, source={"kind": "test", "id": "unit"})
    child_after_signal = engine.drain(child_id, initial=child_after_signal)
    assert child_after_signal.status == "completed"
    # Simulate: terminal child event committed, but parent replay crashed before WorkflowCompleted.
    with engine._connect() as con:
        con.execute("BEGIN IMMEDIATE")
        engine._append_event(con, "wf_child_terminal_crash_window", "ChildWorkflowCompleted", key=child_key, payload={"child_workflow_id": child_id, "result": child_after_signal.result}, idempotency_key=f"child-completed:{child_key}", ignore_duplicate=True)
        con.execute("UPDATE workflow_instances SET status = 'running', waiting_on = NULL WHERE id = ?", ("wf_child_terminal_crash_window",))
    assert engine.pending_child_workflow_keys("wf_child_terminal_crash_window") == []
    recovered = engine.reconcile_children("wf_child_terminal_crash_window")
    recovered = engine.drain("wf_child_terminal_crash_window", initial=recovered)
    assert recovered.status == "completed"
    assert recovered.result == {"payload": {"ok": True}}
    assert [event["type"] for event in engine.events("wf_child_terminal_crash_window")].count("WorkflowCompleted") == 1


def test_map_workflow_waits_on_gather_and_completes_after_children_reconcile_in_order(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        dynamic_waiting_child_map_pipeline,
        {"items": [{"id": "b"}, {"id": "a"}]},
        workflow_id="wf_waiting_child_map",
    )

    assert first.status == "waiting"
    gather_events = [event for event in engine.events("wf_waiting_child_map") if event["type"] == "ChildWorkflowGatherWaiting"]
    assert len(gather_events) == 1
    assert first.waiting_on == gather_events[0]["key"] == "child-gather:map:0"
    requested = [event for event in engine.events("wf_waiting_child_map") if event["type"] == "ChildWorkflowRequested"]
    assert [event["payload"]["child_key"] for event in requested] == ["b", "a"]
    child_ids = {event["payload"]["child_key"]: event["payload"]["child_workflow_id"] for event in requested}
    assert engine.workflow_status(child_ids["b"], recent_events=1)["waiting_on"] == "signal:dynamic.ready:b"
    assert engine.workflow_status(child_ids["a"], recent_events=1)["waiting_on"] == "signal:dynamic.ready:a"

    assert engine.pending_child_workflow_keys("wf_waiting_child_map") == [event["key"] for event in requested]

    child_b = engine.signal(child_ids["b"], "dynamic.ready", key="b", payload={"letter": "B"})
    assert engine.drain(child_ids["b"], initial=child_b).status == "completed"
    after_one = engine.reconcile_children("wf_waiting_child_map")
    after_one = engine.drain("wf_waiting_child_map", initial=after_one)
    assert after_one.status == "waiting"
    assert after_one.waiting_on == first.waiting_on
    assert [event["type"] for event in engine.events("wf_waiting_child_map")].count("ChildWorkflowCompleted") == 1

    child_a = engine.signal(child_ids["a"], "dynamic.ready", key="a", payload={"letter": "A"})
    assert engine.drain(child_ids["a"], initial=child_a).status == "completed"
    final = engine.reconcile_children("wf_waiting_child_map")
    final = engine.drain("wf_waiting_child_map", initial=final)
    assert final.status == "completed"
    assert final.result == {"processed": [{"payload": {"letter": "B"}}, {"payload": {"letter": "A"}}]}
    assert engine.pending_child_workflow_keys("wf_waiting_child_map") == []

    again = engine.reconcile_children("wf_waiting_child_map")
    assert again.status == "completed"
    assert [event["type"] for event in engine.events("wf_waiting_child_map")].count("ChildWorkflowCompleted") == 2


def test_cancelled_parent_does_not_report_active_child_workflows(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        dynamic_waiting_child_pipeline,
        {"item": {"id": "cancelled-child-wait"}},
        workflow_id="wf_cancelled_child_wait",
    )
    assert first.status == "waiting"
    assert engine.workflow_status("wf_cancelled_child_wait", recent_events=1)["child_workflows"]

    cancelled = engine.cancel_workflow(
        "wf_cancelled_child_wait",
        reason="test cancellation",
        source={"kind": "test", "id": "unit"},
    )

    assert cancelled.status == "cancelled"
    status = engine.workflow_status("wf_cancelled_child_wait", recent_events=1)
    assert status["status"] == "cancelled"
    assert status["waiting_on"] is None
    assert status["child_workflows"] == []


def test_reconcile_missing_child_instance_keeps_parent_waiting_with_diagnostic_event(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        dynamic_waiting_child_pipeline,
        {"item": {"id": "missing-child"}},
        workflow_id="wf_missing_child",
    )
    assert first.status == "waiting"
    child_requested = [event for event in engine.events("wf_missing_child") if event["type"] == "ChildWorkflowRequested"][0]
    child_key = child_requested["key"]
    child_id = child_requested["payload"]["child_workflow_id"]

    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM workflow_instances WHERE id = ?", (child_id,))

    assert engine.workflow_status("wf_missing_child", recent_events=1)["child_workflows"] == [
        {
            "key": child_key,
            "child_workflow_id": child_id,
            "status": "pending",
            "waiting_on": None,
            "diagnostic_label": "child_workflow_pending",
            "diagnostic_message": "Parent requested a child workflow that has not produced an inspectable status yet.",
        }
    ]

    reconciled = engine.reconcile_child_result("wf_missing_child", child_key)

    assert reconciled.status == "waiting"
    assert reconciled.waiting_on == child_key
    waiting_events = [event for event in engine.events("wf_missing_child") if event["type"] == "ChildWorkflowWaiting"]
    assert waiting_events[-1]["payload"] == {"child_workflow_id": child_id, "status": "pending", "waiting_on": None}


def test_failed_waiting_child_fails_parent_when_reconciled(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    first = engine.run_until_idle(
        dynamic_signal_then_fail_child_pipeline,
        {"item": {"id": "boom"}},
        workflow_id="wf_failing_child",
    )
    assert first.status == "waiting"
    child_requested = [event for event in engine.events("wf_failing_child") if event["type"] == "ChildWorkflowRequested"][0]
    child_id = child_requested["payload"]["child_workflow_id"]

    child_after_signal = engine.signal(child_id, "dynamic.ready", key="boom", payload={"go": True})
    child_after_signal = engine.drain(child_id, initial=child_after_signal)
    assert child_after_signal.status == "failed"

    reconciled = engine.reconcile_child_result("wf_failing_child", child_requested["key"])

    assert reconciled.status == "failed"
    assert "child exploded" in (reconciled.error or "")
    failed_events = [event for event in engine.events("wf_failing_child") if event["type"] == "ChildWorkflowFailed"]
    assert len(failed_events) == 1
    assert failed_events[0]["payload"]["error"]["type"] == "ChildWorkflowFailed"


def test_unapproved_live_generated_workflow_does_not_start_child_until_approved(tmp_path):
    db = tmp_path / "workflow.sqlite"

    def live_runner(request):
        return {
            "output": {"source": WAITING_CHILD_SOURCE, "symbol": "waiting_child"},
            "provenance": {"runner": "unit-test"},
        }

    engine = WorkflowEngine(db, agent_runner=live_runner)
    first = engine.run_until_idle(
        live_generated_waiting_child_pipeline,
        {"item": {"id": "needs-approval"}},
        workflow_id="wf_live_generated_waiting_child",
    )

    assert first.status == "waiting"
    assert first.waiting_on is not None
    assert first.waiting_on.startswith("signal:approval.decision:generated-workflow:")
    events = engine.events("wf_live_generated_waiting_child")
    assert [event["type"] for event in events].count("ApprovalRequested") == 1
    assert not [event for event in events if event["type"] == "ChildWorkflowRequested"]
    with sqlite3.connect(db) as con:
        child_count = con.execute(
            "SELECT COUNT(*) FROM workflow_instances WHERE id LIKE 'wf_live_generated_waiting_child.child.%'"
        ).fetchone()[0]
    assert child_count == 0

    approval_key = first.waiting_on.removeprefix("signal:approval.decision:")
    after_approval = engine.signal(
        "wf_live_generated_waiting_child",
        "approval.decision",
        key=approval_key,
        payload={"action": "approve", "by": "skylar", "message": "unit test approval"},
        source={"kind": "human", "id": "skylar", "channel": "unit-test", "event_id": "evt-approval"},
    )
    after_approval = engine.drain("wf_live_generated_waiting_child", initial=after_approval)
    assert after_approval.status == "waiting"
    assert after_approval.waiting_on is not None
    assert after_approval.waiting_on.startswith("child:")

    child_requested = [
        event for event in engine.events("wf_live_generated_waiting_child") if event["type"] == "ChildWorkflowRequested"
    ][0]
    child_id = child_requested["payload"]["child_workflow_id"]
    assert engine.workflow_status(child_id, recent_events=1)["waiting_on"] == "signal:dynamic.ready:needs-approval"

    child_done = engine.signal(child_id, "dynamic.ready", key="needs-approval", payload={"ok": True})
    child_done = engine.drain(child_id, initial=child_done)
    assert child_done.status == "completed"

    final = engine.reconcile_child_result("wf_live_generated_waiting_child", child_requested["key"])
    final = engine.drain("wf_live_generated_waiting_child", initial=final)
    assert final.status == "completed"
    assert final.result == {"payload": {"ok": True}}


def test_generated_source_validation_rejects_decorator_shadowing(tmp_path):
    shadowed_decorator_source = '''
from hermes_workflows import step, workflow

def workflow(fn):
    open("/tmp/hermes-workflows-shadowed-decorator", "w").write("boom")
    return fn

@workflow
async def process(ctx, inputs):
    return inputs
'''
    try:
        Workflow.from_source(shadowed_decorator_source, symbol="process", base_dir=tmp_path)
    except ValueError as exc:
        assert "shadow" in str(exc) or "workflow or step" in str(exc)
    else:
        raise AssertionError("generated modules must reject local workflow/step shadowing")


def test_generated_workflow_symbol_must_name_decorated_workflow(tmp_path):
    undecorated_symbol_source = '''
from hermes_workflows import workflow

async def helper(ctx, inputs):
    return inputs

@workflow
async def real_workflow(ctx, inputs):
    return inputs
'''
    try:
        Workflow.from_source(undecorated_symbol_source, symbol="helper", base_dir=tmp_path)
    except ValueError as exc:
        assert "not a @workflow" in str(exc)
    else:
        raise AssertionError("Workflow symbol must point at a decorated @workflow function")

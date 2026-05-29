from __future__ import annotations

import sqlite3

from hermes_workflows import AgentStep, Workflow, WorkflowEngine, step, workflow
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


@workflow
async def dynamic_processor_pipeline(ctx, inputs):
    items = await produce_items(ctx, inputs)
    processor = await AgentStep(
        "build_processor",
        prompt="Write a Python workflow that processes one discovered item.",
        returns=Workflow,
        mock_output={"source": GENERATED_PROCESSOR_SOURCE, "symbol": "process_item"},
    )(ctx)

    results = []
    for item in items:
        results.append(await processor(ctx, item, key=item["id"]))
    return {"processed": results, "workflow_symbol": processor.symbol}


@workflow
async def dynamic_processor_map_pipeline(ctx, inputs):
    items = await produce_items(ctx, inputs)
    processor = await AgentStep(
        "build_processor",
        prompt="Write a Python workflow that processes one discovered item.",
        returns=Workflow,
        mock_output={"source": GENERATED_PROCESSOR_SOURCE, "symbol": "process_item"},
    )(ctx)

    results = await ctx.map_workflow(
        processor,
        items,
        key_fn=lambda item: item["id"],
        concurrency=4,
    )
    return {"processed": results}


@workflow
async def dynamic_waiting_child_pipeline(ctx, inputs):
    processor = await AgentStep(
        "build_waiting_child",
        prompt="Write a Python workflow that waits for a signal.",
        returns=Workflow,
        mock_output={"source": WAITING_CHILD_SOURCE, "symbol": "waiting_child"},
    )(ctx)
    return await processor(ctx, inputs["item"], key=inputs["item"]["id"])


def test_agent_step_can_return_workflow_and_call_it_for_each_item(tmp_path):
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
    completed_agent_step = [event for event in events if event["type"] == "StepCompleted" and event["key"] == "step:agent_step:0"][0]
    workflow_value = completed_agent_step["payload"]["output"]
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
    assert [event["type"] for event in events].count("StepRequested") == 2  # produce_items + AgentStep once each
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
        event for event in engine.events("wf_dynamic_map") if event["type"] == "StepCompleted" and event["key"] == "step:agent_step:0"
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


def test_child_workflow_waits_fail_closed_instead_of_deadlocking_parent(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)

    result = engine.run_until_idle(
        dynamic_waiting_child_pipeline,
        {"item": {"id": "needs-signal"}},
        workflow_id="wf_waiting_child",
    )

    assert result.status == "failed"
    assert "waiting children are not supported" in result.error
    failed_events = [event for event in engine.events("wf_waiting_child") if event["type"] == "ChildWorkflowFailed"]
    assert failed_events
    assert failed_events[0]["payload"]["error"]["type"] == "ChildWorkflowIncomplete"


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

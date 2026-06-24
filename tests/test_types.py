from __future__ import annotations

from dataclasses import dataclass

import pytest

from hermes_workflows.approvals import ApprovalDecision
from hermes_workflows.types import JsonObject, JsonValue, to_json_object, to_json_value
from hermes_workflows.workflow_values import Workflow


@dataclass(frozen=True)
class NestedPacket:
    title: str
    tags: tuple[str, ...]
    metadata: dict[object, object]


def test_to_json_value_normalizes_dataclasses_sequences_and_mapping_keys() -> None:
    packet = NestedPacket(
        title="Launch",
        tags=("workflow", "artifact"),
        metadata={"count": 2, 3: True, "nested": [{"ok": None}]},
    )

    value: JsonValue = to_json_value(packet)

    assert value == {
        "title": "Launch",
        "tags": ["workflow", "artifact"],
        "metadata": {"count": 2, "3": True, "nested": [{"ok": None}]},
    }


def test_to_json_value_normalizes_framework_value_objects() -> None:
    workflow = Workflow(
        source="async def generated(inputs):\n    return inputs\n",
        symbol="generated",
        source_sha256="abc123",
        path="/tmp/generated.py",
        module_name="generated_mod",
        provenance={"created_by": "test"},
        approval_required=True,
        approval_key="approve_generated",
    )
    decision = ApprovalDecision(
        action="approve",
        by="human:test",
        source={"channel": "test"},
        direct_feedback="ship it",
    )

    assert to_json_value({"workflow": workflow, "decision": decision}) == {
        "workflow": workflow.to_json(),
        "decision": {
            "action": "approve",
            "by": "human:test",
            "feedback": "ship it",
            "source": {"channel": "test"},
        },
    }


def test_to_json_object_requires_object_shape() -> None:
    value: JsonObject = to_json_object({"ok": True})
    assert value == {"ok": True}

    with pytest.raises(TypeError, match="expected JSON object"):
        to_json_object(["not", "an", "object"])

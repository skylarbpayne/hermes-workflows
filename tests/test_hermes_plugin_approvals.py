from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_workflows import WorkflowEngine, step, workflow


@step
async def plugin_followup_step(ctx, decision: dict[str, Any]) -> dict[str, Any]:
    return {"followup_ran": True, "decision": decision}


@workflow
async def plugin_approval_workflow(ctx, inputs):
    decision = await ctx.approval.request(
        key="approve_plugin_test",
        prompt="Approve the plugin test packet?",
        artifact={
            "summary": "Plugin approval packet",
            "secret_token": "should-not-leak",
        },
        approver="human:skylar",
        allowed=["approve", "reject"],
        authority={"scope": "plugin-test"},
    )
    return await plugin_followup_step(ctx, decision)


class FakePluginContext:
    def __init__(self):
        self.tools: dict[str, dict[str, Any]] = {}
        self.hooks: dict[str, Any] = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name: str, callback):
        self.hooks[name] = callback


def create_pending_approval(db: Path) -> WorkflowEngine:
    engine = WorkflowEngine(db)
    engine.run_until_idle(
        plugin_approval_workflow,
        {},
        workflow_id="wf_plugin",
        workflow_ref="tests.test_hermes_plugin_approvals:plugin_approval_workflow",
    )
    return engine


def parse_tool_result(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    assert data["success"] is True
    return data


def test_plugin_entrypoint_and_directory_manifest_are_present():
    import tomllib

    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    entry_points = pyproject["project"]["entry-points"]["hermes_agent.plugins"]
    assert entry_points["hermes-workflows-approvals"] == "hermes_workflows.hermes_plugin_approvals"

    manifest = Path("plugins/hermes-workflows-approvals/plugin.yaml")
    shim = Path("plugins/hermes-workflows-approvals/__init__.py")
    assert manifest.exists()
    assert shim.exists()
    assert "workflow_approvals_list" in manifest.read_text()
    assert "pre_gateway_dispatch" in manifest.read_text()


def test_plugin_registers_approval_tools_and_gateway_hook():
    from hermes_workflows.hermes_plugin_approvals import register

    ctx = FakePluginContext()
    register(ctx)

    assert set(ctx.tools) == {"workflow_approvals_list", "workflow_approval_decide"}
    assert ctx.tools["workflow_approvals_list"]["toolset"] == "hermes_workflows_approvals"
    assert ctx.tools["workflow_approval_decide"]["toolset"] == "hermes_workflows_approvals"
    assert "pre_gateway_dispatch" in ctx.hooks


def test_workflow_approvals_list_returns_bounded_redacted_pending_approval(tmp_path):
    from hermes_workflows.hermes_plugin_approvals import register

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    ctx = FakePluginContext()
    register(ctx)

    result = parse_tool_result(ctx.tools["workflow_approvals_list"]["handler"]({"db": str(db)}))

    assert result["count"] == 1
    approval = result["approvals"][0]
    assert approval["workflow_id"] == "wf_plugin"
    assert approval["workflow_ref"] == "tests.test_hermes_plugin_approvals:plugin_approval_workflow"
    assert approval["key"] == "approve_plugin_test"
    assert approval["allowed"] == ["approve", "reject"]
    assert approval["authority"] == {"scope": "plugin-test"}
    assert approval["artifact"]["summary"] == "Plugin approval packet"
    assert approval["artifact"]["secret_token"] == "[REDACTED]"
    assert approval["decision_token_approve"].startswith("hwf-approval:v1:approve:")
    assert approval["decision_token_reject"].startswith("hwf-approval:v1:reject:")


def test_workflow_approval_decide_defaults_to_resume_false(tmp_path):
    from hermes_workflows.hermes_plugin_approvals import register

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    ctx = FakePluginContext()
    register(ctx)

    result = parse_tool_result(
        ctx.tools["workflow_approval_decide"]["handler"](
            {
                "db": str(db),
                "workflow_id": "wf_plugin",
                "key": "approve_plugin_test",
                "action": "approve",
                "by": "skylar",
                "channel": "discord",
                "message_id": "msg-123",
            }
        )
    )

    assert result["receipt"]["action"] == "approve"
    assert result["receipt"]["resume_requested"] is False
    assert result["receipt"]["status"] == "decision_recorded"
    assert result["next_step"] == "Run or queue a trusted workflow resumer for workflow_ref tests.test_hermes_plugin_approvals:plugin_approval_workflow."
    status = WorkflowEngine(db).workflow_status("wf_plugin")
    assert status["status"] == "waiting"
    assert status["result"] is None


def test_workflow_approval_decide_can_resume_when_explicitly_requested(tmp_path):
    from hermes_workflows.hermes_plugin_approvals import register

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    ctx = FakePluginContext()
    register(ctx)

    result = parse_tool_result(
        ctx.tools["workflow_approval_decide"]["handler"](
            {
                "db": str(db),
                "workflow_id": "wf_plugin",
                "key": "approve_plugin_test",
                "action": "approve",
                "by": "skylar",
                "channel": "cli",
                "message_id": "manual-1",
                "resume": True,
            }
        )
    )

    assert result["receipt"]["resume_requested"] is True
    assert result["receipt"]["status"] == "completed"
    assert result["receipt"]["result_summary"]["followup_ran"] is True


@dataclass
class FakeSource:
    platform: Any = "discord"
    chat_id: str = "chat-42"
    user_id: str = "skylar"
    user_name: str = "Skylar Payne"
    message_id: str = "msg-456"


@dataclass
class FakeEvent:
    text: str
    source: FakeSource


def test_gateway_hook_only_handles_exact_decision_token(tmp_path):
    from hermes_workflows.hermes_plugin_approvals import decision_token, register

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    ctx = FakePluginContext()
    register(ctx)
    hook = ctx.hooks["pre_gateway_dispatch"]

    unrelated = hook(event=FakeEvent("yes looks good", FakeSource()), gateway=None, session_store=None)
    assert unrelated is None

    token = decision_token("approve", str(db), "wf_plugin", "approve_plugin_test")
    handled = hook(event=FakeEvent(token, FakeSource()), gateway=None, session_store=None)

    assert handled["action"] == "skip"
    assert handled["reason"] == "workflow approval decision recorded"
    assert handled["receipt"]["action"] == "approve"
    assert handled["receipt"]["source"]["channel"] == "discord:chat-42"
    assert handled["receipt"]["source"]["message_id"] == "msg-456"
    assert WorkflowEngine(db).workflow_status("wf_plugin")["status"] == "waiting"

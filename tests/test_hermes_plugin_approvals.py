from __future__ import annotations

import base64
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hermes_workflows import WorkflowEngine, step, workflow
from hermes_workflows.engine import JsonCodec


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
        approver="human:operator",
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


def crafted_decision_token(action: str, payload: dict[str, str]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"hwf-approval:v1:{action}:{encoded}"


def test_plugin_entrypoint_and_directory_manifest_are_present():
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9/3.10 compatibility
        import tomli as tomllib

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

    assert set(ctx.tools) == {
        "workflow_approvals_list",
        "workflow_operator_steps_list",
        "workflow_approval_decide",
        "workflow_operator_respond",
    }
    assert ctx.tools["workflow_approvals_list"]["toolset"] == "hermes_workflows_approvals"
    assert ctx.tools["workflow_operator_steps_list"]["toolset"] == "hermes_workflows_approvals"
    assert ctx.tools["workflow_approval_decide"]["toolset"] == "hermes_workflows_approvals"
    assert ctx.tools["workflow_operator_respond"]["toolset"] == "hermes_workflows_approvals"
    assert "pre_gateway_dispatch" in ctx.hooks


def test_configured_dbs_accepts_json_string_from_hermes_config(monkeypatch, tmp_path):
    import hermes_workflows.hermes_plugin_approvals as approvals

    db = tmp_path / "workflow.sqlite"
    hermes_cli = ModuleType("hermes_cli")
    hermes_config = ModuleType("hermes_cli.config")

    def fake_load_config():
        return {"fake": "config"}

    def fake_cfg_get(config, *keys, default=None):
        assert keys == ("plugins", "entries", "hermes-workflows-approvals", "workflow_dbs")
        return json.dumps([{"name": "Palmer workflows", "path": str(db)}])

    setattr(hermes_config, "load_config", fake_load_config)
    setattr(hermes_config, "cfg_get", fake_cfg_get)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)
    monkeypatch.delenv("HERMES_WORKFLOWS_DB", raising=False)
    monkeypatch.delenv("HERMES_WORKFLOWS_DBS", raising=False)

    assert approvals._configured_dbs() == {"Palmer workflows": str(db)}



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
    assert approval["decision_token_error"] == "decision tokens require a configured workflow DB alias"
    assert "decision_token_approve" not in approval
    assert "decision_token_reject" not in approval


def test_decision_tokens_use_configured_db_alias_not_raw_db_path(tmp_path, monkeypatch):
    from hermes_workflows.hermes_plugin_approvals import parse_decision_token, register

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    monkeypatch.setenv("HERMES_WORKFLOWS_DBS", json.dumps({"launch": str(db)}))
    ctx = FakePluginContext()
    register(ctx)

    result = parse_tool_result(ctx.tools["workflow_approvals_list"]["handler"]({"db": "launch"}))

    token = result["approvals"][0]["decision_token_approve"]
    parsed = parse_decision_token(token)
    assert parsed is not None
    assert parsed["db"] == "launch"
    assert str(db) not in token


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
                "by": "operator",
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
    assert status["status"] == "running"
    assert status["waiting_on"] == "signal:approval.decision:approve_plugin_test"


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
                "by": "operator",
                "channel": "cli",
                "message_id": "manual-1",
                "resume": True,
            }
        )
    )

    assert result["receipt"]["resume_requested"] is True
    assert result["receipt"]["status"] == "running"
    completed = WorkflowEngine(db).drain("wf_plugin")
    assert completed.status == "completed"
    assert completed.result["followup_ran"] is True


@dataclass
class FakeSource:
    platform: Any = "discord"
    chat_id: str = "chat-42"
    user_id: str = "operator"
    user_name: str = "Operator"
    message_id: str = "msg-456"


@dataclass
class FakeEvent:
    text: str
    source: FakeSource


def test_gateway_hook_only_handles_exact_decision_token(tmp_path, monkeypatch):
    from hermes_workflows.hermes_plugin_approvals import decision_token, register

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    monkeypatch.setenv("HERMES_WORKFLOWS_DBS", json.dumps({"launch": str(db)}))
    ctx = FakePluginContext()
    register(ctx)
    hook = ctx.hooks["pre_gateway_dispatch"]

    unrelated = hook(event=FakeEvent("yes looks good", FakeSource()), gateway=None, session_store=None)
    assert unrelated is None

    token = decision_token("approve", "launch", "wf_plugin", "approve_plugin_test")
    handled = hook(event=FakeEvent(token, FakeSource()), gateway=None, session_store=None)

    assert handled["action"] == "skip"
    assert handled["reason"] == "workflow approval decision recorded"
    assert handled["receipt"]["action"] == "approve"
    assert handled["receipt"]["source"]["channel"] == "discord:chat-42"
    assert handled["receipt"]["source"]["message_id"] == "msg-456"
    hook_status = WorkflowEngine(db).workflow_status("wf_plugin")
    assert hook_status["status"] == "running"
    assert hook_status["waiting_on"] == "signal:approval.decision:approve_plugin_test"


def test_gateway_hook_rejects_path_tokens_without_creating_attacker_db(tmp_path):
    from hermes_workflows.hermes_plugin_approvals import register

    attacker_db = tmp_path / "attacker-controlled.sqlite"
    ctx = FakePluginContext()
    register(ctx)
    hook = ctx.hooks["pre_gateway_dispatch"]
    token = crafted_decision_token(
        "approve",
        {"db": str(attacker_db), "workflow_id": "wf_missing", "key": "approve_plugin_test"},
    )

    handled = hook(event=FakeEvent(token, FakeSource()), gateway=None, session_store=None)

    assert handled["action"] == "skip"
    assert handled["reason"] == "workflow approval token rejected"
    assert "explicit DB paths are not accepted from gateway tokens" in handled["error"]
    assert not attacker_db.exists()


def test_gateway_hook_handles_failed_exact_token_instead_of_falling_through(tmp_path, monkeypatch):
    from hermes_workflows.hermes_plugin_approvals import decision_token, register

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    monkeypatch.setenv("HERMES_WORKFLOWS_DBS", json.dumps({"launch": str(db)}))
    ctx = FakePluginContext()
    register(ctx)
    hook = ctx.hooks["pre_gateway_dispatch"]
    token = decision_token("approve", "launch", "wf_missing", "approve_plugin_test")

    handled = hook(event=FakeEvent(token, FakeSource()), gateway=None, session_store=None)

    assert handled["action"] == "skip"
    assert handled["reason"] == "workflow approval token rejected"
    assert "unknown workflow_id" in handled["error"]


def test_gateway_hook_rejects_token_shaped_invalid_action_instead_of_falling_through():
    from hermes_workflows.hermes_plugin_approvals import register

    ctx = FakePluginContext()
    register(ctx)
    hook = ctx.hooks["pre_gateway_dispatch"]
    token = crafted_decision_token(
        "maybe",
        {"db": "launch", "workflow_id": "wf_plugin", "key": "approve_plugin_test"},
    )

    handled = hook(event=FakeEvent(token, FakeSource()), gateway=None, session_store=None)

    assert handled["action"] == "skip"
    assert handled["reason"] == "workflow approval token rejected"
    assert "invalid approval token" in handled["error"]


def test_gateway_hook_rejects_non_canonical_token_instead_of_accepting_trailing_junk(tmp_path, monkeypatch):
    from hermes_workflows.hermes_plugin_approvals import decision_token, register

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    monkeypatch.setenv("HERMES_WORKFLOWS_DBS", json.dumps({"launch": str(db)}))
    ctx = FakePluginContext()
    register(ctx)
    hook = ctx.hooks["pre_gateway_dispatch"]
    token = decision_token("approve", "launch", "wf_plugin", "approve_plugin_test") + "!!!"

    handled = hook(event=FakeEvent(token, FakeSource()), gateway=None, session_store=None)

    assert handled["action"] == "skip"
    assert handled["reason"] == "workflow approval token rejected"
    assert "strict base64url" in handled["error"]
    assert WorkflowEngine(db).workflow_status("wf_plugin")["status"] == "waiting"


def test_terminal_workflow_rejects_conflicting_late_approval_decision(tmp_path):
    from hermes_workflows import ApprovalDecisionInput

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    engine = WorkflowEngine(db)
    engine.submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_plugin",
            key="approve_plugin_test",
            action="approve",
            by="operator",
            source={"kind": "human", "id": "operator", "channel": "discord", "message_id": "msg-approve"},
            idempotency_key="approval:approve",
        ),
        resume=True,
    )

    with pytest.raises(ValueError, match="already has a recorded decision"):
        WorkflowEngine(db).submit_approval_decision(
            ApprovalDecisionInput(
                workflow_id="wf_plugin",
                key="approve_plugin_test",
                action="reject",
                by="operator",
                source={"kind": "human", "id": "operator", "channel": "discord", "message_id": "msg-reject"},
                idempotency_key="approval:reject",
            ),
            resume=True,
        )


def test_cancelled_workflow_rejects_conflicting_late_recorded_approval_decision(tmp_path):
    from hermes_workflows import ApprovalDecisionInput

    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    engine = WorkflowEngine(db)
    engine.submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id="wf_plugin",
            key="approve_plugin_test",
            action="approve",
            by="operator",
            source={"kind": "human", "id": "operator", "channel": "discord", "message_id": "msg-approve"},
            idempotency_key="approval:approve",
        ),
        resume=False,
    )
    engine.cancel_workflow("wf_plugin", reason="stale", source={"kind": "operator"})

    with pytest.raises(ValueError, match="already has a recorded decision"):
        WorkflowEngine(db).submit_approval_decision(
            ApprovalDecisionInput(
                workflow_id="wf_plugin",
                key="approve_plugin_test",
                action="reject",
                by="operator",
                source={"kind": "human", "id": "operator", "channel": "discord", "message_id": "msg-reject"},
                idempotency_key="approval:reject",
            ),
            resume=False,
        )


def test_list_waiting_approvals_excludes_terminal_workflows(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = create_pending_approval(db)
    assert [approval.key for approval in engine.list_approvals(status="waiting")] == ["approve_plugin_test"]

    engine.cancel_workflow("wf_plugin", reason="stale approval card", source={"kind": "operator", "id": "unit"})

    assert WorkflowEngine(db).workflow_status("wf_plugin")["status"] == "cancelled"
    assert WorkflowEngine(db).list_approvals(status="waiting") == []


def test_approval_decision_signal_is_schema_unique_per_workflow_and_key(tmp_path):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    payload = {
        "signal_type": "approval.decision",
        "key": "approve_plugin_test",
        "payload": {"action": "approve", "by": "operator"},
        "source": {"kind": "human", "id": "operator", "channel": "discord", "message_id": "msg-1"},
    }

    with sqlite3.connect(db) as con:
        con.execute(
            """
            INSERT INTO workflow_events(workflow_id, seq, type, key, payload_json, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wf_plugin",
                99,
                "SignalReceived",
                "signal:approval.decision:approve_plugin_test",
                JsonCodec.dumps(payload),
                "approval:one",
                1,
            ),
        )

        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """
                INSERT INTO workflow_events(workflow_id, seq, type, key, payload_json, idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "wf_plugin",
                    100,
                    "SignalReceived",
                    "signal:approval.decision:approve_plugin_test",
                    JsonCodec.dumps({**payload, "payload": {"action": "reject", "by": "operator"}}),
                    "approval:two",
                    2,
                ),
            )

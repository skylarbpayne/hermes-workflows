from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

from hermes_workflows import WorkflowEngine
from tests.test_hermes_plugin_approvals import create_pending_approval


PLUGIN_DASHBOARD = Path("plugins/hermes-workflows-approvals/dashboard")


def load_dashboard_api():
    plugin_file = PLUGIN_DASHBOARD / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_workflows_dashboard_plugin_test", plugin_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(coro):
    return asyncio.run(coro)


def configure_test_dbs(monkeypatch, tmp_path, mapping: dict[str, str]) -> None:
    # The dashboard plugin also reads Hermes profile config when Hermes is
    # importable. Keep unit tests hermetic so a developer's live profile DB
    # aliases do not leak into test expectations.
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_WORKFLOWS_DB", raising=False)
    monkeypatch.setenv("HERMES_WORKFLOWS_DBS", json.dumps(mapping))


def test_dashboard_plugin_manifest_assets_and_backend_are_present():
    manifest_path = PLUGIN_DASHBOARD / "manifest.json"
    index_path = PLUGIN_DASHBOARD / "dist" / "index.js"
    style_path = PLUGIN_DASHBOARD / "dist" / "style.css"
    api_path = PLUGIN_DASHBOARD / "plugin_api.py"

    manifest = json.loads(manifest_path.read_text())

    assert manifest["name"] == "hermes-workflows-approvals"
    assert manifest["label"] == "Workflows"
    assert manifest["version"] == "0.2.0"
    assert (PLUGIN_DASHBOARD.parent / "plugin.yaml").read_text().splitlines()[1] == 'version: "0.2.0"'
    assert manifest["tab"]["path"] == "/workflows"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["css"] == "dist/style.css"
    assert manifest["api"] == "plugin_api.py"
    assert index_path.exists()
    assert style_path.exists()
    assert api_path.exists()
    index_js = index_path.read_text()
    assert "__HERMES_PLUGINS__.register" in index_js
    assert "/api/plugins/hermes-workflows-approvals" in index_js
    assert "onValueChange" in index_js
    assert "onChange" not in index_js
    assert "approval.approver" in index_js
    assert "dashboard-user" not in index_js


def test_dashboard_plugin_api_lists_configured_dbs_without_touching_credentials(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)})
    api = load_dashboard_api()

    result = run(api.list_dbs())

    assert result["count"] == 1
    assert result["dbs"][0] == {"name": "palmer-smoke", "path": str(db), "exists": True}


def test_dashboard_plugin_api_overview_includes_workflow_observability_and_redacted_approvals(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)})
    api = load_dashboard_api()

    result = run(api.overview(db="palmer-smoke", recent_events=10, command_limit=10, command_payload_chars=300))

    assert result["db"] == str(db)
    assert result["workflow_count"] == 1
    assert result["counts_by_status"] == {"waiting": 1}
    workflow = result["workflows"][0]
    assert workflow["workflow_id"] == "wf_plugin"
    assert workflow["status"] == "waiting"
    assert workflow["waiting_on"] == "signal:approval.decision:approve_plugin_test"
    assert workflow["event_count"] >= 2
    assert workflow["recent_events"]
    assert workflow["approvals"][0]["key"] == "approve_plugin_test"
    assert workflow["approvals"][0]["artifact"]["secret_token"] == "[REDACTED]"
    assert workflow["pending_commands"][0]["payload"]["artifact"]["secret_token"] == "[REDACTED]"
    assert workflow["command_history"][0]["payload_context"]["value"]["artifact"]["secret_token"] == "[REDACTED]"
    assert workflow["pending_commands"][0]["type"] == "notify_approval"
    assert workflow["diagnostics"][0]["label"] == "active_wait"


def test_dashboard_plugin_api_status_and_approval_decision_default_to_record_only(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)})
    api = load_dashboard_api()

    status = run(api.workflow_status("wf_plugin", db="palmer-smoke", recent_events=5, commands="recent"))
    assert status["workflow_id"] == "wf_plugin"
    assert status["approvals"][0]["artifact"]["secret_token"] == "[REDACTED]"
    assert "command_history" in status

    receipt = run(
        api.decide_approval(
            {
                "db": "palmer-smoke",
                "workflow_id": "wf_plugin",
                "key": "approve_plugin_test",
                "action": "approve",
                "by": "skylar",
                "channel": "dashboard-test",
                "message_id": "msg-dashboard-1",
            }
        )
    )

    assert receipt["success"] is True
    assert receipt["receipt"]["resume_requested"] is False
    assert receipt["receipt"]["status"] == "decision_recorded"
    assert "trusted workflow resumer" in receipt["next_step"]
    assert WorkflowEngine(db).workflow_status("wf_plugin")["status"] == "waiting"

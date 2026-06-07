from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

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


def configure_test_dbs(
    monkeypatch,
    tmp_path,
    mapping: dict[str, str],
    *,
    dashboard_approver: str | None = None,
    workflow_catalog: list[dict[str, object]] | None = None,
) -> None:
    # The dashboard plugin also reads Hermes profile config when Hermes is
    # importable. Keep unit tests hermetic so a developer's live profile DB
    # aliases do not leak into test expectations.
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_WORKFLOWS_DB", raising=False)
    monkeypatch.setenv("HERMES_WORKFLOWS_DBS", json.dumps(mapping))
    if workflow_catalog is None:
        monkeypatch.delenv("HERMES_WORKFLOWS_CATALOG", raising=False)
    else:
        monkeypatch.setenv("HERMES_WORKFLOWS_CATALOG", json.dumps(workflow_catalog))
    if dashboard_approver is None:
        monkeypatch.delenv("HERMES_WORKFLOWS_DASHBOARD_APPROVER_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_WORKFLOWS_DASHBOARD_APPROVER_ID", dashboard_approver)


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
    assert "window.prompt" not in index_js
    assert "dashboard-user" not in index_js


def test_dashboard_plugin_api_lists_configured_dbs_without_touching_credentials(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)})
    api = load_dashboard_api()

    result = run(api.list_dbs())

    assert result["count"] == 1
    assert result["dbs"][0] == {"name": "palmer-smoke", "exists": True}
    assert result["runtime_semantics"]["db_selector"].startswith("The dropdown selects a configured workflow DB alias")
    assert str(db) not in json.dumps(result)


def test_dashboard_plugin_api_overview_includes_workflow_observability_and_redacted_approvals(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)})
    api = load_dashboard_api()

    result = run(api.overview(db="palmer-smoke", recent_events=10, command_limit=10, command_payload_chars=300))

    assert result["db_alias"] == "palmer-smoke"
    assert str(db) not in json.dumps(result)
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
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)}, dashboard_approver="skylar")
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


def test_dashboard_plugin_api_rejects_explicit_db_paths(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)}, dashboard_approver="skylar")
    api = load_dashboard_api()

    with pytest.raises(Exception) as excinfo:
        run(api.overview(db=str(db)))

    assert getattr(excinfo.value, "status_code", None) == 400
    assert "configured DB alias" in str(getattr(excinfo.value, "detail", excinfo.value))


def test_dashboard_approval_requires_server_configured_approver(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)})
    api = load_dashboard_api()

    with pytest.raises(Exception) as excinfo:
        run(
            api.decide_approval(
                {
                    "db": "palmer-smoke",
                    "workflow_id": "wf_plugin",
                    "key": "approve_plugin_test",
                    "action": "approve",
                }
            )
        )

    assert getattr(excinfo.value, "status_code", None) == 403
    assert "dashboard_approver_id" in str(getattr(excinfo.value, "detail", excinfo.value))


def test_dashboard_approval_identity_is_server_derived_not_client_supplied(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)}, dashboard_approver="skylar")
    api = load_dashboard_api()

    receipt = run(
        api.decide_approval(
            {
                "db": "palmer-smoke",
                "workflow_id": "wf_plugin",
                "key": "approve_plugin_test",
                "action": "approve",
                "by": "attacker",
                "channel": "forged-channel",
                "message_id": "forged-message",
                "resume": True,
            }
        )
    )

    status = WorkflowEngine(db).workflow_status("wf_plugin")
    signal = [event for event in status["events"] if event["type"] == "SignalReceived"][-1]

    assert receipt["success"] is True
    assert receipt["receipt"]["resume_requested"] is False
    assert signal["payload"]["source"] == {
        "kind": "human",
        "id": "skylar",
        "channel": "hermes-dashboard",
        "message_id": signal["payload"]["source"]["message_id"],
    }
    assert signal["payload"]["payload"] == {"action": "approve", "by": "skylar"}


def test_dashboard_plugin_api_supports_catalog_run_history_artifacts_and_active_approval_detail(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    catalog = [
        {
            "id": "plugin-approval",
            "name": "Plugin approval smoke",
            "description": "Drafts an approval packet and waits for a human decision.",
            "workflow_ref": "tests.test_hermes_plugin_approvals:plugin_approval_workflow",
            "input_schema": {"type": "object", "properties": {}},
            "tags": ["approval", "smoke"],
        }
    ]
    configure_test_dbs(
        monkeypatch,
        tmp_path,
        {"palmer-smoke": str(db)},
        dashboard_approver="skylar",
        workflow_catalog=catalog,
    )
    api = load_dashboard_api()

    definitions = run(api.workflow_definitions(db="palmer-smoke"))
    assert definitions["count"] == 1
    definition = definitions["definitions"][0]
    assert definition["id"] == "plugin-approval"
    assert definition["runnable"] is True
    assert definition["runs"]["total"] == 1
    assert definition["runs"]["by_status"] == {"waiting": 1}

    launched = run(
        api.run_workflow(
            {
                "db": "palmer-smoke",
                "definition_id": "plugin-approval",
                "input": {},
            }
        )
    )
    assert launched["success"] is True
    launched_workflow_id = launched["run"]["workflow_id"]
    assert launched_workflow_id.startswith("wf_plugin_approval_")
    assert launched["run"]["status"] == "waiting"
    assert launched["run"]["workflow_ref"] == "tests.test_hermes_plugin_approvals:plugin_approval_workflow"
    assert str(db) not in json.dumps(launched)

    history = run(api.definition_runs("plugin-approval", db="palmer-smoke"))
    history_ids = [item["workflow_id"] for item in history["runs"]]
    assert launched_workflow_id in history_ids
    assert "wf_plugin" in history_ids
    assert next(item for item in history["runs"] if item["workflow_id"] == launched_workflow_id)["status"] == "waiting"
    assert str(db) not in json.dumps(history)

    status = run(api.run_status(launched_workflow_id, db="palmer-smoke"))
    assert status["run"]["workflow_id"] == launched_workflow_id
    assert status["artifacts"][0]["kind"] == "approval_artifact"
    assert status["artifacts"][0]["preview"]["secret_token"] == "[REDACTED]"
    assert status["artifacts"][0]["artifact_render"]["render"] == "inline-json"
    assert str(db) not in json.dumps(status)

    artifacts = run(api.run_artifacts(launched_workflow_id, db="palmer-smoke"))
    assert artifacts["count"] >= 1
    assert artifacts["artifacts"][0]["workflow_id"] == launched_workflow_id
    assert str(db) not in json.dumps(artifacts)

    approvals = run(api.active_approvals(db="palmer-smoke"))
    approval = next(item for item in approvals["approvals"] if item["workflow_id"] == launched_workflow_id)
    assert approval["headline"] == "Approve the plugin test packet?"
    assert approval["consequence"] == "Records approve/reject only; a trusted local resumer must continue the workflow."
    assert approval["risk"]["level"] == "low"
    assert approval["artifact_render"]["render"] == "inline-json"
    assert approval["artifact_preview"]["summary"] == "Plugin approval packet"
    assert approval["artifact_preview"]["secret_token"] == "[REDACTED]"
    assert str(db) not in json.dumps(approvals)

    detail = run(api.approval_detail(db="palmer-smoke", workflow_id=launched_workflow_id, key="approve_plugin_test"))
    assert detail["approval"]["key"] == "approve_plugin_test"
    assert detail["decision_semantics"]["resume"] is False
    assert detail["what_you_are_approving"]["action"] == "approve_plugin_test"
    assert detail["timeline"][0]["type"] == "WorkflowStarted"
    assert detail["timeline"][-1]["type"] == "ApprovalRequested"
    assert str(db) not in json.dumps(detail)


def test_dashboard_run_launch_rejects_browser_supplied_workflow_id(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(
        monkeypatch,
        tmp_path,
        {"palmer-smoke": str(db)},
        workflow_catalog=[
            {
                "id": "plugin-approval",
                "name": "Plugin approval smoke",
                "workflow_ref": "tests.test_hermes_plugin_approvals:plugin_approval_workflow",
            }
        ],
    )
    api = load_dashboard_api()

    with pytest.raises(Exception) as excinfo:
        run(api.run_workflow({"db": "palmer-smoke", "definition_id": "plugin-approval", "workflow_id": "wf_plugin"}))

    assert getattr(excinfo.value, "status_code", None) == 400
    assert "workflow_id" in str(getattr(excinfo.value, "detail", excinfo.value))


def test_dashboard_inferred_history_definitions_are_not_browser_runnable(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"palmer-smoke": str(db)})
    api = load_dashboard_api()

    definitions = run(api.workflow_definitions(db="palmer-smoke"))
    inferred = definitions["definitions"][0]
    assert inferred["tags"] == ["inferred"]
    assert inferred["runnable"] is False

    with pytest.raises(Exception) as excinfo:
        run(api.run_workflow({"db": "palmer-smoke", "definition_id": inferred["id"]}))

    assert getattr(excinfo.value, "status_code", None) == 403
    assert "workflow_catalog" in str(getattr(excinfo.value, "detail", excinfo.value))

def test_dashboard_artifact_render_descriptors_redact_local_media_paths():
    api = load_dashboard_api()

    card = api._approval_card(
        {
            "workflow_id": "wf_media",
            "workflow_ref": "pkg:flow",
            "key": "approve_media",
            "status": "waiting",
            "prompt": "Approve generated image?",
            "artifact": {
                "kind": "image",
                "media_type": "image/png",
                "path": "/Users/skylarpayne/private/generated.png",
                "caption": "Generated preview",
            },
        },
        db_alias="palmer-smoke",
    )

    assert card["artifact_render"] == {
        "kind": "image",
        "render": "file-reference",
        "persisted": "workflow_history",
        "servable_by_dashboard": False,
        "media_type": "image/png",
        "reference": {"type": "local_path", "field": "path", "href": "[REDACTED_LOCAL_PATH]"},
        "warning": "Local/private files are not served by the dashboard; attach or expose them through an explicit artifact store before rendering media inline.",
    }
    assert card["artifact_preview"]["path"] == "[REDACTED_LOCAL_PATH]"
    assert "/Users/skylarpayne" not in json.dumps(card)

    audio = api._artifact_descriptor({"kind": "audio", "url": "https://example.invalid/review.mp3", "media_type": "audio/mpeg"})
    assert audio["kind"] == "audio"
    assert audio["render"] == "media-reference"
    assert audio["reference"] == {"type": "url", "href": "https://example.invalid/review.mp3"}


def test_dashboard_plugin_frontend_exposes_full_workflows_console_navigation():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    for label in ("Overview", "Workflows", "Runs", "Approvals", "Artifacts"):
        assert label in index_js
    for phrase in (
        "Run workflow",
        "Needs my approval",
        "What you are approving",
        "Record-only decision",
        "View approval",
        "Run history",
        "Workflow DB alias",
        "Configured SQLite alias; not a registry",
        "Dashboard approval buttons record only",
        "artifact: ",
    ):
        assert phrase in index_js
    assert ".hwf-shell" in style_css
    assert ".hwf-approval-detail" in style_css

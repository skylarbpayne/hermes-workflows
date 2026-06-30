from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

import pytest

from hermes_workflows import ApprovalDecisionInput, Workflow, WorkflowEngine, agent, approve, ask, gather, pipeline, select, step, workflow
from hermes_workflows.workflows.coding import coding_workflow
from tests.test_hermes_plugin_approvals import create_pending_approval


PLUGIN_DASHBOARD = Path("plugins/hermes-workflows-approvals/dashboard")


@step
async def dashboard_path_artifact_step(artifact):
    return {"copied_to": "/Users/operator/private/generated-copy.png", "artifact": artifact}


@workflow
async def dashboard_path_artifact_workflow(inputs):
    artifact = inputs["artifact"]
    await dashboard_path_artifact_step(artifact)
    decision = await approve(
        key="approve_path_artifact",
        prompt="Approve path artifact?",
        artifact=artifact,
        allowed=["approve", "reject"],
    )
    return {"decision": decision, "artifact": artifact, "saved_at": "/Users/operator/private/final-report.pdf"}


@workflow
async def dashboard_named_human_approval_workflow(inputs):
    decision = await approve(
        key="approve_named_human",
        prompt="Approve named-human dashboard smoke?",
        artifact={"summary": "Named human approval packet"},
        allowed=["approve", "reject"],
    )
    return {"approved": decision.approved, "actor": decision.get("by"), "source_channel": decision.get("source", {}).get("channel")}




@workflow
async def dashboard_ask_workflow(inputs):
    response = await ask(
        "Review dashboard ask response?",
        key="review_dashboard_payload",
        input={"summary": "dashboard ask packet"},
    )
    return {"response": response}


@dataclass
class DashboardReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@workflow
async def dashboard_review_decision_workflow(inputs):
    response = await ask(
        "Review dashboard review decision?",
        key="review_dashboard_decision",
        input={"summary": "dashboard review packet"},
        choice=["approve", "request_changes"],
        returns=DashboardReviewDecision,
    )
    return {"response": {"action": response.action, "feedback": response.feedback}}


@workflow
async def dashboard_select_workflow(inputs):
    selected = await select(
        "select_dashboard_payload",
        [
            {"title": "Guidance", "summary": "Skills guide behavior."},
            {"title": "Commitments", "summary": "Workflows preserve obligations."},
        ],
        returns=dict,
    )
    return {"selected": selected}


@step
async def dashboard_dynamic_seed(value):
    return {"seed": value}


@step
async def dashboard_dynamic_left(seed):
    return {"side": "left", "seed": seed["seed"]}


@step
async def dashboard_dynamic_right(seed):
    return {"side": "right", "seed": seed["seed"]}


@step
async def dashboard_dynamic_join(left, right):
    return {"joined": [left["side"], right["side"]]}


@workflow
async def dashboard_dynamic_topology_workflow(inputs):
    seed = await dashboard_dynamic_seed(inputs["value"])
    left, right = await gather(
        dashboard_dynamic_left(seed),
        dashboard_dynamic_right(seed),
    )
    joined = await dashboard_dynamic_join(left, right)
    return {"joined": joined}


@step
async def dashboard_pipeline_draft(section):
    return {"section": section, "draft": f"draft:{section}"}


@step
async def dashboard_pipeline_humanize(draft):
    return {"section": draft["section"], "humanized": f"humanized:{draft['draft']}"}


@workflow
async def dashboard_pipeline_lane_workflow(inputs):
    return await pipeline(
        inputs["sections"],
        lambda section: dashboard_pipeline_draft(section),
        lambda draft: dashboard_pipeline_humanize(draft),
    )


DASHBOARD_GENERATED_CHILD_SOURCE = '''
from hermes_workflows import approve, step, workflow

@step
async def dashboard_generated_child_step(inputs):
    return {"generated": inputs["value"]}

@workflow
async def dashboard_generated_child(inputs):
    return await dashboard_generated_child_step(inputs)
'''


@workflow
async def dashboard_generated_workflow_source_pipeline(inputs):
    processor = await agent(
        "build_generated_child",
        prompt="Return a generated workflow that processes one dashboard item.",
        input={"value": inputs["value"]},
        returns=Workflow,
        mock_output={"source": DASHBOARD_GENERATED_CHILD_SOURCE, "symbol": "dashboard_generated_child"},
    )
    return await processor({"value": inputs["value"]}, key="demo")


def load_dashboard_api():
    plugin_file = PLUGIN_DASHBOARD / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_workflows_dashboard_plugin_test", plugin_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(coro):
    return asyncio.run(coro)


def dashboard_operator_response_counts(db: Path, workflow_id: str, key: str) -> dict[str, int | str | None]:
    engine = WorkflowEngine(db)
    with engine._connect() as con:
        signal_count = con.execute(
            """
            SELECT COUNT(*) FROM workflow_events
            WHERE workflow_id = ? AND type = 'SignalReceived' AND key = ?
            """,
            (workflow_id, f"signal:operator.response:{key}"),
        ).fetchone()[0]
        step_count = con.execute(
            """
            SELECT COUNT(*) FROM workflow_events
            WHERE workflow_id = ? AND type = 'StepCompleted' AND key = ?
            """,
            (workflow_id, key),
        ).fetchone()[0]
        command_rows = con.execute(
            """
            SELECT status FROM workflow_commands_outbox
            WHERE workflow_id = ? AND type = 'run_workflow' AND key = 'workflow:run'
            """,
            (workflow_id,),
        ).fetchall()
    return {
        "signals": signal_count,
        "steps": step_count,
        "commands": len(command_rows),
        "command_status": command_rows[0]["status"] if command_rows else None,
    }


def configure_test_dbs(
    monkeypatch,
    tmp_path,
    mapping: dict[str, str],
    *,
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
    assert "hwf-active-source" in index_js
    assert "onValueChange" not in index_js
    assert "onChange" not in index_js
    assert "approval.reviewer" not in index_js
    assert "approval." + "approver" not in index_js
    assert "approver" + ":" not in index_js
    assert "window.prompt" not in index_js
    assert "idempotency_key" in index_js
    assert "dashboard-user" not in index_js


def test_dashboard_run_rows_truncate_long_ids_and_waiting_keys_without_vertical_wrap():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert "hwf-waiting-on" in index_js
    assert "hwf-run-signals" in index_js
    assert "grid-template-columns: minmax(12rem, 1.1fr) minmax(10rem, 0.7fr) minmax(0, 1.4fr) max-content" in style_css
    assert ".hwf-run-id" in style_css
    assert "white-space: nowrap" in style_css
    assert "text-overflow: ellipsis" in style_css
    run_id_block = style_css.split(".hwf-run-id {", 1)[1].split("}", 1)[0]
    assert "word-break" not in run_id_block
    assert "overflow-wrap" not in run_id_block


def test_dashboard_artifact_run_groups_are_collapsible():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert 'e("details", { key: workflowId, className: "hwf-run-artifacts" }' in index_js
    assert 'e("summary", { className: "hwf-run-artifacts-summary" }' in index_js
    assert "hwf-run-artifacts-body" in index_js
    assert ".hwf-run-artifacts-summary" in style_css
    assert ".hwf-run-artifacts-body" in style_css


def test_dashboard_frontend_exposes_workflow_code_and_run_dag_affordances():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert "WorkflowSourceModal" in index_js
    assert '"View code"' in index_js
    assert "language-python" in index_js
    assert "RunDag" in index_js
    assert '"Run DAG"' in index_js
    assert "selectedDagNode" in index_js
    assert "Artifacts from this step" in index_js
    assert "/definitions/" in index_js and "/source" in index_js
    assert "/dag" in index_js
    assert '"Open generated Workflow source"' in index_js
    assert '"Source hash"' in index_js
    assert '"Provenance"' in index_js
    assert 'render.render === "python-source"' in index_js
    assert ".hwf-code-block" in style_css
    assert ".hwf-workflow-source-preview" in style_css
    assert ".hwf-dag-node" in style_css
    assert ".hwf-dag-node-selected" in style_css


def test_dashboard_code_highlighting_uses_subtle_token_colors_instead_of_loud_highlights():
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()
    keyword_rule = style_css[style_css.index(".hwf-code-keyword {") : style_css.index(".hwf-code-string {")]

    assert "--hwf-code-keyword" in style_css
    assert "--hwf-code-string" in style_css
    assert "--hwf-code-comment" in style_css
    assert "color: var(--hwf-code-keyword)" in keyword_rule
    assert "font-weight: 700" not in keyword_rule
    assert "color: #c084fc" not in style_css
    assert "color: #86efac" not in style_css


def test_dashboard_code_highlighting_handles_real_python_and_generated_workflows():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert "function GeneratedPythonWorkflowPreview" in index_js
    assert "isGeneratedPythonWorkflowArtifact(value)" in index_js
    assert 'value.kind === "generated_workflow.approval.v1"' in index_js
    assert '"Generated Python workflow"' in index_js
    assert 'e(PythonCode, { className: "language-python", code: value.source })' in index_js

    assert '"""[\\s\\S]*?"""' in index_js
    assert "'''[\\s\\S]*?'''" in index_js
    assert "@[A-Za-z_]\\w*" in index_js
    assert "hwf-code-decorator" in index_js
    assert "hwf-code-number" in index_js
    assert ".hwf-generated-source" in style_css
    assert ".hwf-generated-code-block" in style_css


def test_dashboard_frontend_exposes_visual_run_dag_graph():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert "selectedDagNodeId" in index_js
    assert "hwf-dag-graph" in index_js
    assert "Workflow run DAG graph" in index_js
    assert "hwf-dag-edge-svg" in index_js
    assert "hwf-dag-edge-line" in index_js
    assert "markerEnd" in index_js
    assert "layoutDagNodes" in index_js
    assert "completion_mode === \"approval\"" in index_js
    assert "completion_mode === \"worker\"" in index_js
    assert "incomingByTarget" in index_js
    assert "data-dag-node-id" in index_js
    assert "Artifacts from this step" in index_js
    assert "expandedChildWorkflowIds" in index_js
    assert "expandInlineChildWorkflows" in index_js
    assert '"Expand inline DAG"' in index_js
    assert '"Collapse inline DAG"' in index_js
    assert "hwf-dag-node-child-inline" in index_js
    assert "Child workflow DAG" not in index_js
    assert "data-child-workflow-id" not in index_js
    assert "e(RunDag, { db: props.db, workflowId: selectedChildWorkflowId" not in index_js
    assert "hwf-dag-strip" not in index_js

    assert ".hwf-dag-graph" in style_css
    assert ".hwf-dag-edge-svg" in style_css
    assert ".hwf-dag-edge-line" in style_css
    assert ".hwf-dag-layer" in style_css
    assert ".hwf-dag-inspector" in style_css
    assert ".hwf-dag-node-kind-child_workflow" in style_css
    assert ".hwf-dag-node-child-inline" in style_css
    assert ".hwf-child-workflow-summary" in style_css


def test_dashboard_frontend_inspect_run_waits_for_run_status_payload():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()

    assert "const runStatus = status.data && status.data.run;" in index_js
    assert "status && status.data && !runStatus" in index_js
    assert "status.data.run.status" not in index_js
    assert "status.data.run.event_count" not in index_js
    assert "status.data.run.workflow_id" not in index_js


def test_dashboard_frontend_overview_inspect_run_opens_runs_inspector():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()

    assert "onInspect: function () {}" not in index_js
    assert "const inspectedRunState = useState(null);" in index_js
    assert "function inspectRun(run)" in index_js
    assert "setActiveTab(\"Runs\");" in index_js
    assert "onInspectRun: inspectRun" in index_js
    assert "inspectRun: inspectedRun" in index_js


def test_dashboard_frontend_inspect_run_inspector_is_visible_before_run_list():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()

    inspector_index = index_js.index('selected && e(Card, { className: "hwf-inspector" }')
    run_list_index = index_js.index('runs.map(function (run)')
    assert inspector_index < run_list_index


def test_dashboard_frontend_runs_panel_passes_rows_as_real_children():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()

    assert 'const runs = Array.isArray(props.runs) ? props.runs : [];' in index_js
    assert '].filter(Boolean).concat(runs.map(function (run) {' in index_js
    assert 'React.createElement.apply(React, ["div", { className: "hwf-panel" }].concat(children));' in index_js


def test_dashboard_frontend_runs_tab_shows_empty_or_error_state_instead_of_blank_panel():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()

    assert "No runs found for the active source." in index_js
    assert "dashboard is looking at the wrong state source" in index_js
    assert "loading: runsData.loading" in index_js
    assert "error: runsData.error" in index_js


def test_dashboard_plugin_api_lists_configured_dbs_without_touching_credentials(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    result = run(api.list_dbs())

    assert result["count"] == 1
    assert result["dbs"][0] == {"name": "runtime-smoke", "exists": True}
    assert result["runtime_semantics"]["state_source"].startswith("The dashboard uses the configured workflow DB alias")
    assert str(db) not in json.dumps(result)


def test_dashboard_plugin_api_marks_default_active_state_source(tmp_path, monkeypatch):
    missing_db = tmp_path / "000-missing.sqlite"
    default_db = tmp_path / "workflow.sqlite"
    create_pending_approval(default_db)
    configure_test_dbs(monkeypatch, tmp_path, {"aaa-missing": str(missing_db), "default": str(default_db)})
    monkeypatch.setenv("HERMES_WORKFLOWS_DB", str(default_db))
    api = load_dashboard_api()

    result = run(api.list_dbs())

    assert result["active_source"] == {"name": "default", "exists": True}
    assert [db["name"] for db in result["dbs"]] == ["aaa-missing", "default"]
    assert result["dbs"][0]["exists"] is False
    assert str(default_db) not in json.dumps(result)


def test_dashboard_plugin_api_prefers_single_populated_source_over_empty_default(tmp_path, monkeypatch):
    default_db = tmp_path / "empty-default.sqlite"
    populated_db = tmp_path / "palmer-workflows.sqlite"
    WorkflowEngine(str(default_db))
    create_pending_approval(populated_db)
    configure_test_dbs(monkeypatch, tmp_path, {"default": str(default_db), "Palmer workflows": str(populated_db)})
    api = load_dashboard_api()

    sources = run(api.list_dbs())
    runs = run(api.runs())

    assert sources["active_source"] == {"name": "Palmer workflows", "exists": True}
    assert runs["db_alias"] == "Palmer workflows"
    assert runs["count"] == 1
    assert str(populated_db) not in json.dumps(sources)
    assert str(populated_db) not in json.dumps(runs)


def test_dashboard_plugin_api_overview_includes_workflow_observability_and_redacts_secrets(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    result = run(api.overview(db="runtime-smoke", recent_events=10, command_limit=10, command_payload_chars=300))

    assert result["db_alias"] == "runtime-smoke"
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
    command_payloads = [item["payload_context"].get("value", {}) for item in workflow["command_history"]]
    notify_payload = next(item for item in command_payloads if isinstance(item, dict) and "artifact" in item)
    assert notify_payload["artifact"]["secret_token"] == "[REDACTED]"
    assert workflow["pending_commands"][0]["type"] == "notify_approval"
    assert workflow["diagnostics"][0]["label"] == "active_wait"


def test_dashboard_plugin_api_approval_decision_records_and_resumes(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    status = run(api.workflow_status("wf_plugin", db="runtime-smoke", recent_events=5, commands="recent"))
    assert status["workflow_id"] == "wf_plugin"
    assert status["approvals"][0]["artifact"]["secret_token"] == "[REDACTED]"
    assert "command_history" in status

    receipt = run(
        api.decide_approval(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_plugin",
                "key": "approve_plugin_test",
                "action": "approve",
                "channel": "dashboard-test",
                "message_id": "msg-dashboard-1",
            }
        )
    )

    assert receipt["success"] is True
    assert receipt["receipt"]["resume_requested"] is True
    assert receipt["receipt"]["status"] == "running"
    assert receipt["post_resume"]["status"] == "running"
    completed = WorkflowEngine(db).drain("wf_plugin")
    assert completed.status == "completed"
    assert WorkflowEngine(db).workflow_status("wf_plugin")["status"] == "completed"


def test_dashboard_plugin_api_approval_decision_loads_project_workflow_ref_before_resume(tmp_path, monkeypatch):
    project = tmp_path / "workflow-project"
    package = project / "project_flows"
    db_dir = project / ".hermes"
    package.mkdir(parents=True)
    db_dir.mkdir()
    (package / "__init__.py").write_text("")
    (package / "email_ops_like.py").write_text(
        "from hermes_workflows import approve, workflow\n"
        "\n"
        "@workflow\n"
        "async def project_email_ops_workflow(inputs):\n"
        "    decision = await approve(key='approve_project_entity', prompt='Approve project entity?')\n"
        "    return {'decision': decision.get('action')}\n"
    )
    workflow_ref = "project_flows.email_ops_like:project_email_ops_workflow"
    db = db_dir / "workflows.sqlite"

    sys.path.insert(0, str(project))
    try:
        from hermes_workflows.engine import _WORKFLOW_REGISTRY

        module = importlib.import_module("project_flows.email_ops_like")
        project_email_ops_workflow = module.project_email_ops_workflow

        WorkflowEngine(db).run_until_idle(
            project_email_ops_workflow,
            {"_registry_name": "project-email-ops"},
            workflow_id="wf_project_import_required",
            workflow_ref=workflow_ref,
        )
    finally:
        if str(project) in sys.path:
            sys.path.remove(str(project))
    sys.modules.pop("project_flows.email_ops_like", None)
    sys.modules.pop("project_flows", None)
    _WORKFLOW_REGISTRY.pop("project_email_ops_workflow", None)

    configure_test_dbs(monkeypatch, tmp_path, {"project-db": str(db)})
    api = load_dashboard_api()

    receipt = run(
        api.decide_approval(
            {
                "db": "project-db",
                "workflow_id": "wf_project_import_required",
                "key": "approve_project_entity",
                "action": "approve",
            }
        )
    )

    assert receipt["success"] is True
    assert receipt["post_resume"]["status"] == "running"
    completed = WorkflowEngine(db).drain("wf_project_import_required")
    assert completed.status == "completed"
    assert WorkflowEngine(db).workflow_status("wf_project_import_required")["status"] == "completed"


def test_dashboard_plugin_api_rejects_explicit_db_paths(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    with pytest.raises(Exception) as excinfo:
        run(api.overview(db=str(db)))

    assert getattr(excinfo.value, "status_code", None) == 400
    assert "configured DB alias" in str(getattr(excinfo.value, "detail", excinfo.value))


def test_dashboard_approval_does_not_require_or_invent_actor_identity(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    receipt = run(
        api.decide_approval(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_plugin",
                "key": "approve_plugin_test",
                "action": "approve",
                "by": "browser-spoof",
                "source": {"id": "browser-spoof"},
            }
        )
    )

    status = WorkflowEngine(db).workflow_status("wf_plugin")
    signal = [event for event in status["events"] if event["type"] == "SignalReceived"][-1]

    assert receipt["success"] is True
    assert "by" not in receipt["receipt"]
    assert signal["payload"]["payload"] == {"action": "approve"}
    assert signal["payload"]["source"] == {
        "channel": "hermes-dashboard",
        "message_id": signal["payload"]["source"]["message_id"],
    }


def test_dashboard_approval_strips_browser_actor_and_provenance(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    receipt = run(
        api.decide_approval(
            {
                "db": "runtime-smoke",
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
    assert receipt["receipt"]["resume_requested"] is True
    assert signal["payload"]["source"] == {
        "channel": "hermes-dashboard",
        "message_id": signal["payload"]["source"]["message_id"],
    }
    assert "by" not in receipt["receipt"]
    assert signal["payload"]["payload"] == {"action": "approve"}


def test_dashboard_approval_records_action_without_actor_identity(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_named_human_approval_workflow,
        {},
        workflow_id="wf_named_human",
        workflow_ref="tests.test_dashboard_plugin:dashboard_named_human_approval_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    receipt = run(
        api.decide_approval(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_named_human",
                "key": "approve_named_human",
                "action": "approve",
            }
        )
    )

    assert receipt["success"] is True
    assert "by" not in receipt["receipt"]
    assert receipt["post_resume"]["status"] == "running"
    completed = WorkflowEngine(db).drain("wf_named_human")
    assert completed.status == "completed"
    status = WorkflowEngine(db).workflow_status("wf_named_human")
    signal = [event for event in status["events"] if event["type"] == "SignalReceived"][-1]
    assert status["result"] == {"approved": True, "actor": None, "source_channel": "hermes-dashboard"}
    assert signal["payload"]["source"]["channel"] == "hermes-dashboard"
    assert signal["payload"]["payload"] == {"action": "approve"}


def test_dashboard_approval_has_no_permission_or_actor_check(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_named_human_approval_workflow,
        {},
        workflow_id="wf_named_human_mismatch",
        workflow_ref="tests.test_dashboard_plugin:dashboard_named_human_approval_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    receipt = run(
        api.decide_approval(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_named_human_mismatch",
                "key": "approve_named_human",
                "action": "approve",
            }
        )
    )

    assert receipt["success"] is True
    assert "by" not in receipt["receipt"]
    assert receipt["post_resume"]["status"] == "running"
    completed = WorkflowEngine(db).drain("wf_named_human_mismatch")
    assert completed.status == "completed"
    status = WorkflowEngine(db).workflow_status("wf_named_human_mismatch")
    signal = [event for event in status["events"] if event["type"] == "SignalReceived"][-1]
    assert status["result"] == {"approved": True, "actor": None, "source_channel": "hermes-dashboard"}
    assert signal["payload"]["source"]["channel"] == "hermes-dashboard"
    assert signal["payload"]["payload"] == {"action": "approve"}


def test_dashboard_review_response_does_not_require_or_invent_approver_identity(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_ask_workflow,
        {},
        workflow_id="wf_dashboard_ask_response",
        workflow_ref="tests.test_dashboard_plugin:dashboard_ask_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    receipt = run(
        api.respond_review_request(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_dashboard_ask_response",
                "key": "review_dashboard_payload",
                "payload": {"action": "approve", "by": "browser-spoof", "source": {"id": "browser"}},
            }
        )
    )

    assert receipt["success"] is True
    assert "by" not in receipt["receipt"]
    assert dashboard_operator_response_counts(db, "wf_dashboard_ask_response", "review_dashboard_payload") == {
        "signals": 1,
        "steps": 1,
        "commands": 1,
        "command_status": "pending",
    }
    completed = WorkflowEngine(db).drain("wf_dashboard_ask_response")
    assert completed.status == "completed"
    assert completed.result["response"] == {"action": "approve"}
    signal = [event for event in WorkflowEngine(db).events("wf_dashboard_ask_response") if event["type"] == "SignalReceived"][-1]
    assert signal["payload"]["payload"] == {"action": "approve"}
    assert signal["payload"]["source"] == {
        "channel": "hermes-dashboard",
        "message_id": signal["payload"]["source"]["message_id"],
    }


def test_dashboard_review_response_idempotency_key_replay_does_not_duplicate_continuation(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_ask_workflow,
        {},
        workflow_id="wf_dashboard_ask_idempotent_response",
        workflow_ref="tests.test_dashboard_plugin:dashboard_ask_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()
    body = {
        "db": "runtime-smoke",
        "workflow_id": "wf_dashboard_ask_idempotent_response",
        "key": "review_dashboard_payload",
        "payload": {"action": "approve"},
        "idempotency_key": "review-response-idempotency-key-1",
    }

    first = run(api.respond_review_request(body))
    second = run(api.respond_review_request(body))

    assert first["success"] is True
    assert second["success"] is True
    assert dashboard_operator_response_counts(db, "wf_dashboard_ask_idempotent_response", "review_dashboard_payload") == {
        "signals": 1,
        "steps": 1,
        "commands": 1,
        "command_status": "pending",
    }
    signal = [event for event in WorkflowEngine(db).events("wf_dashboard_ask_idempotent_response") if event["type"] == "SignalReceived"][-1]
    assert signal["payload"]["source"]["message_id"] == "dashboard:review-response-idempotency-key-1"

    conflicting_body = dict(body)
    conflicting_body["payload"] = {"action": "request_changes", "feedback": "same key different payload"}
    with pytest.raises(Exception, match="idempotency key was reused with a different decision/response"):
        run(api.respond_review_request(conflicting_body))
    assert dashboard_operator_response_counts(db, "wf_dashboard_ask_idempotent_response", "review_dashboard_payload") == {
        "signals": 1,
        "steps": 1,
        "commands": 1,
        "command_status": "pending",
    }


def test_dashboard_review_response_conflicting_retry_does_not_duplicate_continuation(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_ask_workflow,
        {},
        workflow_id="wf_dashboard_ask_conflicting_response",
        workflow_ref="tests.test_dashboard_plugin:dashboard_ask_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    run(
        api.respond_review_request(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_dashboard_ask_conflicting_response",
                "key": "review_dashboard_payload",
                "payload": {"action": "approve"},
            }
        )
    )
    with pytest.raises(Exception, match="already has a recorded decision/response"):
        run(
            api.respond_review_request(
                {
                    "db": "runtime-smoke",
                    "workflow_id": "wf_dashboard_ask_conflicting_response",
                    "key": "review_dashboard_payload",
                    "payload": {"action": "request_changes", "feedback": "conflicting retry"},
                }
            )
        )

    assert dashboard_operator_response_counts(db, "wf_dashboard_ask_conflicting_response", "review_dashboard_payload") == {
        "signals": 1,
        "steps": 1,
        "commands": 1,
        "command_status": "pending",
    }


def test_dashboard_review_response_rolls_back_if_continuation_enqueue_fails(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_ask_workflow,
        {},
        workflow_id="wf_dashboard_ask_atomic_rollback",
        workflow_ref="tests.test_dashboard_plugin:dashboard_ask_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("forced enqueue failure")

    monkeypatch.setattr(api.WorkflowEngine, "_enqueue_workflow_run_row", fail_enqueue)

    with pytest.raises(Exception, match="forced enqueue failure"):
        run(
            api.respond_review_request(
                {
                    "db": "runtime-smoke",
                    "workflow_id": "wf_dashboard_ask_atomic_rollback",
                    "key": "review_dashboard_payload",
                    "payload": {"action": "approve"},
                }
            )
        )

    assert dashboard_operator_response_counts(db, "wf_dashboard_ask_atomic_rollback", "review_dashboard_payload") == {
        "signals": 0,
        "steps": 0,
        "commands": 1,
        "command_status": "completed",
    }
    status = WorkflowEngine(db).workflow_status("wf_dashboard_ask_atomic_rollback")
    assert status["status"] == "waiting"
    assert status["waiting_on"] == "signal:operator.response:review_dashboard_payload"
    assert status["review_requests"][0]["status"] == "waiting"


def test_dashboard_review_response_with_feedback_is_not_silent_approval(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_review_decision_workflow,
        {},
        workflow_id="wf_dashboard_ask_feedback",
        workflow_ref="tests.test_dashboard_plugin:dashboard_review_decision_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    receipt = run(
        api.respond_review_request(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_dashboard_ask_feedback",
                "key": "review_dashboard_decision",
                "payload": {"action": "approve", "feedback": "start from first principles", "by": "browser-spoof"},
            }
        )
    )

    assert receipt["success"] is True
    completed = WorkflowEngine(db).drain("wf_dashboard_ask_feedback")
    assert completed.status == "completed"
    assert completed.result["response"] == {"action": "request_changes", "feedback": "start from first principles"}
    signal = [event for event in WorkflowEngine(db).events("wf_dashboard_ask_feedback") if event["type"] == "SignalReceived"][-1]
    assert signal["payload"]["payload"] == {"action": "request_changes", "feedback": "start from first principles"}
    assert "by" not in signal["payload"]["payload"]


def test_dashboard_select_response_does_not_require_dashboard_actor_identity(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_select_workflow,
        {},
        workflow_id="wf_dashboard_select_response",
        workflow_ref="tests.test_dashboard_plugin:dashboard_select_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    receipt = run(
        api.respond_review_request(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_dashboard_select_response",
                "key": "select_dashboard_payload",
                "payload": {"title": "Commitments", "summary": "Workflows preserve obligations."},
            }
        )
    )

    assert receipt["success"] is True
    completed = WorkflowEngine(db).drain("wf_dashboard_select_response")
    assert completed.status == "completed"
    assert completed.result["selected"] == {"title": "Commitments", "summary": "Workflows preserve obligations."}


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
        {"runtime-smoke": str(db)},
        workflow_catalog=catalog,
    )
    api = load_dashboard_api()

    definitions = run(api.workflow_definitions(db="runtime-smoke"))
    assert definitions["count"] == 1
    definition = definitions["definitions"][0]
    assert definition["id"] == "plugin-approval"
    assert definition["runnable"] is True
    assert definition["runs"]["total"] == 1
    assert definition["runs"]["by_status"] == {"waiting": 1}

    launched = run(
        api.run_workflow(
            {
                "db": "runtime-smoke",
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

    history = run(api.definition_runs("plugin-approval", db="runtime-smoke"))
    history_ids = [item["workflow_id"] for item in history["runs"]]
    assert launched_workflow_id in history_ids
    assert "wf_plugin" in history_ids
    assert next(item for item in history["runs"] if item["workflow_id"] == launched_workflow_id)["status"] == "waiting"
    assert str(db) not in json.dumps(history)

    status = run(api.run_status(launched_workflow_id, db="runtime-smoke"))
    assert status["run"]["workflow_id"] == launched_workflow_id
    assert status["artifacts"][0]["kind"] == "approval_artifact"
    assert status["artifacts"][0]["preview"]["secret_token"] == "[REDACTED]"
    assert status["artifacts"][0]["artifact_render"]["render"] == "inline-json"
    assert str(db) not in json.dumps(status)

    artifacts = run(api.run_artifacts(launched_workflow_id, db="runtime-smoke"))
    assert artifacts["count"] >= 1
    assert artifacts["artifacts"][0]["workflow_id"] == launched_workflow_id
    assert str(db) not in json.dumps(artifacts)

    approvals = run(api.active_approvals(db="runtime-smoke"))
    approval = next(item for item in approvals["approvals"] if item["workflow_id"] == launched_workflow_id)
    assert approval["headline"] == "Approve the plugin test packet?"
    assert approval["consequence"] == "Records approve/reject with human provenance and creates an inspectable workflow continuation."
    assert approval["risk"]["level"] == "low"
    assert approval["artifact_render"]["render"] == "inline-json"
    assert approval["artifact_preview"]["summary"] == "Plugin approval packet"
    assert approval["artifact_preview"]["secret_token"] == "[REDACTED]"
    assert str(db) not in json.dumps(approvals)

    detail = run(api.approval_detail(db="runtime-smoke", workflow_id=launched_workflow_id, key="approve_plugin_test"))
    assert detail["approval"]["key"] == "approve_plugin_test"
    assert detail["decision_semantics"]["resume"] is True
    assert "inspectable continuation" in detail["decision_semantics"]["description"]
    assert detail["what_you_are_approving"]["action"] == "approve_plugin_test"
    assert detail["timeline"][0]["type"] == "WorkflowStarted"
    assert detail["timeline"][-1]["type"] == "ApprovalRequested"
    assert str(db) not in json.dumps(detail)

    source = run(api.workflow_definition_source("plugin-approval", db="runtime-smoke"))
    assert source["definition"]["id"] == "plugin-approval"
    assert source["workflow_ref"] == "tests.test_hermes_plugin_approvals:plugin_approval_workflow"
    assert source["language"] == "python"
    assert source["highlight_class"] == "language-python"
    assert "def plugin_approval_workflow" in source["code"]
    assert "approve(" in source["code"]
    assert source["location"]["module"] == "tests.test_hermes_plugin_approvals"
    assert str(db) not in json.dumps(source)


def test_dashboard_workflow_definition_source_loads_path_refs(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db)
    workflow_file = tmp_path / "demo_flow.py"
    workflow_file.write_text(
        "from hermes_workflows import wait_for, workflow\n"
        "\n"
        "@workflow\n"
        "async def demo_path_workflow(inputs):\n"
        "    return {'ok': True}\n"
    )
    configure_test_dbs(
        monkeypatch,
        tmp_path,
        {"runtime-smoke": str(db)},
        workflow_catalog=[
            {
                "id": "path-ref-demo",
                "name": "Path ref demo",
                "workflow_ref": f"{workflow_file}:demo_path_workflow",
            }
        ],
    )
    api = load_dashboard_api()

    source = run(api.workflow_definition_source("path-ref-demo", db="runtime-smoke"))

    assert source["workflow_ref"] == f"{workflow_file}:demo_path_workflow"
    assert source["location"]["attribute"] == "demo_path_workflow"
    assert "async def demo_path_workflow" in source["code"]


def test_dashboard_workflow_catalog_accepts_json_string_from_profile_config(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db)
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    catalog = [
        {
            "id": "json-string-smoke",
            "name": "JSON string smoke",
            "workflow_ref": "tests.test_hermes_plugin_approvals:plugin_approval_workflow",
            "tags": ["smoke"],
        }
    ]
    fake_config = {
        "plugins": {
            "entries": {
                "hermes-workflows-approvals": {
                    "workflow_catalog": json.dumps(catalog),
                }
            }
        }
    }

    hermes_cli = ModuleType("hermes_cli")
    hermes_config = ModuleType("hermes_cli.config")
    setattr(hermes_config, "load_config", lambda: fake_config)

    def cfg_get(config, *keys, default=None):
        value = config
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    setattr(hermes_config, "cfg_get", cfg_get)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)
    api = load_dashboard_api()

    definitions = run(api.workflow_definitions(db="runtime-smoke"))
    assert definitions["count"] == 1
    assert definitions["definitions"][0]["id"] == "json-string-smoke"
    assert definitions["definitions"][0]["runnable"] is True


def test_dashboard_run_launch_rejects_browser_supplied_workflow_id(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(
        monkeypatch,
        tmp_path,
        {"runtime-smoke": str(db)},
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
        run(api.run_workflow({"db": "runtime-smoke", "definition_id": "plugin-approval", "workflow_id": "wf_plugin"}))

    assert getattr(excinfo.value, "status_code", None) == 400
    assert "workflow_id" in str(getattr(excinfo.value, "detail", excinfo.value))


def test_dashboard_inferred_history_definitions_are_not_browser_runnable(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    create_pending_approval(db)
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    definitions = run(api.workflow_definitions(db="runtime-smoke"))
    inferred = definitions["definitions"][0]
    assert inferred["tags"] == ["inferred"]
    assert inferred["runnable"] is False

    with pytest.raises(Exception) as excinfo:
        run(api.run_workflow({"db": "runtime-smoke", "definition_id": inferred["id"]}))

    assert getattr(excinfo.value, "status_code", None) == 403
    assert "workflow_catalog" in str(getattr(excinfo.value, "detail", excinfo.value))

def test_dashboard_runtime_state_packets_are_sanitized_and_labeled(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_ask_workflow,
        {},
        workflow_id="wf_dashboard_runtime_state",
        workflow_ref="tests.test_dashboard_plugin:dashboard_ask_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    waiting = run(api.run_status("wf_dashboard_runtime_state", db="runtime-smoke"))
    waiting_state = waiting["run"]["runtime_state"]
    assert waiting_state["primary"] == "waiting_on_human"
    assert waiting_state["label"] == "Waiting on Skylar"
    assert waiting_state["source"] == {"alias": "runtime-smoke"}
    assert str(db) not in json.dumps(waiting)

    receipt = run(
        api.respond_review_request(
            {
                "db": "runtime-smoke",
                "workflow_id": "wf_dashboard_runtime_state",
                "key": "review_dashboard_payload",
                "payload": {"answer": "ship it"},
            }
        )
    )
    queued_state = receipt["post_resume"]["runtime_state"]
    assert queued_state["primary"] == "queued"
    assert queued_state["label"] == "Queued — no worker has claimed this yet"

    review_queue = run(api.active_review_requests(db="runtime-smoke"))
    review_card = next(item for item in review_queue["review_requests"] if item["workflow_id"] == "wf_dashboard_runtime_state")
    assert review_card["status"] == "completed"
    assert review_card["runtime_state"]["label"] == "Queued — no worker has claimed this yet"
    assert str(db) not in json.dumps(review_queue)


def test_dashboard_runtime_state_hides_worker_paths_and_warns_on_multiple_workers(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.run_until_idle(
        dashboard_ask_workflow,
        {},
        workflow_id="wf_dashboard_runtime_worker",
        workflow_ref="tests.test_dashboard_plugin:dashboard_ask_workflow",
    )
    engine.submit_operator_response(
        workflow_id="wf_dashboard_runtime_worker",
        key="review_dashboard_payload",
        payload={"answer": "continue"},
        source={"channel": "test", "message_id": "test-runtime-worker"},
        resume=True,
    )
    secret_cwd = tmp_path / "private-workspace"
    secret_cwd.mkdir()
    secret_python = tmp_path / "private-python" / "bin" / "python"
    secret_python.parent.mkdir(parents=True)
    secret_python.write_text("python")
    engine.record_worker_heartbeat(
        worker_id="worker-a",
        worker_instance_id="worker-a-1",
        identity={
            "hostname": "test-host",
            "pid": 4242,
            "cwd": str(secret_cwd),
            "python_executable": str(secret_python),
            "python_version": "3.test",
            "platform": "test-platform",
            "hermes_version": "test-version",
            "agent_runner_enabled": True,
            "metadata": {
                "source_db_name": "runtime-smoke",
                "source_db_path": str(db),
                "allowed_workflow_refs_count": 2,
                "package_fingerprint": {
                    "hermes_workflows": "test-version",
                    "python": "3.test",
                    "executable": str(secret_python),
                },
            },
        },
    )
    engine.record_worker_heartbeat(
        worker_id="worker-b",
        worker_instance_id="worker-b-1",
        identity={"cwd": str(secret_cwd), "python_executable": str(secret_python)},
    )
    claimed = engine.claim_command(
        "wf_dashboard_runtime_worker",
        worker_id="worker-a",
        worker_instance_id="worker-a-1",
        command_type="run_workflow",
        lease_seconds=60,
    )
    assert claimed is not None
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    status = run(api.run_status("wf_dashboard_runtime_worker", db="runtime-smoke"))
    runtime_state = status["run"]["runtime_state"]
    assert runtime_state["primary"] == "running"
    assert runtime_state["label"] == "Running — claimed by worker-a"
    assert runtime_state["worker"]["environment"]["python_executable"] == "python"
    assert runtime_state["worker"]["environment"]["workspace_relation"] == "different_from_dashboard"
    assert runtime_state["worker"]["metadata"] == {
        "source_db_name": "runtime-smoke",
        "allowed_workflow_refs_count": 2,
        "package_fingerprint": {"hermes_workflows": "test-version", "python": "3.test"},
    }
    packet = json.dumps(status)
    assert str(db) not in packet
    assert str(secret_cwd) not in packet
    assert str(secret_python) not in packet
    assert status["worker_warning"]["code"] == "multiple_active_workers"
    assert status["worker_warning"]["active_worker_count"] == 2

    runs = run(api.runs(db="runtime-smoke"))
    row = next(item for item in runs["runs"] if item["workflow_id"] == "wf_dashboard_runtime_worker")
    assert row["runtime_state"]["label"] == "Running — claimed by worker-a"
    assert runs["worker_warning"]["code"] == "multiple_active_workers"
    assert str(secret_cwd) not in json.dumps(runs)


def test_dashboard_artifact_render_descriptors_keep_local_media_paths_visible():
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
                "path": "/Users/operator/private/generated.png",
                "caption": "Generated preview",
            },
        },
        db_alias="runtime-smoke",
    )

    assert card["artifact_render"] == {
        "kind": "image",
        "render": "file-reference",
        "persisted": "workflow_history",
        "servable_by_dashboard": False,
        "media_type": "image/png",
        "reference": {"type": "local_path", "field": "path", "href": "/Users/operator/private/generated.png"},
        "warning": "Local/private files are not served by the dashboard; attach or expose them through an explicit artifact store before rendering media inline.",
    }
    assert card["artifact_preview"]["path"] == "/Users/operator/private/generated.png"
    assert "/Users/operator/private/generated.png" in json.dumps(card)

    assert api._redact_artifact_local_refs("/Users/operator/private.png") == "/Users/operator/private.png"
    assert api._redact_artifact_local_refs({"kind": "image", "uri": "/Users/operator/private.png"})["uri"] == "/Users/operator/private.png"
    assert api._redact_artifact_local_refs({"kind": "file", "href": "../private/report.pdf"})["href"] == "../private/report.pdf"
    assert api._redact_artifact_local_refs({"url": "file:///Users/operator/private.mov"})["url"] == "file:///Users/operator/private.mov"

    audio = api._artifact_descriptor({"kind": "audio", "url": "https://example.invalid/review.mp3", "media_type": "audio/mpeg"})
    assert audio["kind"] == "audio"
    assert audio["render"] == "media-reference"
    assert audio["reference"] == {"type": "url", "href": "https://example.invalid/review.mp3"}


def test_dashboard_artifact_render_descriptors_cover_special_artifact_types():
    api = load_dashboard_api()

    html = api._artifact_descriptor({"kind": "html", "html": "<h1>Hi</h1>"})
    assert html["kind"] == "html"
    assert html["render"] == "inline-html"
    assert html["media_type"] == "text/html"

    image = api._artifact_descriptor({"kind": "image", "url": "https://example.invalid/chart.png", "media_type": "image/png"})
    assert image["kind"] == "image"
    assert image["render"] == "media-reference"
    assert image["reference"] == {"type": "url", "href": "https://example.invalid/chart.png"}

    video = api._artifact_descriptor({"kind": "video", "url": "https://example.invalid/demo.mp4", "media_type": "video/mp4"})
    assert video["kind"] == "video"
    assert video["render"] == "media-reference"

    diff = api._artifact_descriptor({"kind": "diff", "diff": "-old\n+new"})
    assert diff["kind"] == "diff"
    assert diff["render"] == "inline-diff"

    custom = api._artifact_descriptor({"kind": "chart", "renderer": "acme.chart.v1", "data": [1, 2]})
    assert custom["kind"] == "chart"
    assert custom["render"] == "custom-render"
    assert custom["reference"] == {"type": "custom_renderer", "renderer": "acme.chart.v1"}


def test_dashboard_frontend_renders_special_artifact_types_without_serving_private_files():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert "function MarkdownArtifactPreview" in index_js
    assert "function HtmlArtifactPreview" in index_js
    assert "sandbox: \"\"" in index_js
    assert "srcDoc" in index_js
    assert "dangerouslySetInnerHTML" not in index_js
    assert "function MediaArtifactPreview" in index_js
    assert 'e("img"' in index_js
    assert 'e("audio"' in index_js
    assert 'e("video"' in index_js
    assert "function FileReferencePreview" in index_js
    assert 'render.render === "file-reference"' in index_js
    assert 'render.render === "media-reference"' in index_js
    assert "Local/private files are not served by the dashboard" in index_js
    assert ".hwf-html-preview" in style_css
    assert ".hwf-media-image" in style_css
    assert ".hwf-media-audio" in style_css
    assert ".hwf-media-video" in style_css
    assert ".hwf-file-reference" in style_css


def test_dashboard_frontend_exposes_custom_artifact_renderer_fallback_and_diff_preview():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert "custom-render" in index_js
    assert "artifactRenderers" in index_js
    assert "registerArtifactRenderer" in index_js
    assert "CustomArtifactFallback" in index_js
    assert "inline-diff" in index_js
    assert "function DiffPreview" in index_js
    assert "hwf-diff-added" in style_css
    assert "hwf-diff-removed" in style_css
    assert "hwf-diff-hunk" in style_css


def test_dashboard_run_dag_derives_dynamic_fanout_topology_from_run_events(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_dynamic_topology_workflow,
        {"value": "run-derived"},
        workflow_id="wf_dynamic_topology",
        workflow_ref="tests.test_dashboard_plugin:dashboard_dynamic_topology_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    dag = run(api.run_dag("wf_dynamic_topology", db="runtime-smoke"))
    edges = {(edge["from"], edge["to"]) for edge in dag["edges"]}
    nodes = {node["id"]: node for node in dag["nodes"]}

    assert dag["layout"] == "run-derived-topology"
    assert nodes["gather:0"]["kind"] == "gather"
    assert nodes["gather:0"]["status"] == "completed"
    assert nodes["step:dashboard_dynamic_left:0"]["status"] == "completed"
    assert nodes["step:dashboard_dynamic_right:0"]["status"] == "completed"
    assert ("step:dashboard_dynamic_seed:0", "step:dashboard_dynamic_left:0") in edges
    assert ("step:dashboard_dynamic_seed:0", "step:dashboard_dynamic_right:0") in edges
    assert ("step:dashboard_dynamic_left:0", "gather:0") in edges
    assert ("step:dashboard_dynamic_right:0", "gather:0") in edges
    assert ("gather:0", "step:dashboard_dynamic_join:0") in edges
    assert ("step:dashboard_dynamic_left:0", "step:dashboard_dynamic_right:0") not in edges


def test_dashboard_run_dag_preserves_pipeline_item_lanes(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_pipeline_lane_workflow,
        {"sections": ["a", "b"]},
        workflow_id="wf_pipeline_lanes",
        workflow_ref="tests.test_dashboard_plugin:dashboard_pipeline_lane_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    dag = run(api.run_dag("wf_pipeline_lanes", db="runtime-smoke"))
    edges = {(edge["from"], edge["to"]) for edge in dag["edges"]}

    draft_a = "step:dashboard_pipeline_draft:0"
    draft_b = "step:dashboard_pipeline_draft:1"
    humanize_a = "step:dashboard_pipeline_humanize:0"
    humanize_b = "step:dashboard_pipeline_humanize:1"

    assert (draft_a, humanize_a) in edges
    assert (draft_b, humanize_b) in edges
    assert (draft_a, humanize_b) not in edges
    assert (draft_b, humanize_a) not in edges


def test_dashboard_run_dag_groups_returned_workflow_children_as_collapsible_nodes(tmp_path, monkeypatch):
    from tests.test_dynamic_workflow_return import dynamic_processor_pipeline

    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dynamic_processor_pipeline,
        {"items": [{"id": "a", "label": "alpha"}, {"id": "b", "label": "beta"}]},
        workflow_id="wf_dynamic_child_dag",
        workflow_ref="tests.test_dynamic_workflow_return:dynamic_processor_pipeline",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    dag = run(api.run_dag("wf_dynamic_child_dag", db="runtime-smoke"))
    nodes = {node["id"]: node for node in dag["nodes"]}
    child_nodes = [node for node in dag["nodes"] if node["kind"] == "child_workflow"]
    child_internal_step_suffix = ":analyze_generated_item:0"

    assert len(child_nodes) == 2
    assert not any(node_id.endswith(child_internal_step_suffix) for node_id in nodes)
    for child_node in child_nodes:
        assert child_node["collapsible"] is True
        assert child_node["expanded_by_default"] is False
        assert child_node["symbol"] == "process_item"
        assert child_node["label"] == "process_item"
        assert child_node["child_workflow_id"].startswith("wf_dynamic_child_dag.child.")
        assert child_node["child_status"] == "completed"
        assert child_node["child_node_count"] >= 3
        child_dag = child_node["child_dag"]
        child_dag_nodes = {node["id"]: node for node in child_dag["nodes"]}
        child_internal_step = next(node_id for node_id in child_dag_nodes if node_id.endswith(child_internal_step_suffix))
        assert child_dag["workflow_id"] == child_node["child_workflow_id"]
        assert child_dag_nodes[child_internal_step]["kind"] == "step"
        assert child_dag_nodes[child_internal_step]["status"] == "completed"


def test_dashboard_run_dag_does_not_chain_out_of_order_parallel_steps():
    api = load_dashboard_api()
    status = {
        "workflow_id": "wf_parallel_out_of_order",
        "events": [
            {"seq": 1, "type": "WorkflowStarted", "payload": {}},
            {"seq": 2, "type": "StepRequested", "payload": {"key": "step:a", "step_name": "a"}},
            {"seq": 3, "type": "StepRequested", "payload": {"key": "step:b", "step_name": "b"}},
            {"seq": 4, "type": "StepCompleted", "payload": {"key": "step:b", "output": "B"}},
            {"seq": 5, "type": "StepCompleted", "payload": {"key": "step:a", "output": "A"}},
            {"seq": 6, "type": "StepRequested", "payload": {"key": "step:c", "step_name": "c"}},
            {"seq": 7, "type": "StepCompleted", "payload": {"key": "step:c", "output": "C"}},
            {"seq": 8, "type": "WorkflowCompleted", "payload": {"result": "done"}},
        ],
    }

    dag = api._run_dag_payload(status, [])
    edges = {(edge["from"], edge["to"]) for edge in dag["edges"]}

    assert ("workflow:start", "step:a") in edges
    assert ("workflow:start", "step:b") in edges
    assert ("step:a", "step:c") in edges
    assert ("step:b", "step:c") in edges
    assert ("step:c", "workflow:completed") in edges
    assert ("step:a", "step:b") not in edges
    assert ("step:b", "step:a") not in edges


def test_dashboard_run_dag_attaches_step_artifacts(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    artifact = {
        "kind": "file",
        "path": "/tmp/workflows-private/generated.png",
        "summary": "Generated preview",
    }
    WorkflowEngine(db).run_until_idle(
        dashboard_path_artifact_workflow,
        {"artifact": artifact},
        workflow_id="wf_dag",
        workflow_ref="tests.test_dashboard_plugin:dashboard_path_artifact_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    dag = run(api.run_dag("wf_dag", db="runtime-smoke"))

    assert dag["workflow_id"] == "wf_dag"
    assert dag["layout"] == "run-derived-topology"
    assert dag["artifact_count"] >= 1
    step_node = next(node for node in dag["nodes"] if node["id"] == "step:dashboard_path_artifact_step:0")
    assert step_node["kind"] == "step"
    assert step_node["status"] == "completed"
    assert step_node["artifact_count"] == 1
    assert step_node["artifacts"][0]["kind"] == "step_output"
    assert step_node["artifacts"][0]["source"]["key"] == "step:dashboard_path_artifact_step:0"
    assert step_node["artifacts"][0]["artifact_render"]["render"] == "inline-json"
    approval_node = next(node for node in dag["nodes"] if node["id"] == "approve_path_artifact")
    assert approval_node["kind"] == "step"
    assert approval_node["completion_mode"] == "approval"
    assert approval_node["status"] == "waiting"
    assert any(edge["from"] == "workflow:start" and edge["to"] == "step:dashboard_path_artifact_step:0" for edge in dag["edges"])
    assert any(edge["to"] == "approve_path_artifact" for edge in dag["edges"])
    assert str(db) not in json.dumps(dag)


def test_dashboard_run_dag_attaches_generated_workflow_source_to_agent_and_child(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    WorkflowEngine(db).run_until_idle(
        dashboard_generated_workflow_source_pipeline,
        {"value": "syntax-highlight-me"},
        workflow_id="wf_generated_source_viewer",
        workflow_ref="tests.test_dashboard_plugin:dashboard_generated_workflow_source_pipeline",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    dag = run(api.run_dag("wf_generated_source_viewer", db="runtime-smoke"))
    nodes = {node["id"]: node for node in dag["nodes"]}
    workflow_source_artifacts = [artifact for artifact in dag["artifacts"] if artifact["kind"] == "workflow_source"]

    assert len(workflow_source_artifacts) == 2
    assert {artifact["source"]["event"] for artifact in workflow_source_artifacts} == {
        "StepCompleted",
        "ChildWorkflowRequested",
    }
    assert nodes["agent:build_generated_child:0"]["artifact_count"] == 1
    child_node = next(node for node in dag["nodes"] if node["id"].startswith("child:dashboard_generated_child:"))
    assert child_node["artifact_count"] == 1

    artifact = workflow_source_artifacts[0]
    assert artifact["artifact_render"]["kind"] == "workflow_source"
    assert artifact["artifact_render"]["render"] == "python-source"
    assert artifact["artifact_render"]["language"] == "python"
    assert artifact["artifact_render"]["highlight_class"] == "language-python"
    assert artifact["artifact_render"]["source_hash"] == artifact["preview"]["source_sha256"]
    assert artifact["preview"]["symbol"] == "dashboard_generated_child"
    assert artifact["preview"]["workflow_name"].startswith("generated:")
    assert "@workflow" in artifact["preview"]["source"]
    assert "async def dashboard_generated_child" in artifact["preview"]["source"]
    assert "source_sha256" in artifact["preview"]
    assert "provenance" in artifact["preview"]


def test_dashboard_generated_workflow_approval_artifact_renders_as_python_source():
    api = load_dashboard_api()
    source_hash = hashlib.sha256(DASHBOARD_GENERATED_CHILD_SOURCE.encode("utf-8")).hexdigest()
    artifact = {
        "kind": "generated_workflow.approval.v1",
        "workflow_name": f"generated:{source_hash}:dashboard_generated_child",
        "symbol": "dashboard_generated_child",
        "source_sha256": source_hash,
        "source": DASHBOARD_GENERATED_CHILD_SOURCE,
        "runner_provenance": {"runner": "unit-test"},
        "agent_request": {"name": "build_generated_child"},
        "agent_response": {"response_keys": ["source", "symbol"]},
    }

    preview = api._workflow_source_preview(artifact)
    descriptor = api._artifact_descriptor(artifact)

    assert preview["source"] == DASHBOARD_GENERATED_CHILD_SOURCE
    assert preview["source_sha256"] == source_hash
    assert preview["source_hash_verified"] is True
    assert preview["provenance"]["runner_provenance"] == {"runner": "unit-test"}
    assert descriptor["kind"] == "workflow_source"
    assert descriptor["render"] == "python-source"
    assert descriptor["highlight_class"] == "language-python"


def test_dashboard_run_dag_collapses_coding_workflow_approvals_and_handoff_to_steps(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "feature.txt").write_text("before\n")
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Workflow Tests"],
        ["git", "add", "feature.txt"],
        ["git", "commit", "-q", "-m", "initial"],
    ):
        subprocess.run(args, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    workflow_id = "wf_step_oriented_coding_dag"
    inputs = {
        "repo_path": str(repo),
        "goal": "prove step-oriented coding DAG",
        "verification_commands": ["python -c 'print(\"ok\")'"],
        "verification_timeout": 30,
        "commit": False,
        "push": False,
    }
    result = engine.run_until_idle(
        coding_workflow,
        inputs,
        workflow_id=workflow_id,
        workflow_ref="hermes_workflows.workflows.coding:coding_workflow",
    )
    result = engine.drain(workflow_id, initial=result)
    assert result.status == "waiting"

    receipt = engine.submit_approval_decision(
        ApprovalDecisionInput(
            workflow_id=workflow_id,
            key="approve_coding_plan",
            action="approve",
            by="skylar",
            source={"kind": "human", "id": "skylar", "channel": "test", "message_id": "plan-ok"},
        )
    )
    after_plan = engine.drain(workflow_id)
    assert after_plan.status == "waiting"
    (repo / "feature.txt").write_text("after\n")
    signal_result = engine.signal(
        workflow_id,
        "agent.completed",
        key="coding_ready",
        payload={"by": "agent:implementer", "summary": "updated feature.txt"},
        source={"kind": "agent", "id": "agent-1"},
    )
    engine.drain(workflow_id, initial=signal_result)

    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()
    dag = run(api.run_dag(workflow_id, db="runtime-smoke"))
    nodes = {node["id"]: node for node in dag["nodes"]}
    node_ids = set(nodes)

    assert "approve_coding_plan" in node_ids
    assert "coding_ready" in node_ids
    assert "approve_coding_review" in node_ids
    assert nodes["approve_coding_plan"]["kind"] == "step"
    assert nodes["approve_coding_plan"]["completion_mode"] == "approval"
    assert nodes["approve_coding_plan"]["status"] == "completed"
    assert "StepRequested" in nodes["approve_coding_plan"]["event_types"]
    assert "StepCompleted" in nodes["approve_coding_plan"]["event_types"]
    assert nodes["coding_ready"]["kind"] == "step"
    assert nodes["coding_ready"]["completion_mode"] == "agent"
    assert nodes["coding_ready"]["status"] == "completed"
    assert "StepRequested" in nodes["coding_ready"]["event_types"]
    assert "StepCompleted" in nodes["coding_ready"]["event_types"]
    assert nodes["approve_coding_review"]["kind"] == "step"
    assert nodes["approve_coding_review"]["completion_mode"] == "approval"
    assert nodes["approve_coding_review"]["status"] == "waiting"
    assert not any(node_id.startswith(("approval:", "signal:", "handoff:", "wait:")) for node_id in node_ids)


def test_dashboard_path_ref_workflow_source_run_and_dag(tmp_path, monkeypatch):
    project = tmp_path / "path-ref-project"
    workflow_file = project / "path_ref_flow.py"
    db = project / ".hermes" / "workflows.sqlite"
    workflow_file.parent.mkdir(parents=True)
    db.parent.mkdir(parents=True)
    workflow_file.write_text(
        "from hermes_workflows import approve, step, workflow\n"
        "\n"
        "@step\n"
        "async def build_path_ref_packet(value):\n"
        "    return {'kind': 'packet', 'value': value, 'path': '/Users/operator/private/path-ref.txt'}\n"
        "\n"
        "@workflow\n"
        "async def path_ref_dag_workflow(inputs):\n"
        "    packet = await build_path_ref_packet(inputs.get('value', 1))\n"
        "    decision = await approve(\n"
        "        key='approve_path_ref_dag',\n"
        "        prompt='Approve path-ref DAG?',\n"
        "        artifact=packet,\n"
        "        allowed=['approve', 'reject'],\n"
        "    )\n"
        "    return {'decision': decision.get('action'), 'packet': packet}\n"
    )
    workflow_ref = f"{workflow_file}:path_ref_dag_workflow"
    WorkflowEngine(db)  # Create the configured DB before read-only dashboard source lookup.
    configure_test_dbs(
        monkeypatch,
        tmp_path,
        {"runtime-smoke": str(db)},
        workflow_catalog=[
            {
                "id": "path-ref-dag",
                "name": "Path-ref DAG smoke",
                "description": "Dashboard smoke for workflow refs loaded from a .py file path.",
                "workflow_ref": workflow_ref,
                "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}},
            }
        ],
    )
    api = load_dashboard_api()

    source = run(api.workflow_definition_source("path-ref-dag", db="runtime-smoke"))
    launch = run(api.run_workflow({"db": "runtime-smoke", "definition_id": "path-ref-dag", "input": {"value": 2}}))
    workflow_id = launch["result"]["workflow_id"]
    dag = run(api.run_dag(workflow_id, db="runtime-smoke"))

    assert source["workflow_ref"] == workflow_ref
    assert source["location"]["attribute"] == "path_ref_dag_workflow"
    assert source["location"]["file"] == "path_ref_flow.py"
    assert "async def path_ref_dag_workflow" in source["code"]
    assert launch["result"]["status"] == "waiting"
    assert launch["result"]["waiting_on"] == "signal:approval.decision:approve_path_ref_dag"
    assert dag["workflow_id"] == workflow_id
    assert dag["layout"] == "run-derived-topology"
    step_node = next(node for node in dag["nodes"] if node["id"] == "step:build_path_ref_packet:0")
    assert step_node["kind"] == "step"
    assert step_node["status"] == "completed"
    assert step_node["artifact_count"] == 1
    assert step_node["artifacts"][0]["artifact_render"]["render"] == "file-reference"
    approval_node = next(node for node in dag["nodes"] if node["id"] == "approve_path_ref_dag")
    assert approval_node["kind"] == "step"
    assert approval_node["completion_mode"] == "approval"
    assert approval_node["status"] == "waiting"
    assert any(edge["from"] == "workflow:start" and edge["to"] == "step:build_path_ref_packet:0" for edge in dag["edges"])
    assert any(edge["to"] == "approve_path_ref_dag" for edge in dag["edges"])
    combined = json.dumps([source, launch, dag], sort_keys=True)
    assert str(db) not in combined
    assert "/Users/operator/private/path-ref.txt" in combined


def test_dashboard_email_ops_review_artifacts_do_not_expose_future_approval_queue():
    api = load_dashboard_api()
    legacy_packet = {
        "kind": "email_ops_packet",
        "mode": "dry_run",
        "summary": {"total_items": 1, "draft_artifacts": 1, "entity_proposals": 1},
        "items": [{"handle": "gmail:ops:001", "safe_summary": "Needs reply"}],
        "entity_proposals": [{"name": "Acme"}],
        "draft_artifacts": [{"source_handle": "gmail:ops:001"}],
        "follow_up_recommendations": [{"source_handle": "gmail:ops:001"}],
        "archive_candidates": [{"source_handle": "gmail:ops:001"}],
        "approval_queue": [
            {"approval_kind": "email_draft_send", "risk": "external email send"},
            {"approval_kind": "entity_graph_writeback", "risk": "canonical knowledge-base edits"},
        ],
        "side_effect_ledger": {"gmail_sent": 0},
    }

    card = api._approval_card(
        {
            "workflow_id": "wf_email_ops",
            "workflow_ref": "project_workflows.email_ops:email_ops_workflow",
            "key": "email_ops_dry_run_review",
            "prompt": "Review email ops dry-run packet?",
            "artifact": legacy_packet,
        },
        db_alias="Project workflows",
    )

    preview = card["artifact_preview"]
    assert preview["kind"] == "email_ops_dry_run_review"
    assert preview["review_scope"] == "classification_review_only"
    assert preview["decision_requested"].startswith("Approve whether this dry-run classification packet")
    assert "approval_queue" not in preview
    assert "draft_artifacts" not in preview
    assert "follow_up_recommendations" not in preview
    assert "archive_candidates" not in preview
    assert "email_draft_send" not in json.dumps(preview)
    assert "entity_graph_writeback" not in json.dumps(preview)
    assert preview["deferred_action_counts"] == {
        "drafts_requiring_send_review": 1,
        "followups_requiring_separate_approval": 1,
        "archive_candidates_requiring_policy_or_approval": 1,
        "entity_proposals_requiring_separate_writeback_review": 1,
    }


def test_dashboard_email_draft_approval_preview_hides_internal_atomic_flag():
    api = load_dashboard_api()

    card = api._approval_card(
        {
            "workflow_id": "wf_email_draft",
            "workflow_ref": "automation-agent_workflows.email_ops:automation-agent_email_ops_workflow",
            "key": "automation-agent_email_ops:draft_send:gmail:ops:001:abc",
            "prompt": "Approve this one email draft?",
            "artifact": {
                "kind": "email_draft_send_approval",
                "atomic": True,
                "source_handle": "gmail:ops:001",
                "consequence": "external_email_send_after_human_review",
                "source_email": {
                    "from": "Jane Founder <jane@acme.ai>",
                    "subject": "Can you confirm Tuesday?",
                    "links": {"gmail_thread": "https://mail.google.com/mail/u/0/#all/thread-source-456"},
                },
                "draft": {
                    "to": "jane@acme.ai",
                    "subject": "Re: Can you confirm Tuesday?",
                    "body": "Thanks — Tuesday works for me.",
                    "gmail_draft_id": "draft-123",
                    "send_requires_approval": True,
                },
            },
        },
        db_alias="Workflow runtime",
    )

    preview = card["artifact_preview"]
    assert preview["kind"] == "email_draft_send_approval"
    assert "atomic" not in preview
    assert preview["source_email"]["links"]["gmail_thread"].startswith("https://mail.google.com/")
    assert preview["draft"]["gmail_draft_id"] == "draft-123"
    assert "atomic" not in json.dumps(card["artifact_preview"])



def test_dashboard_status_detail_and_overview_keep_local_artifact_paths_visible(tmp_path, monkeypatch):
    db = tmp_path / "workflow.sqlite"
    artifact = {
        "kind": "image",
        "media_type": "image/png",
        "uri": "/Users/operator/private/generated.png",
        "href": "../private/report.pdf",
        "nested": ["/Users/operator/private/nested.wav", {"url": "file:///Users/operator/private/video.mov"}],
    }
    WorkflowEngine(db).run_until_idle(
        dashboard_path_artifact_workflow,
        {"artifact": artifact},
        workflow_id="wf_dashboard_path_artifact",
        workflow_ref="tests.test_dashboard_plugin:dashboard_path_artifact_workflow",
    )
    configure_test_dbs(monkeypatch, tmp_path, {"runtime-smoke": str(db)})
    api = load_dashboard_api()

    responses = [
        run(api.run_status("wf_dashboard_path_artifact", db="runtime-smoke", commands="all", recent_events=100)),
        run(api.approval_detail(db="runtime-smoke", workflow_id="wf_dashboard_path_artifact", key="approve_path_artifact")),
        run(api.overview(db="runtime-smoke", recent_events=100, command_limit=20, command_payload_chars=5000)),
        run(api.active_approvals(db="runtime-smoke")),
    ]
    combined = json.dumps(responses, sort_keys=True)

    for visible in (
        "/Users/operator/private/generated.png",
        "../private/report.pdf",
        "/Users/operator/private/nested.wav",
        "file:///Users/operator/private/video.mov",
        "/Users/operator/private/generated-copy.png",
    ):
        assert visible in combined
    assert "[REDACTED_LOCAL_PATH]" not in combined
    detail = responses[1]
    assert detail["what_you_are_approving"]["artifact"]["uri"] == "/Users/operator/private/generated.png"
    assert detail["approval"]["artifact"]["href"] == "../private/report.pdf"


def test_dashboard_plugin_frontend_exposes_full_workflows_console_navigation():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    for label in ("Overview", "Workflows", "Runs", "Review Queue", "Artifacts"):
        assert label in index_js
    for phrase in (
        "Run workflow",
        "Needs review",
        "Human input",
        "Submit input",
        "What you are approving",
        "Record and resume",
        "View approval",
        "Run history",
        "Source",
        "Workflow state source",
        "Human input requests record typed outputs; approval gates are approve/reject review requests.",
        "artifact: ",
        "ArtifactInlinePreview",
        "artifactInlineValue",
        'value.__hermes_type__ === "Artifact"',
        "hwf-markdown-preview",
    ):
        assert phrase in index_js
    for confusing_phrase in (
        "Operator Steps",
        "Needs operator input",
        "Submit response",
        "JSON response payload",
        "human/operator steps",
    ):
        assert confusing_phrase not in index_js
    assert "active_source" in index_js
    assert "firstDb" not in index_js
    assert "selected DB alias" not in index_js
    assert ".hwf-shell" in style_css
    assert "hwf-approval-dialog" in index_js
    assert "showModal" in index_js
    assert "returnValue" in index_js
    assert "hwf-approval-detail-body" in index_js
    assert ".hwf-approval-dialog::backdrop" in style_css
    assert "width: min(72rem, calc(100vw - 2rem))" in style_css
    assert "max-height: calc(100vh - 2rem)" in style_css
    assert "overflow: auto" in style_css
    assert ".hwf-close-button" in style_css


def test_dashboard_frontend_unifies_human_work_into_review_queue_and_guides_input():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert 'const reviewRequests = reviewRequestsData.data && reviewRequestsData.data.review_requests || overviewData.active_review_requests || [];' in index_js
    assert 'activeTab === "Review Queue"' in index_js
    assert 'tabs: ["Overview", "Workflows", "Runs", "Review Queue", "Artifacts"]' in index_js
    assert 'API + "/review-requests"' in index_js
    assert 'function HumanInputCard' in index_js
    assert 'input_surface' in index_js
    assert 'surface.kind === "review_decision"' in index_js
    assert 'normalizeAction' in index_js
    assert 'feedbackText && String(action || "").toLowerCase().replace(/-/g, "_") === "approve"' in index_js
    assert 'submitReviewDecision(action.value, actions)' in index_js
    assert 'formatActionLabel(action)' in index_js
    assert 'Request edits' not in index_js
    assert 'submitReviewDecision("reject")' not in index_js
    assert ".hwf-review-action-row button" in style_css
    assert "letter-spacing: normal" in style_css
    assert 'Upload support is not wired yet' in index_js
    assert 'OperatorStepCard' not in index_js
    assert 'operatorStepsData' not in index_js
    assert 'Paste JSON matching the requested schema' not in index_js


def test_dashboard_frontend_hides_successful_initial_loading_state():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()

    assert "const initialConsoleLoading" in index_js
    assert "Loading workflow console…" not in index_js
    assert "Refreshing workflow console…" in index_js
    assert "!hasConsoleData" in index_js


def test_dashboard_frontend_run_rows_do_not_overlap_long_ids():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()
    style_css = (PLUGIN_DASHBOARD / "dist" / "style.css").read_text()

    assert "hwf-run-main" in index_js
    assert "hwf-run-id" in index_js
    assert "hwf-run-signals" in index_js
    assert "hwf-waiting-on" in index_js
    assert "hwf-run-tail" in index_js
    assert "grid-template-columns: minmax(12rem, 1.1fr) minmax(10rem, 0.7fr) minmax(0, 1.4fr) max-content" in style_css
    run_id_block = style_css.split(".hwf-run-id {", 1)[1].split("}", 1)[0]
    assert "overflow-wrap" not in run_id_block
    assert "word-break" not in run_id_block
    assert "white-space: nowrap" in style_css
    assert "text-overflow: ellipsis" in style_css
    assert "min-width: 0" in style_css


def test_dashboard_frontend_hierarchy_and_artifacts_are_not_json_firehose():
    index_js = (PLUGIN_DASHBOARD / "dist" / "index.js").read_text()

    assert "Workflow → Run → Step → Artifact" in index_js
    assert "Top-level queue for active approvals; artifacts live under their run." in index_js
    assert "ArtifactCard" in index_js
    assert "ArtifactSummary" in index_js
    assert "Raw JSON" in index_js
    assert "pretty(artifact.preview)" not in index_js
    assert "approval_queue" in index_js

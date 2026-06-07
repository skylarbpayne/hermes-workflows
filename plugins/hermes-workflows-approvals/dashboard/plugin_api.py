from __future__ import annotations

import asyncio
import importlib
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_workflows import ApprovalDecisionInput, WorkflowEngine
from hermes_workflows.hermes_plugin_approvals import (
    _configured_dbs,
    _next_step_for_receipt,
    _redact,
    _receipt_to_payload,
    approval_view_to_dict,
)

try:  # FastAPI is provided by Hermes Agent's dashboard process.
    import fastapi as _fastapi
except Exception:  # pragma: no cover - keeps direct unit imports dependency-light.
    _fastapi = None


class _FallbackHTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _FallbackAPIRouter:  # minimal decorator shim for tests without FastAPI.
    def get(self, *_args: Any, **_kwargs: Any):
        return lambda fn: fn

    def post(self, *_args: Any, **_kwargs: Any):
        return lambda fn: fn


HTTPException = _fastapi.HTTPException if _fastapi is not None else _FallbackHTTPException
APIRouter = _fastapi.APIRouter if _fastapi is not None else _FallbackAPIRouter

router = APIRouter()


def _int(value: Any, *, default: int, minimum: int = 1, maximum: int = 100) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return cleaned or "workflow"


def _load_workflow(ref: str) -> Any:
    if ":" not in ref:
        raise ValueError("workflow_ref must look like module:function")
    module_name, attr = ref.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _strip_internal_fields(value: Any) -> Any:
    """Remove local-only implementation details before returning browser JSON."""
    if isinstance(value, dict):
        return {key: _strip_internal_fields(item) for key, item in value.items() if key != "db_path"}
    if isinstance(value, list):
        return [_strip_internal_fields(item) for item in value]
    return value


_ARTIFACT_PATH_KEYS = {"path", "file_path", "local_path", "absolute_path", "filesystem_path"}
_MEDIA_KINDS = {"image", "audio", "video"}


def _looks_like_local_path(value: str) -> bool:
    cleaned = value.strip()
    return cleaned.startswith(("/", "./", "../", "~")) or re.match(r"^[A-Za-z]:[\\/]", cleaned) is not None


def _redact_artifact_local_refs(value: Any) -> Any:
    """Redact local filesystem references from browser artifact previews.

    The dashboard API may render inside Hermes Agent, but it does not serve or
    host arbitrary local media files. A typed `artifact_render` descriptor tells
    the UI what kind of artifact it is without leaking private paths.
    """

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str.lower() in _ARTIFACT_PATH_KEYS and isinstance(item, str) and _looks_like_local_path(item):
                out[key_str] = "[REDACTED_LOCAL_PATH]"
            else:
                out[key_str] = _redact_artifact_local_refs(item)
        return out
    if isinstance(value, list):
        return [_redact_artifact_local_refs(item) for item in value]
    return value


def _artifact_kind_from_media_type(media_type: str | None) -> str | None:
    if not media_type:
        return None
    major = media_type.split("/", 1)[0].lower()
    if major in _MEDIA_KINDS:
        return major
    if media_type in {"text/markdown", "application/markdown"}:
        return "markdown"
    if media_type.startswith("text/"):
        return "text"
    if media_type == "application/json":
        return "json"
    return None


def _artifact_descriptor(artifact: Any) -> dict[str, Any]:
    """Return the low-risk rendering seam for approval/run artifacts.

    This is descriptive only: it does not fetch, transcode, or serve media. The
    raw artifact remains persisted in workflow history; the dashboard receives a
    redacted preview plus this descriptor so future renderers can support text,
    JSON, image, audio, video, links, and local/private file references safely.
    """

    descriptor: dict[str, Any] = {
        "kind": "json",
        "render": "inline-json",
        "persisted": "workflow_history",
        "servable_by_dashboard": False,
    }
    if artifact is None:
        return {**descriptor, "kind": "none", "render": "none"}
    if isinstance(artifact, str):
        if artifact.startswith(("http://", "https://")):
            return {**descriptor, "kind": "link", "render": "external-link", "reference": {"type": "url", "href": artifact}}
        return {**descriptor, "kind": "text", "render": "inline-text"}
    if isinstance(artifact, dict):
        media_type = artifact.get("media_type") or artifact.get("mime_type") or artifact.get("content_type")
        media_type = str(media_type) if media_type else None
        explicit_kind = str(artifact.get("kind") or artifact.get("type") or "").lower()
        kind = _artifact_kind_from_media_type(media_type) or (explicit_kind if explicit_kind in {"text", "json", "markdown", "image", "audio", "video", "file", "link"} else "json")
        ref = None
        ref_key = None
        for key in ("url", "uri", "href", "path", "file_path", "local_path"):
            raw = artifact.get(key)
            if isinstance(raw, str) and raw.strip():
                ref = raw.strip()
                ref_key = key
                break
        if ref and ref.startswith(("http://", "https://")):
            render = "external-link" if kind == "link" else "media-reference" if kind in _MEDIA_KINDS else "external-reference"
            return {**descriptor, "kind": kind, "render": render, "media_type": media_type, "reference": {"type": "url", "href": ref}}
        if ref:
            guessed_type = media_type or mimetypes.guess_type(ref)[0]
            guessed_kind = _artifact_kind_from_media_type(guessed_type) or kind
            return {
                **descriptor,
                "kind": guessed_kind,
                "render": "file-reference",
                "media_type": guessed_type,
                "reference": {"type": "local_path", "field": ref_key, "href": "[REDACTED_LOCAL_PATH]"},
                "warning": "Local/private files are not served by the dashboard; attach or expose them through an explicit artifact store before rendering media inline.",
            }
        if kind == "markdown" or "markdown" in artifact:
            return {**descriptor, "kind": "markdown", "render": "inline-markdown", "media_type": media_type}
        if kind == "text" or "text" in artifact:
            return {**descriptor, "kind": "text", "render": "inline-text", "media_type": media_type}
        return {**descriptor, "kind": kind, "render": "inline-json", "media_type": media_type}
    return descriptor


def _runtime_semantics() -> dict[str, Any]:
    return {
        "execution_environment": "Workflow code is imported and executed in the Python process that owns the WorkflowEngine for the selected DB alias. The dashboard API route runs that engine locally; record-only approval decisions do not resume workflow code.",
        "db_selector": "The dropdown selects a configured workflow DB alias, not a remote registry or deployment environment. Raw SQLite paths are intentionally hidden from browser responses.",
        "agent_steps": "AgentStep calls run through the engine's configured agent_runner when present, otherwise deterministic mock/rendered output is used. Runner requests and live responses are persisted as step metadata for replay.",
        "approval_decisions": "Dashboard approve/reject records human provenance only (resume=false); a trusted local resumer must continue the workflow.",
        "artifacts": "Approval and run artifacts are persisted in workflow history and returned as redacted previews plus artifact_render descriptors. The dashboard does not host local media files.",
    }


def _status_packet(
    engine: WorkflowEngine,
    workflow_id: str,
    *,
    recent_events: int,
    commands: str = "recent",
    command_limit: int,
    command_payload_chars: int,
) -> dict[str, Any]:
    packet = _redact(
        engine.workflow_status(
            workflow_id,
            recent_events=recent_events,
            command_history=commands,
            command_limit=command_limit,
            command_payload_chars=command_payload_chars,
        )
    )
    packet["recent_events"] = packet.get("events", [])
    packet["run_id"] = packet.get("workflow_id")
    return _strip_internal_fields(packet)


def _dashboard_approver_id() -> str | None:
    """Return the server-configured human identity dashboard decisions may use."""
    env_value = os.getenv("HERMES_WORKFLOWS_DASHBOARD_APPROVER_ID")
    if env_value and env_value.strip():
        return env_value.strip()
    try:
        from hermes_cli.config import cfg_get, load_config  # type: ignore

        config = load_config()
        for key in ("dashboard_approver_id", "approver_id"):
            value = cfg_get(config, "plugins", "entries", "hermes-workflows-approvals", key, default=None)
            if value and str(value).strip():
                return str(value).strip()
    except Exception:
        pass
    return None


def _resolve_dashboard_db(db: Any = None) -> tuple[str, str]:
    """Resolve dashboard requests through configured aliases only.

    The dashboard runs inside the Hermes process, so accepting explicit paths
    over HTTP would turn a UI route into arbitrary local SQLite read/write.
    Operators can add aliases in plugin config instead.
    """
    configured = _configured_dbs()
    raw = str(db or "").strip()
    if not raw:
        if len(configured) == 1:
            alias, path = next(iter(configured.items()))
            return alias, path
        if "default" in configured:
            return "default", configured["default"]
        raise HTTPException(status_code=400, detail="Select a configured DB alias.")
    if raw not in configured:
        raise HTTPException(status_code=400, detail="Dashboard API only accepts configured DB aliases.")
    return raw, configured[raw]


def _raw_catalog_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    env_catalog = os.getenv("HERMES_WORKFLOWS_CATALOG")
    if env_catalog:
        try:
            parsed = json.loads(env_catalog)
            if isinstance(parsed, list):
                entries.extend(item for item in parsed if isinstance(item, dict))
            elif isinstance(parsed, dict):
                raw_items = parsed.get("workflows", parsed)
                if isinstance(raw_items, list):
                    entries.extend(item for item in raw_items if isinstance(item, dict))
                elif isinstance(raw_items, dict):
                    for key, value in raw_items.items():
                        if isinstance(value, dict):
                            entries.append({"id": str(key), **value})
        except Exception:
            pass
    try:
        from hermes_cli.config import cfg_get, load_config  # type: ignore

        config = load_config()
        configured = cfg_get(config, "plugins", "entries", "hermes-workflows-approvals", "workflow_catalog", default=[])
        if isinstance(configured, list):
            entries.extend(item for item in configured if isinstance(item, dict))
        elif isinstance(configured, dict):
            raw_items = configured.get("workflows", configured)
            if isinstance(raw_items, list):
                entries.extend(item for item in raw_items if isinstance(item, dict))
            elif isinstance(raw_items, dict):
                for key, value in raw_items.items():
                    if isinstance(value, dict):
                        entries.append({"id": str(key), **value})
    except Exception:
        pass
    return entries


def _workflow_catalog() -> list[dict[str, Any]]:
    seen: set[str] = set()
    catalog: list[dict[str, Any]] = []
    for entry in _raw_catalog_entries():
        ref = str(entry.get("workflow_ref") or entry.get("ref") or "").strip()
        if not ref:
            continue
        name = str(entry.get("name") or ref.rsplit(":", 1)[-1].replace("_", " ")).strip()
        definition_id = str(entry.get("id") or _slug(name or ref)).strip()
        if definition_id in seen:
            continue
        seen.add(definition_id)
        input_schema = entry.get("input_schema") or entry.get("schema") or {"type": "object", "properties": {}}
        defaults = entry.get("input_defaults") or entry.get("defaults") or {}
        tags = entry.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        catalog.append(
            {
                "id": definition_id,
                "name": name,
                "description": str(entry.get("description") or ""),
                "workflow_ref": ref,
                "input_schema": input_schema,
                "input_defaults": defaults,
                "tags": tags,
                "runnable": True,
                "run_button_label": str(entry.get("run_button_label") or "Run workflow"),
            }
        )
    return catalog


def _definition_by_id(definition_id: str, engine: WorkflowEngine | None = None) -> dict[str, Any]:
    catalog = _catalog_for_engine(engine) if engine is not None else _workflow_catalog()
    for definition in catalog:
        if definition["id"] == definition_id:
            return definition
    raise HTTPException(status_code=404, detail=f"Unknown workflow definition: {definition_id}")


def _catalog_for_engine(engine: WorkflowEngine | None) -> list[dict[str, Any]]:
    """Return configured runnable workflows plus inferred historical refs.

    Configured catalog entries carry richer schema/description. Inferred entries
    keep the UX useful on a live DB that already has runs but has not yet had a
    formal catalog configured.
    """
    catalog = list(_workflow_catalog())
    if engine is None:
        return catalog
    seen_refs = {str(item.get("workflow_ref")) for item in catalog}
    seen_ids = {str(item.get("id")) for item in catalog}
    for run in _all_runs(engine, limit=500):
        ref = str(run.get("workflow_ref") or "").strip()
        if not ref or ref in seen_refs:
            continue
        base_id = _slug(ref.rsplit(":", 1)[-1])
        definition_id = base_id
        suffix = 2
        while definition_id in seen_ids:
            definition_id = f"{base_id}-{suffix}"
            suffix += 1
        seen_refs.add(ref)
        seen_ids.add(definition_id)
        catalog.append(
            {
                "id": definition_id,
                "name": ref.rsplit(":", 1)[-1].replace("_", " ").title(),
                "description": "Inferred from existing run history. Add workflow_catalog config to make this runnable.",
                "workflow_ref": ref,
                "input_schema": {"type": "object", "properties": {}},
                "input_defaults": {},
                "tags": ["inferred"],
                "runnable": False,
                "run_button_label": "View history",
            }
        )
    return catalog


def _all_runs(engine: WorkflowEngine, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return engine.list_workflows(status=status)[: _int(limit, default=100, maximum=500)]


def _runs_for_ref(engine: WorkflowEngine, workflow_ref: str, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    return [row for row in _all_runs(engine, status=status, limit=limit) if row.get("workflow_ref") == workflow_ref]


def _run_counts(runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for run in runs:
        status = str(run.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {"total": len(runs), "by_status": by_status}


def _definition_payload(definition: dict[str, Any], engine: WorkflowEngine) -> dict[str, Any]:
    runs = _runs_for_ref(engine, str(definition["workflow_ref"]), limit=500)
    latest = runs[0] if runs else None
    return {**definition, "runs": _run_counts(runs), "latest_run": latest}


def _risk_for_approval(approval: dict[str, Any]) -> dict[str, str]:
    authority = approval.get("authority")
    artifact = approval.get("artifact")
    text = json.dumps({"authority": authority, "artifact": artifact}, default=str).lower()
    if any(word in text for word in ("payment", "purchase", "delete", "publish", "send_email", "external_send", "credential")):
        return {"level": "high", "reason": "The approval appears to authorize an external, destructive, financial, or credential-affecting action."}
    if any(word in text for word in ("email", "calendar", "schedule", "deploy", "post", "message")):
        return {"level": "medium", "reason": "The approval may affect people, publishing, scheduling, or deployment state."}
    return {"level": "low", "reason": "Record-only dashboard decision; no workflow resume or external side effect happens in this route."}


def _approval_card(approval: dict[str, Any], *, db_alias: str) -> dict[str, Any]:
    prompt = approval.get("prompt") or approval.get("key") or "Approval needed"
    return {
        "db_alias": db_alias,
        "workflow_id": approval.get("workflow_id"),
        "workflow_name": approval.get("workflow_name"),
        "workflow_ref": approval.get("workflow_ref"),
        "key": approval.get("key"),
        "status": approval.get("status"),
        "headline": prompt,
        "prompt": prompt,
        "approver": approval.get("approver"),
        "allowed": approval.get("allowed") or ["approve", "reject"],
        "authority": approval.get("authority"),
        "artifact_preview": _redact_artifact_local_refs(approval.get("artifact")),
        "artifact_render": _artifact_descriptor(approval.get("artifact")),
        "decision": approval.get("decision"),
        "source": approval.get("source"),
        "diagnostics": approval.get("diagnostics") or [],
        "waiting_on": approval.get("waiting_on"),
        "requested_seq": approval.get("requested_seq"),
        "risk": _risk_for_approval(approval),
        "consequence": "Records approve/reject only; a trusted local resumer must continue the workflow.",
        "detail_url": f"/approvals/detail?db={db_alias}&workflow_id={approval.get('workflow_id')}&key={approval.get('key')}",
    }


def _artifacts_from_status(status: dict[str, Any]) -> list[dict[str, Any]]:
    workflow_id = str(status.get("workflow_id") or "")
    artifacts: list[dict[str, Any]] = []
    for approval in status.get("approvals") or []:
        artifact = approval.get("artifact")
        if artifact is not None:
            artifacts.append(
                {
                    "id": f"{workflow_id}:approval:{approval.get('key')}",
                    "workflow_id": workflow_id,
                    "kind": "approval_artifact",
                    "title": approval.get("prompt") or approval.get("key") or "Approval artifact",
                    "source": {"event": "ApprovalRequested", "key": approval.get("key"), "seq": approval.get("requested_seq")},
                    "preview": _redact_artifact_local_refs(artifact),
                    "artifact_render": _artifact_descriptor(artifact),
                }
            )
    result = status.get("result")
    if result is not None:
        artifacts.append(
            {
                "id": f"{workflow_id}:result",
                "workflow_id": workflow_id,
                "kind": "run_result",
                "title": "Run result",
                "source": {"event": "WorkflowCompleted"},
                "preview": _redact_artifact_local_refs(result),
                "artifact_render": _artifact_descriptor(result),
            }
        )
    for event in status.get("events") or status.get("recent_events") or []:
        if event.get("type") != "StepCompleted":
            continue
        payload = event.get("payload") or {}
        if "output" in payload:
            artifacts.append(
                {
                    "id": f"{workflow_id}:step:{event.get('key')}",
                    "workflow_id": workflow_id,
                    "kind": "step_output",
                    "title": f"Step output: {event.get('key')}",
                    "source": {"event": "StepCompleted", "key": event.get("key"), "seq": event.get("seq")},
                    "preview": _redact_artifact_local_refs(payload.get("output")),
                    "artifact_render": _artifact_descriptor(payload.get("output")),
                    "metadata": payload.get("metadata"),
                }
            )
    return artifacts


def _input_from_body(body: dict[str, Any]) -> Any:
    if "input" in body:
        return body["input"]
    if "inputs" in body:
        return body["inputs"]
    if "input_json" in body:
        raw = body["input_json"]
        if isinstance(raw, str):
            return json.loads(raw)
        return raw
    return {}


def _new_workflow_id(definition_id: str) -> str:
    return f"wf_{_slug(definition_id).replace('-', '_')}_{int(time.time())}_{uuid.uuid4().hex[:8]}"


@router.get("/dbs")
async def list_dbs() -> dict[str, Any]:
    dbs = []
    for name, path in sorted(_configured_dbs().items()):
        dbs.append({"name": name, "exists": Path(path).expanduser().exists()})
    return {"count": len(dbs), "dbs": dbs, "runtime_semantics": _runtime_semantics()}


@router.get("/definitions")
async def workflow_definitions(db: str | None = None) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    definitions = [_definition_payload(definition, engine) for definition in _catalog_for_engine(engine)]
    return {"db_alias": db_alias, "count": len(definitions), "definitions": definitions, "runtime_semantics": _runtime_semantics()}


@router.get("/definitions/{definition_id}/runs")
async def definition_runs(
    definition_id: str,
    db: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    definition = _definition_by_id(definition_id, engine)
    runs = _runs_for_ref(engine, str(definition["workflow_ref"]), status=status, limit=_int(limit, default=50, maximum=500))
    return {"db_alias": db_alias, "definition": definition, "count": len(runs), "runs": runs, "runtime_semantics": _runtime_semantics()}


@router.post("/runs")
async def run_workflow(body: dict[str, Any]) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(body.get("db"))
    read_engine = WorkflowEngine(db_path, read_only=True)
    definition = _definition_by_id(str(body.get("definition_id") or ""), read_engine)
    if "workflow_id" in body:
        raise HTTPException(status_code=400, detail="workflow_id is server-generated for dashboard launches")
    if not definition.get("runnable"):
        raise HTTPException(status_code=403, detail="Only workflows explicitly configured in workflow_catalog are browser-runnable")
    workflow_id = _new_workflow_id(str(definition["id"]))
    workflow_ref = str(definition["workflow_ref"])
    inputs = _input_from_body(body)
    def execute_run() -> tuple[Any, dict[str, Any]]:
        workflow = _load_workflow(workflow_ref)
        result = WorkflowEngine(db_path).run_until_idle(workflow, inputs, workflow_id=workflow_id, workflow_ref=workflow_ref)
        status = _status_packet(
            WorkflowEngine(db_path, read_only=True),
            workflow_id,
            recent_events=20,
            commands="recent",
            command_limit=20,
            command_payload_chars=2000,
        )
        return result, status

    try:
        result, status = await asyncio.to_thread(execute_run)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"workflow run failed: {type(exc).__name__}: {exc}") from exc
    return {
        "success": True,
        "db_alias": db_alias,
        "definition": definition,
        "result": {"workflow_id": result.workflow_id, "status": result.status, "waiting_on": result.waiting_on, "error": result.error},
        "run": status,
        "artifacts": _artifacts_from_status(status),
        "runtime_semantics": _runtime_semantics(),
    }


@router.get("/runs")
async def runs(
    db: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    rows = _all_runs(engine, status=status, limit=_int(limit, default=100, maximum=500))
    return {"db_alias": db_alias, "count": len(rows), "runs": rows, "counts": _run_counts(rows), "runtime_semantics": _runtime_semantics()}


@router.get("/runs/{workflow_id}")
async def run_status(
    workflow_id: str,
    db: str | None = None,
    recent_events: int = 20,
    commands: str = "recent",
    command_limit: int = 20,
    command_payload_chars: int = 1000,
) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    packet = _status_packet(
        engine,
        workflow_id,
        recent_events=_int(recent_events, default=20, maximum=200),
        commands=commands,
        command_limit=_int(command_limit, default=20, maximum=200),
        command_payload_chars=_int(command_payload_chars, default=1000, maximum=20000),
    )
    artifacts = _artifacts_from_status(packet)
    return {"db_alias": db_alias, "run": packet, "artifacts": artifacts, "runtime_semantics": _runtime_semantics()}


@router.get("/runs/{workflow_id}/artifacts")
async def run_artifacts(workflow_id: str, db: str | None = None, recent_events: int = 100) -> dict[str, Any]:
    status = await run_status(workflow_id, db=db, recent_events=recent_events, commands="all", command_limit=100, command_payload_chars=5000)
    artifacts = status["artifacts"]
    return {"db_alias": status["db_alias"], "workflow_id": workflow_id, "count": len(artifacts), "artifacts": artifacts, "runtime_semantics": _runtime_semantics()}


@router.get("/approvals")
async def active_approvals(db: str | None = None, status: str | None = "waiting", limit: int = 100) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    approvals = [
        _approval_card(approval_view_to_dict(approval), db_alias=db_alias)
        for approval in engine.list_approvals(status=status)[: _int(limit, default=100, maximum=500)]
    ]
    return {"db_alias": db_alias, "count": len(approvals), "approvals": approvals, "runtime_semantics": _runtime_semantics()}


@router.get("/approvals/detail")
async def approval_detail(db: str | None = None, workflow_id: str = "", key: str = "") -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    if not workflow_id or not key:
        raise HTTPException(status_code=400, detail="workflow_id and key are required")
    engine = WorkflowEngine(db_path, read_only=True)
    approval = _strip_internal_fields(approval_view_to_dict(engine.get_approval(workflow_id, key)))
    status = _status_packet(engine, workflow_id, recent_events=100, commands="recent", command_limit=20, command_payload_chars=5000)
    timeline = [event for event in engine.events(workflow_id) if event.get("seq", 0) <= (approval.get("requested_seq") or 10**9)]
    timeline = _redact(timeline)
    card = _approval_card(approval, db_alias=db_alias)
    return {
        "db_alias": db_alias,
        "workflow": status,
        "approval": approval,
        "approval_card": card,
        "what_you_are_approving": {
            "action": approval.get("key"),
            "prompt": approval.get("prompt"),
            "allowed_decisions": approval.get("allowed") or ["approve", "reject"],
            "artifact": _redact_artifact_local_refs(approval.get("artifact")),
            "artifact_render": _artifact_descriptor(approval.get("artifact")),
            "authority": approval.get("authority"),
            "approver": approval.get("approver"),
        },
        "risk": card["risk"],
        "consequence": card["consequence"],
        "decision_semantics": {
            "resume": False,
            "label": "Record-only decision",
            "description": "The dashboard records approve/reject with server-derived human provenance. It does not resume workflow execution; a trusted local resumer must continue it.",
        },
        "timeline": timeline,
        "artifacts": _artifacts_from_status(status),
        "runtime_semantics": _runtime_semantics(),
    }


@router.get("/overview")
async def overview(
    db: str | None = None,
    status: str | None = None,
    limit: int = 50,
    recent_events: int = 10,
    commands: str = "recent",
    command_limit: int = 10,
    command_payload_chars: int = 1000,
) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    workflow_rows = engine.list_workflows(status=status)[: _int(limit, default=50, maximum=200)]
    workflows = [
        _status_packet(
            engine,
            row["workflow_id"],
            recent_events=_int(recent_events, default=10, maximum=100),
            commands=commands,
            command_limit=_int(command_limit, default=10, maximum=100),
            command_payload_chars=_int(command_payload_chars, default=1000, maximum=10000),
        )
        for row in workflow_rows
    ]
    counts_by_status: dict[str, int] = {}
    artifacts: list[dict[str, Any]] = []
    for item in workflows:
        counts_by_status[str(item.get("status") or "unknown")] = counts_by_status.get(str(item.get("status") or "unknown"), 0) + 1
        artifacts.extend(_artifacts_from_status(item))
    definitions = [_definition_payload(definition, engine) for definition in _catalog_for_engine(engine)]
    approvals = [_approval_card(approval_view_to_dict(approval), db_alias=db_alias) for approval in engine.list_approvals(status="waiting")[:50]]
    return {
        "db_alias": db_alias,
        "workflow_count": len(workflows),
        "counts_by_status": counts_by_status,
        "workflows": workflows,
        "definitions_count": len(definitions),
        "definitions": definitions,
        "active_approval_count": len(approvals),
        "active_approvals": approvals,
        "artifact_count": len(artifacts),
        "artifacts": artifacts[:50],
        "runtime_semantics": _runtime_semantics(),
    }


@router.get("/workflows/{workflow_id}")
async def workflow_status(
    workflow_id: str,
    db: str | None = None,
    recent_events: int = 20,
    commands: str = "recent",
    command_limit: int = 20,
    command_payload_chars: int = 1000,
) -> dict[str, Any]:
    _db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    return _status_packet(
        engine,
        workflow_id,
        recent_events=_int(recent_events, default=20, maximum=200),
        commands=commands,
        command_limit=_int(command_limit, default=20, maximum=200),
        command_payload_chars=_int(command_payload_chars, default=1000, maximum=20000),
    )


@router.post("/approvals/decision")
async def decide_approval(body: dict[str, Any]) -> dict[str, Any]:
    # Dashboard approvals are record-only and derive human provenance from
    # server-side plugin configuration, never from untrusted browser JSON.
    approver_id = _dashboard_approver_id()
    if not approver_id:
        raise HTTPException(
            status_code=403,
            detail="Dashboard approvals require server-configured dashboard_approver_id.",
        )
    db_alias, db_path = _resolve_dashboard_db(body.get("db"))
    action = str(body.get("action") or "approve").strip().lower()
    if action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="action must be approve or reject")
    workflow_id = str(body.get("workflow_id") or "").strip()
    key = str(body.get("key") or "").strip()
    if not workflow_id or not key:
        raise HTTPException(status_code=400, detail="workflow_id and key are required")

    message_id = f"dashboard:{uuid.uuid4()}"
    decision = ApprovalDecisionInput(
        workflow_id=workflow_id,
        key=key,
        action=action,
        by=approver_id,
        source={
            "kind": "human",
            "id": approver_id,
            "channel": "hermes-dashboard",
            "message_id": message_id,
        },
        note=body.get("note"),
        reason=body.get("reason"),
        idempotency_key=message_id,
    )
    try:
        receipt = WorkflowEngine(db_path).submit_approval_decision(decision, resume=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"approval decision failed: {type(exc).__name__}: {exc}") from exc
    receipt_payload = _receipt_to_payload(receipt, resume_requested=False)
    return {
        "success": True,
        "db_alias": db_alias,
        "receipt": receipt_payload,
        "next_step": _next_step_for_receipt(receipt_payload),
    }

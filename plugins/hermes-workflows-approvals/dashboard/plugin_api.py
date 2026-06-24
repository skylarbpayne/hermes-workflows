from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_workflows import ApprovalDecisionInput, Workflow, WorkflowEngine
from hermes_workflows.hermes_plugin_approvals import (
    _configured_dbs,
    _next_step_for_receipt,
    _redact,
    _receipt_to_payload,
    approval_view_to_dict,
)
from hermes_workflows.workflow_loading import load_workflow_ref

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


def _workflow_project_root_for_db(db_path: str | Path) -> Path | None:
    path = Path(db_path).expanduser().resolve()
    if path.parent.name == ".hermes":
        return path.parent.parent
    return None


def _ensure_workflow_project_on_path(db_path: str | Path) -> Path | None:
    """Make project-local workflow modules importable for trusted resume.

    Workflow projects often keep state in <project>/.hermes/workflows.sqlite
    while workflow modules live under <project>. The dashboard process may be
    launched from Hermes or the runtime repo, so raw engine resume can otherwise
    see the DB row but fail to import the stored workflow_ref.
    """

    project_root = _workflow_project_root_for_db(db_path)
    if project_root is None or not project_root.exists():
        return None
    root = str(project_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return project_root


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
    return load_workflow_ref(ref)


def _strip_internal_fields(value: Any) -> Any:
    """Remove local-only implementation details before returning browser JSON."""
    if isinstance(value, dict):
        return {key: _strip_internal_fields(item) for key, item in value.items() if key != "db_path"}
    if isinstance(value, list):
        return [_strip_internal_fields(item) for item in value]
    return value


_ARTIFACT_PATH_KEYS = {"path", "file_path", "local_path", "absolute_path", "filesystem_path"}
_ARTIFACT_REF_KEYS = _ARTIFACT_PATH_KEYS | {"uri", "href", "url"}
_MEDIA_KINDS = {"image", "audio", "video"}


def _looks_like_local_path(value: str) -> bool:
    cleaned = value.strip()
    return cleaned.startswith(("/", "./", "../", "~", "file://")) or re.match(r"^[A-Za-z]:[\\/]", cleaned) is not None


def _redact_artifact_local_refs(value: Any) -> Any:
    """Return artifact values unchanged for local/operator dashboard review."""

    return value


def _operator_approval_artifact(value: Any) -> Any:
    """Shape approval artifacts around the one decision being requested.

    Older workflow dry-run approvals sometimes persist the full workflow packet,
    including an internal `approval_queue` for possible future actions. Rendering
    that field inside an approval card makes it look like the current approval is
    a bundled send/archive/writeback decision. Normalize that legacy packet into
    the same single-review artifact future workflow runs emit.
    """

    if (
        isinstance(value, dict)
        and "approval_queue" in value
        and isinstance(value.get("summary"), dict)
        and isinstance(value.get("items"), list)
    ):
        summary = dict(value.get("summary") or {})
        return {
            "kind": "email_ops_dry_run_review",
            "mode": value.get("mode", "dry_run"),
            "review_scope": "classification_review_only",
            "decision_requested": "Approve whether this dry-run classification packet is useful enough to continue; this does not send, archive, schedule, or write entities.",
            "summary": summary,
            "items": value.get("items", []),
            "entity_proposals": value.get("entity_proposals", []),
            "side_effect_ledger": value.get("side_effect_ledger", {}),
            "deferred_action_counts": {
                "drafts_requiring_send_review": summary.get("draft_artifacts", len(value.get("draft_artifacts", []))),
                "followups_requiring_separate_approval": len(value.get("follow_up_recommendations", [])),
                "archive_candidates_requiring_policy_or_approval": len(value.get("archive_candidates", [])),
                "entity_proposals_requiring_separate_writeback_review": summary.get("entity_proposals", len(value.get("entity_proposals", []))),
            },
            "notes": value.get("notes", []),
        }
    if isinstance(value, dict) and value.get("kind") in {"email_draft_send_approval", "entity_extraction_approval"}:
        return {key: item for key, item in value.items() if key != "atomic"}
    return value


def _workflow_source_preview(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Workflow):
        source = value.source
        symbol = value.symbol
        source_sha256 = value.source_sha256
        provenance = value.provenance
        module_name = value.module_name
        approval_required = value.approval_required
        approval_key = value.approval_key
    elif isinstance(value, dict) and value.get("__hermes_type__") == "Workflow" and isinstance(value.get("source"), str):
        source = value["source"]
        symbol = str(value.get("symbol") or "workflow")
        source_sha256 = str(value.get("source_sha256") or hashlib.sha256(source.encode("utf-8")).hexdigest())
        provenance = value.get("provenance")
        module_name = value.get("module_name")
        approval_required = bool(value.get("approval_required", False))
        approval_key = value.get("approval_key")
    elif isinstance(value, dict) and value.get("kind") == "generated_workflow.approval.v1" and isinstance(value.get("source"), str):
        source = value["source"]
        symbol = str(value.get("symbol") or "workflow")
        source_sha256 = str(value.get("source_sha256") or hashlib.sha256(source.encode("utf-8")).hexdigest())
        provenance = {
            "runner_provenance": value.get("runner_provenance"),
            "agent_request": value.get("agent_request"),
            "agent_response": value.get("agent_response"),
        }
        module_name = None
        approval_required = True
        approval_key = value.get("approval_key")
    else:
        return None
    actual_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return {
        "kind": "generated_workflow_source",
        "language": "python",
        "highlight_class": "language-python",
        "source": source,
        "symbol": symbol,
        "source_sha256": source_sha256,
        "source_hash_verified": actual_sha256 == source_sha256,
        "workflow_name": str(value.get("workflow_name") or f"generated:{source_sha256}:{symbol}") if isinstance(value, dict) else f"generated:{source_sha256}:{symbol}",
        "module_name": module_name,
        "provenance": provenance,
        "approval_required": approval_required,
        "approval_key": approval_key,
    }


def _workflow_source_artifact(
    value: Any,
    *,
    artifact_id: str,
    workflow_id: str,
    title: str,
    source: dict[str, Any],
    metadata: Any = None,
) -> dict[str, Any] | None:
    preview = _workflow_source_preview(value)
    if preview is None:
        return None
    artifact: dict[str, Any] = {
        "id": artifact_id,
        "workflow_id": workflow_id,
        "kind": "workflow_source",
        "title": title,
        "source": source,
        "preview": _redact_artifact_local_refs(preview),
        "artifact_render": _artifact_descriptor(value),
    }
    if metadata is not None:
        artifact["metadata"] = metadata
    return artifact


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
    workflow_source = _workflow_source_preview(artifact)
    if workflow_source is not None:
        return {
            **descriptor,
            "kind": "workflow_source",
            "render": "python-source",
            "language": "python",
            "highlight_class": "language-python",
            "source_hash": workflow_source["source_sha256"],
            "symbol": workflow_source["symbol"],
            "hash_verified": workflow_source["source_hash_verified"],
        }
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
                "reference": {"type": "local_path", "field": ref_key, "href": ref},
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
        "execution_environment": "Workflow code is imported and executed in the Python process that owns the WorkflowEngine for the configured workflow state source. The dashboard API route runs that engine locally; review responses and approval decisions resume trusted local workflow code when requested.",
        "state_source": "The dashboard uses the configured workflow DB alias as its state source. Raw SQLite paths are intentionally hidden from browser responses; the review UI shows the active source instead of making users choose debug databases.",
        "agent_requests": "Worker-capable steps are queued, claimed, executed, and completed with step output/provenance. agent(...) calls run through the engine's configured agent_runner when present; runner requests and live responses are persisted as step metadata for replay.",
        "review_responses": "Human input requests are completed by trusted review surfaces setting typed step output with provenance.",
        "approval_decisions": "Approval gates are approve/reject review requests for risky transitions, not a separate place operators need to hunt for work.",
        "artifacts": "Human input, approval, and run artifacts are persisted in workflow history and returned as review previews plus artifact_render descriptors. The dashboard does not host local media files.",
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
    packet = _redact_artifact_local_refs(_strip_internal_fields(packet))
    packet["recent_events"] = packet.get("events", [])
    packet["run_id"] = packet.get("workflow_id")
    return packet


def _dashboard_decision_by_id() -> str | None:
    """Return the server-configured label dashboard decisions may use."""
    env_value = os.getenv("HERMES_WORKFLOWS_DASHBOARD_DECISION_BY")
    if env_value and env_value.strip():
        return env_value.strip()
    try:
        from hermes_cli.config import cfg_get, load_config  # type: ignore

        config = load_config()
        for key in ("dashboard_decision_by_id", "decision_by"):
            value = cfg_get(config, "plugins", "entries", "hermes-workflows-approvals", key, default=None)
            if value and str(value).strip():
                return str(value).strip()
    except Exception:
        pass
    return None


def _dashboard_decision_actor(configured_actor: str) -> str:
    """Return the server-configured actor label to store as decision provenance."""
    return configured_actor


def _workflow_count_for_dashboard_db(path: str) -> int:
    db_path = Path(path).expanduser()
    if not db_path.exists():
        return 0
    try:
        return len(WorkflowEngine(str(db_path), read_only=True).list_workflows())
    except Exception:
        return 0


def _active_dashboard_db(configured: dict[str, str]) -> tuple[str, str] | None:
    if not configured:
        return None
    if len(configured) == 1:
        return next(iter(configured.items()))

    existing = [(alias, path) for alias, path in configured.items() if Path(path).expanduser().exists()]
    populated = [(alias, path) for alias, path in existing if _workflow_count_for_dashboard_db(path) > 0]
    if len(populated) == 1:
        return populated[0]
    if "default" in configured:
        return "default", configured["default"]
    if len(existing) == 1:
        return existing[0]
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
        active = _active_dashboard_db(configured)
        if active:
            return active
        raise HTTPException(status_code=400, detail="Select a configured DB alias.")
    if raw not in configured:
        raise HTTPException(status_code=400, detail="Dashboard API only accepts configured DB aliases.")
    return raw, configured[raw]


def _append_catalog_entries(entries: list[dict[str, Any]], configured: Any) -> None:
    if isinstance(configured, str):
        try:
            configured = json.loads(configured)
        except Exception:
            return
    if isinstance(configured, list):
        entries.extend(item for item in configured if isinstance(item, dict))
    elif isinstance(configured, dict):
        raw_items = configured.get("workflows", configured)
        if isinstance(raw_items, str):
            try:
                raw_items = json.loads(raw_items)
            except Exception:
                return
        if isinstance(raw_items, list):
            entries.extend(item for item in raw_items if isinstance(item, dict))
        elif isinstance(raw_items, dict):
            for key, value in raw_items.items():
                if isinstance(value, dict):
                    entries.append({"id": str(key), **value})


def _raw_catalog_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    env_catalog = os.getenv("HERMES_WORKFLOWS_CATALOG")
    if env_catalog:
        _append_catalog_entries(entries, env_catalog)
    try:
        from hermes_cli.config import cfg_get, load_config  # type: ignore

        config = load_config()
        configured = cfg_get(config, "plugins", "entries", "hermes-workflows-approvals", "workflow_catalog", default=[])
        _append_catalog_entries(entries, configured)
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


def _relative_source_path(path: str | None) -> str | None:
    if not path:
        return None
    source_path = Path(path).expanduser().resolve()
    for root in (Path.cwd().resolve(),):
        try:
            return str(source_path.relative_to(root))
        except ValueError:
            continue
    return source_path.name


def _workflow_source_payload(definition: dict[str, Any]) -> dict[str, Any]:
    workflow_ref = str(definition["workflow_ref"])
    workflow = _load_workflow(workflow_ref)
    try:
        lines, line_start = inspect.getsourcelines(workflow)
    except (OSError, TypeError) as exc:
        raise HTTPException(status_code=404, detail=f"Workflow source is not inspectable: {workflow_ref}") from exc
    if ":" in workflow_ref:
        module_name, attr = workflow_ref.rsplit(":", 1)
    else:
        module_name = getattr(workflow, "__module__", workflow_ref)
        attr = getattr(workflow, "__name__", None)
    source_file = inspect.getsourcefile(workflow) or inspect.getfile(workflow)
    code = "".join(lines)
    return {
        "definition": definition,
        "workflow_ref": workflow_ref,
        "language": "python",
        "highlight_class": "language-python",
        "code": code,
        "location": {
            "module": module_name,
            "attribute": attr,
            "file": _relative_source_path(source_file),
            "line_start": line_start,
            "line_end": line_start + len(lines) - 1,
        },
        "runtime_semantics": _runtime_semantics(),
    }


def _approval_step_id(key: str) -> str | None:
    if not key:
        return None
    return key.split(":", 1)[1] if key.startswith("approval:") else key


def _agent_request_step_id(key: str) -> str | None:
    if not key:
        return None
    return key.split(":", 1)[1] if key.startswith("agent:") else key


def _signal_step_id(payload: dict[str, Any]) -> str | None:
    signal_type = str(payload.get("signal_type") or "")
    key = str(payload.get("key") or "")
    if signal_type == "approval.decision":
        return _approval_step_id(key)
    if signal_type == "operator.response":
        return _approval_step_id(key)
    if signal_type == "agent.completed":
        return _agent_request_step_id(key)
    return None


def _dag_node_id_for_event(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type") or "")
    payload = event.get("payload") or {}
    key = str(payload.get("key") or event.get("key") or "")
    if event_type == "WorkflowStarted":
        return "workflow:start"
    if event_type in {"StepRequested", "StepCompleted", "StepFailed"}:
        return key or None
    if event_type == "GatherWaiting":
        return key or None
    if event_type in {"ChildWorkflowRequested", "ChildWorkflowCompleted", "ChildWorkflowFailed"}:
        return key or None
    if event_type == "ChildWorkflowGatherWaiting":
        return key or None
    if event_type == "ApprovalRequested":
        return _approval_step_id(key)
    if event_type == "AgentRequested":
        return _agent_request_step_id(key)
    if event_type == "WaitRequested":
        return None
    if event_type == "SignalReceived":
        return _signal_step_id(payload)
    if event_type == "WorkflowCompleted":
        return "workflow:completed"
    if event_type == "WorkflowFailed":
        return "workflow:failed"
    if event_type == "WorkflowCancelled":
        return "workflow:cancelled"
    return None


def _dag_node_kind(event_type: str) -> str:
    if event_type.startswith("Step"):
        return "step"
    if event_type == "ApprovalRequested":
        return "step"
    if event_type == "AgentRequested":
        return "step"
    if event_type in {"SignalReceived", "WaitRequested"}:
        return "step"
    if event_type in {"GatherWaiting", "ChildWorkflowGatherWaiting"}:
        return "gather"
    if event_type.startswith("ChildWorkflow"):
        return "child_workflow"
    return "workflow"


def _dag_node_status(event_type: str, existing: str | None = None) -> str:
    if event_type == "StepRequested":
        return existing or "requested"
    if event_type == "StepCompleted":
        return "completed"
    if event_type == "StepFailed":
        return "failed"
    if event_type == "ApprovalRequested":
        return "waiting"
    if event_type == "AgentRequested":
        return existing or "waiting"
    if event_type == "WaitRequested":
        return existing or "waiting"
    if event_type == "SignalReceived":
        return "completed"
    if event_type in {"GatherWaiting", "ChildWorkflowGatherWaiting"}:
        return existing or "waiting"
    if event_type == "ChildWorkflowRequested":
        return existing or "requested"
    if event_type == "ChildWorkflowCompleted":
        return "completed"
    if event_type == "ChildWorkflowFailed":
        return "failed"
    if event_type == "WorkflowCompleted":
        return "completed"
    if event_type == "WorkflowFailed":
        return "failed"
    if event_type == "WorkflowCancelled":
        return "cancelled"
    if event_type == "WorkflowStarted":
        return "started"
    return existing or "recorded"


def _dag_completion_mode(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type.startswith("Step") and payload.get("completion_mode"):
        return str(payload.get("completion_mode"))
    if event_type == "ApprovalRequested":
        kind = str(payload.get("kind") or "")
        return "operator" if kind in {"human_input.request.v1", "operator.request.v1"} else "approval"
    if event_type == "AgentRequested":
        return "agent"
    if event_type == "SignalReceived":
        signal_type = str(payload.get("signal_type") or "")
        if signal_type == "approval.decision":
            return "approval"
        if signal_type == "operator.response":
            return "operator"
        if signal_type == "agent.completed":
            return "agent"
    if event_type.startswith("Step"):
        return "agent"
    return None


def _dag_node_label(event_type: str, payload: dict[str, Any], event: dict[str, Any]) -> str:
    public_label = _public_dag_label(payload)
    if public_label:
        return public_label
    if event_type == "ApprovalRequested":
        return str(payload.get("prompt") or payload.get("key") or event.get("key") or "Operator step")
    if event_type == "AgentRequested":
        return str(payload.get("key") or event.get("key") or "Agent step")
    if event_type.startswith("ChildWorkflow"):
        return str(payload.get("symbol") or payload.get("workflow_name") or payload.get("child_key") or event.get("key") or "Child workflow")
    if event_type == "SignalReceived":
        return str(payload.get("key") or event.get("key") or "Step output")
    return str(payload.get("step_name") or event.get("key") or event_type)


def _public_dag_label(payload: dict[str, Any]) -> str | None:
    for field in ("public_label", "public_name"):
        value = payload.get(field)
        if value:
            return str(value)
    args = payload.get("args")
    if isinstance(args, list) and args and isinstance(args[0], dict):
        for field in ("public_label", "public_name"):
            value = args[0].get(field)
            if value:
                return str(value)
    request = payload.get("request")
    if isinstance(request, dict):
        for field in ("public_label", "public_name"):
            value = request.get(field)
            if value:
                return str(value)
    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        for field in ("public_label", "public_name"):
            value = artifact.get(field)
            if value:
                return str(value)
    return None


def _artifact_node_id(artifact: dict[str, Any]) -> str | None:
    source = artifact.get("source") or {}
    key = source.get("key")
    if not key:
        if source.get("event") == "WorkflowCompleted":
            return "workflow:completed"
        return None
    if artifact.get("kind") == "approval_artifact":
        return _approval_step_id(str(key))
    return str(key)


def _payload_contains_value(container: Any, value: Any) -> bool:
    """Return true when a request payload concretely carries a prior output.

    Pipeline stages feed each item output into the next stage request. When the
    run history shows that exact value inside a later request payload, the DAG
    can keep that lane's edge narrow instead of connecting every previous
    parallel item to every next-stage item.
    """

    if container == value:
        return True
    if isinstance(container, dict):
        return any(_payload_contains_value(item, value) for item in container.values())
    if isinstance(container, (list, tuple)):
        return any(_payload_contains_value(item, value) for item in container)
    return False


def _child_workflow_nodes_from_events(events: list[dict[str, Any]], child_runs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "ChildWorkflowRequested":
            continue
        payload = event.get("payload") or {}
        node_id = str(payload.get("key") or event.get("key") or "")
        child_workflow_id = payload.get("child_workflow_id")
        if not node_id or not child_workflow_id:
            continue
        child_id = str(child_workflow_id)
        child_dag = child_runs.get(child_id)
        summary: dict[str, Any] = {
            "child_workflow_id": child_id,
            "child_key": payload.get("child_key"),
            "group": payload.get("group"),
            "workflow_name": payload.get("workflow_name") or payload.get("symbol"),
            "symbol": payload.get("symbol"),
            "source_sha256": payload.get("source_sha256"),
            "collapsible": True,
            "expanded_by_default": False,
        }
        if child_dag:
            child_run = child_dag.get("run") or {}
            summary.update(
                {
                    "child_status": child_run.get("status"),
                    "child_waiting_on": child_run.get("waiting_on"),
                    "child_node_count": len(child_dag.get("nodes") or []),
                    "child_edge_count": len(child_dag.get("edges") or []),
                    "child_dag": child_dag,
                }
            )
        summaries[node_id] = summary
    return summaries


def _child_run_dags(
    engine: WorkflowEngine,
    status: dict[str, Any],
    *,
    recent_events: int,
    command_limit: int = 50,
    command_payload_chars: int = 2000,
) -> dict[str, dict[str, Any]]:
    child_ids: list[str] = []
    seen: set[str] = set()
    for event in status.get("events") or status.get("recent_events") or []:
        if event.get("type") != "ChildWorkflowRequested":
            continue
        payload = event.get("payload") or {}
        child_workflow_id = payload.get("child_workflow_id")
        if child_workflow_id and str(child_workflow_id) not in seen:
            child_ids.append(str(child_workflow_id))
            seen.add(str(child_workflow_id))

    child_runs: dict[str, dict[str, Any]] = {}
    for child_id in child_ids:
        try:
            child_status = _status_packet(
                engine,
                child_id,
                recent_events=recent_events,
                commands="all",
                command_limit=command_limit,
                command_payload_chars=command_payload_chars,
            )
        except Exception:
            continue
        child_artifacts = _artifacts_from_status(child_status)
        child_runs[child_id] = _run_dag_payload(child_status, child_artifacts)
    return child_runs


def _run_dag_payload(status: dict[str, Any], artifacts: list[dict[str, Any]], child_runs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str]] = set()
    frontier: set[str] = set()
    pending_requests: list[str] = []
    pending_parents: dict[str, set[str]] = {}
    completed_pending_frontier: set[str] = set()
    gather_children: set[str] = set()
    completed_outputs: dict[str, Any] = {}
    events = status.get("events") or status.get("recent_events") or []
    child_node_summaries = _child_workflow_nodes_from_events(events, child_runs or {})

    def add_edge(source: str, target: str) -> None:
        if not source or not target or source == target:
            return
        key = (source, target)
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append({"from": source, "to": target})

    def add_node(event: dict[str, Any]) -> str | None:
        node_id = _dag_node_id_for_event(event)
        if not node_id:
            return None
        event_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        node = nodes.get(node_id)
        if node is None:
            node = {
                "id": node_id,
                "kind": _dag_node_kind(event_type),
                "label": _dag_node_label(event_type, payload, event),
                "status": _dag_node_status(event_type),
                "first_seq": event.get("seq"),
                "last_seq": event.get("seq"),
                "event_types": [event_type],
                "artifacts": [],
                "artifact_count": 0,
            }
            completion_mode = _dag_completion_mode(event_type, payload)
            if completion_mode:
                node["completion_mode"] = completion_mode
            if node.get("kind") == "child_workflow" and node_id in child_node_summaries:
                node.update(child_node_summaries[node_id])
            nodes[node_id] = node
        else:
            node["status"] = _dag_node_status(event_type, str(node.get("status") or ""))
            node["last_seq"] = event.get("seq")
            if event_type not in node["event_types"]:
                node["event_types"].append(event_type)
            completion_mode = _dag_completion_mode(event_type, payload)
            if completion_mode:
                node["completion_mode"] = completion_mode
            if payload.get("step_name") or event_type in {"ApprovalRequested", "AgentRequested"}:
                node["label"] = _dag_node_label(event_type, payload, event)
            if node.get("kind") == "child_workflow" and node_id in child_node_summaries:
                node.update(child_node_summaries[node_id])
        return node_id

    def flush_sequential_until(node_id: str | None = None) -> None:
        nonlocal completed_pending_frontier, frontier
        if not pending_requests:
            return
        keep: list[str] = []
        for request_id in pending_requests:
            if node_id is not None and request_id != node_id:
                keep.append(request_id)
                continue
            parents = pending_parents.pop(request_id, None) or frontier
            for parent_id in parents:
                add_edge(parent_id, request_id)
            completed_pending_frontier.add(request_id)
            if node_id is not None:
                keep.extend(item for item in pending_requests if item != request_id and item not in keep)
                break
        pending_requests[:] = keep
        if not pending_requests and completed_pending_frontier:
            frontier = set(completed_pending_frontier)
            completed_pending_frontier = set()

    def parents_for_request(request_payload: dict[str, Any]) -> set[str]:
        parents = set(frontier or {"workflow:start"})
        if len(parents) <= 1:
            return parents
        matching_parents = {
            parent_id
            for parent_id in parents
            if parent_id in completed_outputs and _payload_contains_value(request_payload, completed_outputs[parent_id])
        }
        return matching_parents if len(matching_parents) == 1 else parents

    for event in events:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        node_id = add_node(event)
        if not node_id:
            if event_type == "ParallelWaiting":
                flush_sequential_until()
            continue

        if event_type == "WorkflowStarted":
            frontier = {node_id}
        elif event_type in {"StepRequested", "ChildWorkflowRequested"}:
            if node_id not in pending_requests and node_id not in gather_children:
                pending_requests.append(node_id)
                pending_parents[node_id] = parents_for_request(payload)
        elif event_type in {"GatherWaiting", "ChildWorkflowGatherWaiting"}:
            pending = [str(item) for item in payload.get("pending") or []]
            parents = frontier or {"workflow:start"}
            for child_id in pending:
                gather_children.add(child_id)
                for parent_id in parents:
                    add_edge(parent_id, child_id)
                add_edge(child_id, node_id)
            pending_requests[:] = [item for item in pending_requests if item not in set(pending)]
            for child_id in pending:
                pending_parents.pop(child_id, None)
            frontier = {node_id}
        elif event_type in {"StepCompleted", "StepFailed", "ChildWorkflowCompleted", "ChildWorkflowFailed"}:
            if event_type in {"StepCompleted", "ChildWorkflowCompleted"} and "output" in payload:
                completed_outputs[node_id] = payload.get("output")
            if node_id not in gather_children:
                flush_sequential_until(node_id)
        elif event_type in {"ApprovalRequested", "AgentRequested"}:
            if node_id in pending_requests:
                continue
            flush_sequential_until()
            if node_id not in frontier or "StepRequested" not in nodes[node_id].get("event_types", []):
                for parent_id in frontier:
                    add_edge(parent_id, node_id)
                frontier = {node_id}
        elif event_type == "WaitRequested":
            continue
        elif event_type == "SignalReceived":
            flush_sequential_until()
            if node_id not in frontier or "StepRequested" not in nodes[node_id].get("event_types", []):
                for parent_id in frontier:
                    add_edge(parent_id, node_id)
                frontier = {node_id}
        elif event_type in {"WorkflowCompleted", "WorkflowFailed", "WorkflowCancelled"}:
            flush_sequential_until()
            for parent_id in frontier:
                add_edge(parent_id, node_id)
            frontier = {node_id}

    flush_sequential_until()
    for artifact in artifacts:
        node_id = _artifact_node_id(artifact)
        if node_id and node_id in nodes:
            nodes[node_id]["artifacts"].append(artifact)
    outgoing_targets = {edge["from"] for edge in edges}
    for node in nodes.values():
        node["artifact_count"] = len(node["artifacts"])
        if node.get("kind") == "gather" and node.get("id") in outgoing_targets and node.get("status") == "waiting":
            node["status"] = "completed"
    return {
        "workflow_id": status.get("workflow_id"),
        "run": status,
        "layout": "run-derived-topology",
        "nodes": list(nodes.values()),
        "edges": edges,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "runtime_semantics": _runtime_semantics(),
    }


def _risk_for_approval(approval: dict[str, Any]) -> dict[str, str]:
    artifact = approval.get("artifact")
    text = json.dumps({"artifact": artifact}, default=str).lower()
    if any(word in text for word in ("payment", "purchase", "delete", "publish", "send_email", "external_send", "credential")):
        return {"level": "high", "reason": "The approval appears to authorize an external, destructive, financial, or credential-affecting action."}
    if any(word in text for word in ("email", "calendar", "schedule", "deploy", "post", "message")):
        return {"level": "medium", "reason": "The approval may affect people, publishing, scheduling, or deployment state."}
    return {"level": "low", "reason": "Approval records human provenance and resumes the trusted local workflow; no obvious external/destructive keyword was detected."}


def _review_request_schema_descriptor(schema_id: str) -> dict[str, Any]:
    normalized = schema_id or "json"
    if ":" in normalized:
        module, name = normalized.rsplit(":", 1)
    else:
        module, name = "", normalized
    if name == "ReviewDecision":
        kind = "review_decision"
    elif normalized in {"json", "dict", "builtins:dict"}:
        kind = "json_object"
    elif normalized in {"str", "builtins:str"}:
        kind = "text"
    else:
        kind = "structured_object"
    descriptor = {"id": normalized, "name": name or normalized, "kind": kind}
    if module:
        descriptor["module"] = module
    return descriptor


def _review_input_surface(schema: str | dict[str, Any]) -> dict[str, Any]:
    descriptor = schema if isinstance(schema, dict) else _review_request_schema_descriptor(schema)
    fields = descriptor.get("fields") if isinstance(descriptor.get("fields"), list) else []
    action_field = next((field for field in fields if isinstance(field, dict) and field.get("name") == "action" and field.get("kind") == "choice"), None)
    action_options = (action_field.get("options") or []) if isinstance(action_field, dict) else []
    if action_field:
        return {
            "kind": "review_decision",
            "actions": [_review_action_descriptor(option) for option in action_options],
            "feedback": {"kind": "text", "optional": True, "placeholder": "What should change?"},
        }
    if descriptor["kind"] == "review_decision":
        return {
            "kind": "review_decision",
            "actions": [_review_action_descriptor(action) for action in ["approve", "request_changes"]],
            "feedback": {"kind": "text", "optional": True},
        }
    if descriptor["kind"] == "text":
        return {"kind": "textarea", "placeholder": "Enter feedback"}
    if descriptor["kind"] == "structured_object":
        return {"kind": "structured_form", "schema": descriptor}
    return {"kind": "json_object", "schema": descriptor}


def _review_action_descriptor(action: Any) -> dict[str, Any]:
    value = str(action)
    label = value.replace("_", " ").strip().capitalize() or value
    item: dict[str, Any] = {"value": value, "label": label}
    if value != "approve":
        item["requires_feedback"] = True
    return item


def _risk_for_operator_step(step: dict[str, Any]) -> dict[str, str]:
    raw_request = step.get("request")
    request: dict[str, Any] = raw_request if isinstance(raw_request, dict) else {}
    artifact = step.get("artifact") if step.get("artifact") is not None else request.get("artifact")
    text = json.dumps({"artifact": artifact, "request": step.get("request")}, default=str).lower()
    if any(word in text for word in ("payment", "purchase", "delete", "publish", "send_email", "external_send", "credential")):
        return {"level": "high", "reason": "This human input request may authorize an external, destructive, financial, or credential-affecting action."}
    if any(word in text for word in ("email", "calendar", "schedule", "deploy", "post", "message")):
        return {"level": "medium", "reason": "This human input request may affect people, publishing, scheduling, or deployment state."}
    return {"level": "low", "reason": "This human input request records input/provenance for the trusted local workflow."}


def _operator_step_card(step: dict[str, Any], *, db_alias: str) -> dict[str, Any]:
    raw_request = step.get("request")
    request: dict[str, Any] = raw_request if isinstance(raw_request, dict) else {}
    artifact = _operator_approval_artifact(step.get("artifact") if step.get("artifact") is not None else request.get("artifact"))
    prompt = step.get("prompt") or step.get("label") or step.get("key") or "Operator input needed"
    schema_id = str(step.get("schema") or request.get("schema") or "json")
    raw_descriptor = step.get("schema_descriptor") if isinstance(step.get("schema_descriptor"), dict) else request.get("schema_descriptor")
    descriptor = raw_descriptor if isinstance(raw_descriptor, dict) else _review_request_schema_descriptor(schema_id)
    return {
        "db_alias": db_alias,
        "workflow_id": step.get("workflow_id"),
        "workflow_name": step.get("workflow_name"),
        "workflow_ref": step.get("workflow_ref"),
        "key": step.get("key"),
        "status": step.get("status"),
        "kind": "human_input",
        "request_type": "human_input",
        "headline": prompt,
        "prompt": prompt,
        "schema": schema_id,
        "request_schema": descriptor,
        "input_surface": _review_input_surface(descriptor),
        "artifact_preview": _redact_artifact_local_refs(artifact),
        "artifact_render": _artifact_descriptor(artifact),
        "output": step.get("output"),
        "source": step.get("source"),
        "waiting_on": step.get("waiting_on"),
        "requested_seq": step.get("requested_seq"),
        "risk": _risk_for_operator_step(step),
        "consequence": "Records typed human input with provenance, then the workflow worker or trusted runtime can continue.",
    }


def _approval_card(approval: dict[str, Any], *, db_alias: str) -> dict[str, Any]:
    prompt = approval.get("prompt") or approval.get("key") or "Approval needed"
    artifact = _operator_approval_artifact(approval.get("artifact"))
    return {
        "db_alias": db_alias,
        "workflow_id": approval.get("workflow_id"),
        "workflow_name": approval.get("workflow_name"),
        "workflow_ref": approval.get("workflow_ref"),
        "key": approval.get("key"),
        "status": approval.get("status"),
        "kind": "approval_policy",
        "request_type": "approval_policy",
        "headline": prompt,
        "prompt": prompt,
        "allowed": approval.get("allowed") or ["approve", "reject"],
        "request_schema": {
            "id": "hermes_workflows.approvals:ApprovalDecision",
            "name": "ApprovalDecision",
            "kind": "approval_decision",
        },
        "input_surface": {
            "kind": "approval_decision",
            "actions": list(approval.get("allowed") or ["approve", "reject"]),
            "feedback": {"kind": "text", "optional": True},
        },
        "artifact_preview": _redact_artifact_local_refs(artifact),
        "artifact_render": _artifact_descriptor(artifact),
        "decision": approval.get("decision"),
        "source": approval.get("source"),
        "diagnostics": approval.get("diagnostics") or [],
        "waiting_on": approval.get("waiting_on"),
        "requested_seq": approval.get("requested_seq"),
        "risk": _risk_for_approval(approval),
        "consequence": "Records approve/reject with human provenance, then resumes the trusted local workflow immediately.",
        "detail_url": f"/approvals/detail?db={db_alias}&workflow_id={approval.get('workflow_id')}&key={approval.get('key')}",
    }


def _artifacts_from_status(status: dict[str, Any]) -> list[dict[str, Any]]:
    workflow_id = str(status.get("workflow_id") or "")
    artifacts: list[dict[str, Any]] = []
    for approval in status.get("approvals") or []:
        artifact = _operator_approval_artifact(approval.get("artifact"))
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
    for operator_step in status.get("operator_steps") or []:
        if operator_step.get("kind") != "operator":
            continue
        request = operator_step.get("request") if isinstance(operator_step.get("request"), dict) else {}
        artifact = _operator_approval_artifact(operator_step.get("artifact") if operator_step.get("artifact") is not None else request.get("artifact"))
        if artifact is not None:
            artifacts.append(
                {
                    "id": f"{workflow_id}:operator:{operator_step.get('key')}",
                    "workflow_id": workflow_id,
                    "kind": "operator_step_artifact",
                    "title": operator_step.get("prompt") or operator_step.get("label") or operator_step.get("key") or "Operator step artifact",
                    "source": {"event": "StepRequested", "key": operator_step.get("key"), "seq": operator_step.get("requested_seq")},
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
        if event.get("type") == "ChildWorkflowRequested":
            payload = event.get("payload") or {}
            workflow_artifact = _workflow_source_artifact(
                payload.get("workflow"),
                artifact_id=f"{workflow_id}:child-workflow-source:{event.get('key')}",
                workflow_id=workflow_id,
                title=f"Generated child workflow source: {payload.get('symbol') or event.get('key')}",
                source={"event": "ChildWorkflowRequested", "key": event.get("key"), "seq": event.get("seq")},
            )
            if workflow_artifact is not None:
                artifacts.append(workflow_artifact)
            continue
        if event.get("type") != "StepCompleted":
            continue
        payload = event.get("payload") or {}
        if "output" in payload:
            workflow_artifact = _workflow_source_artifact(
                payload.get("output"),
                artifact_id=f"{workflow_id}:workflow-source:{event.get('key')}",
                workflow_id=workflow_id,
                title=f"Generated workflow source: {event.get('key')}",
                source={"event": "StepCompleted", "key": event.get("key"), "seq": event.get("seq")},
                metadata=payload.get("metadata"),
            )
            if workflow_artifact is not None:
                artifacts.append(workflow_artifact)
                continue
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
    configured = _configured_dbs()
    dbs = []
    for name, path in sorted(configured.items()):
        dbs.append({"name": name, "exists": Path(path).expanduser().exists()})
    active_source = None
    try:
        active_alias, active_path = _resolve_dashboard_db(None)
        active_source = {"name": active_alias, "exists": Path(active_path).expanduser().exists()}
    except HTTPException:
        active_source = None
    return {"count": len(dbs), "dbs": dbs, "active_source": active_source, "runtime_semantics": _runtime_semantics()}


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


@router.get("/definitions/{definition_id}/source")
async def workflow_definition_source(definition_id: str, db: str | None = None) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    definition = _definition_by_id(definition_id, engine)
    _ensure_workflow_project_on_path(db_path)
    return {"db_alias": db_alias, **_workflow_source_payload(definition)}


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
        _ensure_workflow_project_on_path(db_path)
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


@router.get("/runs/{workflow_id}/dag")
async def run_dag(workflow_id: str, db: str | None = None, recent_events: int = 200) -> dict[str, Any]:
    status = await run_status(
        workflow_id,
        db=db,
        recent_events=recent_events,
        commands="all",
        command_limit=200,
        command_payload_chars=5000,
    )
    _db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    child_runs = _child_run_dags(
        engine,
        status["run"],
        recent_events=_int(recent_events, default=200, maximum=200),
        command_limit=50,
        command_payload_chars=2000,
    )
    return {"db_alias": status["db_alias"], **_run_dag_payload(status["run"], status["artifacts"], child_runs=child_runs)}


@router.get("/operator-steps")
async def active_operator_steps(db: str | None = None, status: str | None = "waiting", limit: int = 100) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    operator_steps = [
        _operator_step_card(_strip_internal_fields(step), db_alias=db_alias)
        for step in engine.list_operator_steps(status=status)[: _int(limit, default=100, maximum=500)]
    ]
    return {"db_alias": db_alias, "count": len(operator_steps), "operator_steps": operator_steps, "runtime_semantics": _runtime_semantics()}


@router.get("/review-requests")
async def active_review_requests(db: str | None = None, status: str | None = "waiting", limit: int = 100) -> dict[str, Any]:
    db_alias, db_path = _resolve_dashboard_db(db)
    engine = WorkflowEngine(db_path, read_only=True)
    max_items = _int(limit, default=100, maximum=500)
    approvals = [
        _approval_card(approval_view_to_dict(approval), db_alias=db_alias)
        for approval in engine.list_approvals(status=status)[:max_items]
    ]
    remaining = max(0, max_items - len(approvals))
    human_inputs = [
        _operator_step_card(_strip_internal_fields(step), db_alias=db_alias)
        for step in engine.list_operator_steps(status=status)[:remaining]
    ]
    review_requests = approvals + human_inputs
    return {"db_alias": db_alias, "count": len(review_requests), "review_requests": review_requests, "runtime_semantics": _runtime_semantics()}


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
    approval = _redact_artifact_local_refs(_strip_internal_fields(approval_view_to_dict(engine.get_approval(workflow_id, key))))
    approval["artifact"] = _operator_approval_artifact(approval.get("artifact"))
    status = _status_packet(engine, workflow_id, recent_events=100, commands="recent", command_limit=20, command_payload_chars=5000)
    timeline = [event for event in engine.events(workflow_id) if event.get("seq", 0) <= (approval.get("requested_seq") or 10**9)]
    timeline = _redact_artifact_local_refs(_redact(timeline))
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
                },
        "risk": card["risk"],
        "consequence": card["consequence"],
        "decision_semantics": {
            "resume": True,
            "label": "Record and resume",
            "description": "The dashboard records approve/reject with server-derived human provenance, then resumes the trusted local workflow immediately in this Hermes process.",
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
    operator_steps = [_operator_step_card(_strip_internal_fields(step), db_alias=db_alias) for step in engine.list_operator_steps(status="waiting")[:50]]
    review_requests = approvals + operator_steps
    return {
        "db_alias": db_alias,
        "workflow_count": len(workflows),
        "counts_by_status": counts_by_status,
        "workflows": workflows,
        "definitions_count": len(definitions),
        "definitions": definitions,
        "active_review_request_count": len(review_requests),
        "active_review_requests": review_requests,
        "active_operator_step_count": len(operator_steps),
        "active_operator_steps": operator_steps,
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


@router.post("/operator-steps/response")
async def respond_operator_step(body: dict[str, Any]) -> dict[str, Any]:
    return await respond_review_request(body)


@router.post("/review-requests/response")
async def respond_review_request(body: dict[str, Any]) -> dict[str, Any]:
    decision_by = _dashboard_decision_by_id()
    if not decision_by:
        raise HTTPException(
            status_code=403,
            detail="Dashboard review responses require server-configured dashboard_decision_by_id.",
        )
    db_alias, db_path = _resolve_dashboard_db(body.get("db"))
    workflow_id = str(body.get("workflow_id") or "").strip()
    key = str(body.get("key") or "").strip()
    if not workflow_id or not key:
        raise HTTPException(status_code=400, detail="workflow_id and key are required")
    raw_payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if not raw_payload:
        raise HTTPException(status_code=400, detail="review response payload is required")
    decision_actor = _dashboard_decision_actor(decision_by)
    payload = {key: value for key, value in raw_payload.items() if key not in {"by", "source"}}
    payload["by"] = decision_actor
    message_id = f"dashboard:{uuid.uuid4()}"

    def record_and_resume() -> tuple[Any, dict[str, Any]]:
        _ensure_workflow_project_on_path(db_path)
        receipt = WorkflowEngine(db_path).submit_operator_response(
            workflow_id=workflow_id,
            key=key,
            payload=payload,
            source={
                "kind": "human",
                "id": decision_actor,
                "channel": "hermes-dashboard",
                "message_id": message_id,
            },
            idempotency_key=message_id,
            resume=True,
        )
        post_resume = _status_packet(
            WorkflowEngine(db_path, read_only=True),
            workflow_id,
            recent_events=20,
            commands="recent",
            command_limit=20,
            command_payload_chars=2000,
        )
        return receipt, post_resume

    try:
        receipt, post_resume = await asyncio.to_thread(record_and_resume)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"review response/resume failed: {type(exc).__name__}: {exc}") from exc
    receipt_payload = _receipt_to_payload(receipt, resume_requested=True)
    return {
        "success": True,
        "db_alias": db_alias,
        "receipt": receipt_payload,
        "post_resume": post_resume,
        "next_step": _next_step_for_receipt(receipt_payload),
    }


@router.post("/approvals/decision")
async def decide_approval(body: dict[str, Any]) -> dict[str, Any]:
    # Dashboard approvals derive human provenance from server-side plugin
    # configuration, never from untrusted browser JSON. After recording the
    # decision they immediately resume trusted local workflow code.
    decision_by = _dashboard_decision_by_id()
    if not decision_by:
        raise HTTPException(
            status_code=403,
            detail="Dashboard approvals require server-configured dashboard_decision_by_id.",
        )
    db_alias, db_path = _resolve_dashboard_db(body.get("db"))
    action = str(body.get("action") or "approve").strip().lower()
    if action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="action must be approve or reject")
    workflow_id = str(body.get("workflow_id") or "").strip()
    key = str(body.get("key") or "").strip()
    if not workflow_id or not key:
        raise HTTPException(status_code=400, detail="workflow_id and key are required")

    decision_actor = _dashboard_decision_actor(decision_by)
    message_id = f"dashboard:{uuid.uuid4()}"
    decision = ApprovalDecisionInput(
        workflow_id=workflow_id,
        key=key,
        action=action,
        by=decision_actor,
        source={
            "kind": "human",
            "id": decision_actor,
            "channel": "hermes-dashboard",
            "message_id": message_id,
        },
        note=body.get("note"),
        reason=body.get("reason"),
        idempotency_key=message_id,
    )
    def record_and_resume() -> tuple[Any, dict[str, Any]]:
        _ensure_workflow_project_on_path(db_path)
        receipt = WorkflowEngine(db_path).submit_approval_decision(decision, resume=True)
        post_resume = _status_packet(
            WorkflowEngine(db_path, read_only=True),
            workflow_id,
            recent_events=20,
            commands="recent",
            command_limit=20,
            command_payload_chars=2000,
        )
        return receipt, post_resume

    try:
        receipt, post_resume = await asyncio.to_thread(record_and_resume)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"approval decision/resume failed: {type(exc).__name__}: {exc}") from exc
    receipt_payload = _receipt_to_payload(receipt, resume_requested=True)
    return {
        "success": True,
        "db_alias": db_alias,
        "receipt": receipt_payload,
        "post_resume": post_resume,
        "next_step": _next_step_for_receipt(receipt_payload),
    }

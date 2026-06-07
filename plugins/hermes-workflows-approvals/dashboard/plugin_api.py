from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from hermes_workflows import ApprovalDecisionInput, WorkflowEngine
from hermes_workflows.hermes_plugin_approvals import (
    _configured_dbs,
    _next_step_for_receipt,
    _redact,
    _receipt_to_payload,
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
    return packet


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


@router.get("/dbs")
async def list_dbs() -> dict[str, Any]:
    dbs = []
    for name, path in sorted(_configured_dbs().items()):
        dbs.append({"name": name, "path": path, "exists": Path(path).expanduser().exists()})
    return {"count": len(dbs), "dbs": dbs}


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
    for item in workflows:
        counts_by_status[str(item.get("status") or "unknown")] = counts_by_status.get(str(item.get("status") or "unknown"), 0) + 1
    return {
        "db": db_path,
        "db_alias": db_alias,
        "workflow_count": len(workflows),
        "counts_by_status": counts_by_status,
        "workflows": workflows,
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
        "db": db_path,
        "db_alias": db_alias,
        "receipt": receipt_payload,
        "next_step": _next_step_for_receipt(receipt_payload),
    }

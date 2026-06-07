from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_workflows import WorkflowEngine
from hermes_workflows.hermes_plugin_approvals import (
    _configured_dbs,
    _handle_workflow_approval_decide,
    _redact,
    resolve_db,
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


def _json_tool_result(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"invalid plugin tool response: {exc}") from exc
    if not payload.get("success"):
        raise HTTPException(status_code=400, detail=str(payload.get("error") or "approval decision failed"))
    return payload


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
    db_path = resolve_db(db)
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
    db_path = resolve_db(db)
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
    # Dashboard approvals intentionally default to record-only. Operators can opt
    # into resume per click, but the dashboard must not surprise-run workflows.
    args = dict(body)
    args.setdefault("resume", False)
    return _json_tool_result(_handle_workflow_approval_decide(args))

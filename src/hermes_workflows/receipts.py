from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import RunResult, WorkflowEngine
from .registry import WorkflowDbConfig, WorkflowRefConfig

SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "share",
    "sheet_id",
    "token",
)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if any(fragment in key_str.lower() for fragment in SECRET_KEY_FRAGMENTS):
                out[key_str] = "[REDACTED]"
            else:
                out[key_str] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def build_workflow_receipt(
    *,
    engine: WorkflowEngine,
    result: RunResult,
    workflow_config: WorkflowRefConfig,
    db_config: WorkflowDbConfig,
    input_payload: Any | None = None,
    source: dict[str, Any] | None = None,
    dashboard_path: str | Path | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    status = engine.workflow_status(result.workflow_id)
    receipt: dict[str, Any] = {
        "workflow_id": result.workflow_id,
        "workflow_name": status.get("workflow_name"),
        "workflow_ref": status.get("workflow_ref") or workflow_config.workflow_ref,
        "registry_name": workflow_config.name,
        "db": {"alias": db_config.name},
        "status": result.status,
        "waiting_on": result.waiting_on,
        "result": redact_secrets(result.result),
        "error": "[REDACTED]" if result.error is not None else None,
        "approvals": redact_secrets(status.get("approvals", [])),
        "pending_commands": redact_secrets(status.get("pending_commands", [])),
        "event_count": status.get("event_count"),
    }
    if input_payload is not None:
        receipt["input"] = redact_secrets(input_payload)
    if source is not None:
        receipt["source"] = redact_secrets(source)
    if dashboard_path is not None:
        receipt["dashboard"] = str(dashboard_path)
    if worker_id is not None:
        receipt["worker_id"] = worker_id
    if status.get("terminal_reason") is not None:
        receipt["terminal_reason"] = redact_secrets(status.get("terminal_reason"))
    return receipt


def write_receipt(path: str | Path, receipt: dict[str, Any]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out

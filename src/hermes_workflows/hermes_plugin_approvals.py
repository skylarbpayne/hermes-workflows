from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast

from . import ApprovalDecisionInput, WorkflowEngine

PLUGIN_NAME = "hermes-workflows-approvals"
TOOLSET = "hermes_workflows_approvals"
_TOKEN_PREFIX = "hwf-approval:v1"
_SECRET_KEY_FRAGMENTS = (
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


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _tool_error(message: str, **extra: Any) -> str:
    return _json_result({"success": False, "error": message, **extra})


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"1", "true", "yes", "on"}:
            return True
        if cleaned in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_limit(value: Any, *, default: int = 20, maximum: int = 50) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(1, min(maximum, parsed))


def _platform_value(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    if value is None:
        return "unknown"
    return str(value)


def _configured_dbs() -> dict[str, str]:
    """Read configured workflow DB aliases without requiring Hermes at import time."""
    configured: dict[str, str] = {}
    env_db = os.getenv("HERMES_WORKFLOWS_DB")
    if env_db:
        configured["default"] = env_db
    env_dbs = os.getenv("HERMES_WORKFLOWS_DBS")
    if env_dbs:
        try:
            parsed = json.loads(env_dbs)
            if isinstance(parsed, dict):
                configured.update({str(k): str(v) for k, v in parsed.items()})
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("name") and item.get("path"):
                        configured[str(item["name"])] = str(item["path"])
        except Exception:
            pass
    try:
        from hermes_cli.config import cfg_get, load_config  # type: ignore

        config = load_config()
        entries = cfg_get(config, "plugins", "entries", PLUGIN_NAME, "workflow_dbs", default=[])
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict) and item.get("name") and item.get("path"):
                    configured[str(item["name"])] = str(item["path"])
        elif isinstance(entries, dict):
            configured.update({str(k): str(v) for k, v in entries.items()})
    except Exception:
        pass
    return configured


def _looks_like_path(value: str) -> bool:
    return value.startswith(("/", "./", "../", "~")) or os.sep in value or value.endswith((".db", ".sqlite", ".sqlite3"))


def _normalized_path(value: str) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def _db_alias_for_path(db_path: str) -> str | None:
    normalized = _normalized_path(db_path)
    for alias, configured_path in _configured_dbs().items():
        if _normalized_path(configured_path) == normalized:
            return alias
    return None


def resolve_db(db: Any = None) -> str:
    configured = _configured_dbs()
    if db is None or str(db).strip() == "":
        if len(configured) == 1:
            return next(iter(configured.values()))
        if "default" in configured:
            return configured["default"]
        raise ValueError("No workflow DB provided and no single configured DB was found.")
    raw = str(db).strip()
    if raw in configured:
        return configured[raw]
    if _looks_like_path(raw):
        return str(Path(raw).expanduser())
    raise ValueError(f"Unknown workflow DB alias {raw!r}. Provide a configured name or a path.")


def resolve_gateway_token_db(db: Any) -> str:
    raw = str(db or "").strip()
    if not raw:
        raise ValueError("Approval token missing DB alias.")
    if _looks_like_path(raw):
        raise ValueError("explicit DB paths are not accepted from gateway tokens; use a configured DB alias")
    configured = _configured_dbs()
    if raw not in configured:
        raise ValueError(f"Unknown workflow DB alias in approval token: {raw!r}")
    return configured[raw]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if any(fragment in key_str.lower() for fragment in _SECRET_KEY_FRAGMENTS):
                out[key_str] = "[REDACTED]"
            else:
                out[key_str] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _as_payload(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(cast(Any, value))
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _b64url_encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(payload: str) -> dict[str, Any]:
    if not payload or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in payload):
        raise ValueError("Approval token payload is not strict base64url.")
    padded = payload + "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    data = json.loads(decoded)
    if not isinstance(data, dict):
        raise ValueError("Approval token payload is not an object.")
    if _b64url_encode(data) != payload:
        raise ValueError("Approval token payload is not canonical base64url.")
    return data


def decision_token(action: str, db: str, workflow_id: str, key: str) -> str:
    action = str(action).strip().lower()
    if action not in {"approve", "reject"}:
        raise ValueError("action must be approve or reject")
    return f"{_TOKEN_PREFIX}:{action}:{_b64url_encode({'db': db, 'workflow_id': workflow_id, 'key': key})}"


def parse_decision_token(text: str) -> dict[str, str] | None:
    cleaned = text.strip()
    prefix = f"{_TOKEN_PREFIX}:"
    if not cleaned.startswith(prefix):
        return None
    parts = cleaned.split(":", 3)
    if len(parts) != 4:
        return None
    _, version, action, encoded = parts
    if version != "v1" or action not in {"approve", "reject"}:
        return None
    payload = _b64url_decode(encoded)
    db_raw = payload.get("db")
    workflow_id_raw = payload.get("workflow_id")
    key_raw = payload.get("key")
    if not all(isinstance(item, str) and item for item in (db_raw, workflow_id_raw, key_raw)):
        raise ValueError("Approval token missing db, workflow_id, or key.")
    return {
        "action": action,
        "db": cast(str, db_raw),
        "workflow_id": cast(str, workflow_id_raw),
        "key": cast(str, key_raw),
    }


def approval_view_to_dict(approval: Any) -> dict[str, Any]:
    payload = _as_payload(approval)
    payload["artifact"] = _redact(payload.get("artifact"))
    db_path = str(payload.get("db_path") or "")
    workflow_id = str(payload.get("workflow_id") or "")
    key = str(payload.get("key") or "")
    allowed = payload.get("allowed") or []
    db_ref = _db_alias_for_path(db_path)
    if not db_ref:
        payload["decision_token_error"] = "decision tokens require a configured workflow DB alias"
        return payload
    if "approve" in allowed:
        payload["decision_token_approve"] = decision_token("approve", db_ref, workflow_id, key)
    if "reject" in allowed:
        payload["decision_token_reject"] = decision_token("reject", db_ref, workflow_id, key)
    return payload


def _handle_workflow_approvals_list(args: dict[str, Any], **_: Any) -> str:
    try:
        db_path = resolve_db(args.get("db"))
        status = args.get("status", "waiting")
        limit = _coerce_limit(args.get("limit"))
        engine = WorkflowEngine(db_path, read_only=True)
        approvals = engine.list_approvals(status=status)
        payload = [approval_view_to_dict(approval) for approval in approvals[:limit]]
        return _json_result({"success": True, "db": db_path, "count": len(payload), "approvals": payload})
    except Exception as exc:
        return _tool_error(f"workflow_approvals_list failed: {type(exc).__name__}: {exc}")


def _source_from_args(args: dict[str, Any]) -> dict[str, Any]:
    by = str(args.get("by") or args.get("user") or "human").strip()
    channel = str(args.get("channel") or "hermes-plugin").strip()
    source = {"kind": "human", "id": by, "channel": channel}
    for field in ("message_id", "message_url", "event_id"):
        value = args.get(field)
        if value:
            source[field] = str(value)
    return source


def _receipt_to_payload(receipt: Any, *, resume_requested: bool) -> dict[str, Any]:
    payload = _as_payload(receipt)
    payload["resume_requested"] = resume_requested
    return payload


def _next_step_for_receipt(receipt_payload: dict[str, Any]) -> str | None:
    if receipt_payload.get("resume_requested"):
        return None
    if receipt_payload.get("status") in {"completed", "failed", "cancelled"}:
        return None
    workflow_ref = receipt_payload.get("workflow_ref")
    if workflow_ref:
        return f"Run or queue a trusted workflow resumer for workflow_ref {workflow_ref}."
    return "Run or queue a trusted workflow resumer for this workflow instance."


def _handle_workflow_approval_decide(args: dict[str, Any], **_: Any) -> str:
    try:
        db_path = resolve_db(args.get("db"))
        action = str(args.get("action") or "approve").strip().lower()
        resume = _coerce_bool(args.get("resume"), default=False)
        by = str(args.get("by") or args.get("user") or "human").strip()
        if action not in {"approve", "reject"}:
            raise ValueError("action must be approve or reject")
        decision = ApprovalDecisionInput(
            workflow_id=str(args.get("workflow_id") or "").strip(),
            key=str(args.get("key") or "").strip(),
            action=action,
            by=by,
            source=_source_from_args(args),
            note=args.get("note"),
            reason=args.get("reason"),
            idempotency_key=args.get("idempotency_key")
            or args.get("message_url")
            or args.get("message_id")
            or args.get("event_id"),
        )
        receipt = WorkflowEngine(db_path).submit_approval_decision(decision, resume=resume)
        receipt_payload = _receipt_to_payload(receipt, resume_requested=resume)
        next_step = _next_step_for_receipt(receipt_payload)
        return _json_result(
            {
                "success": True,
                "db": db_path,
                "receipt": receipt_payload,
                "next_step": next_step,
            }
        )
    except Exception as exc:
        return _tool_error(f"workflow_approval_decide failed: {type(exc).__name__}: {exc}")


def _event_message_id(event: Any, source: Any) -> str | None:
    for obj in (event, source):
        for attr in ("message_id", "event_id", "id"):
            value = getattr(obj, attr, None)
            if value:
                return str(value)
    return None


def _gateway_rejected(error: str) -> dict[str, Any]:
    return {"action": "skip", "reason": "workflow approval token rejected", "error": error}


def _handle_gateway_message(*, event: Any, gateway: Any = None, session_store: Any = None, **_: Any) -> dict[str, Any] | None:
    text = str(getattr(event, "text", "") or "").strip()
    if not text:
        return None
    try:
        parsed = parse_decision_token(text)
    except Exception as exc:
        return _gateway_rejected(f"{type(exc).__name__}: {exc}")
    if parsed is None:
        if text.startswith(f"{_TOKEN_PREFIX}:"):
            return _gateway_rejected("invalid approval token")
        return None
    try:
        db_path = resolve_gateway_token_db(parsed["db"])
    except Exception as exc:
        return _gateway_rejected(f"{type(exc).__name__}: {exc}")
    source_obj = getattr(event, "source", None)
    platform = _platform_value(getattr(source_obj, "platform", "unknown"))
    chat_id = getattr(source_obj, "chat_id", None)
    user_id = getattr(source_obj, "user_id", None) or getattr(source_obj, "user_name", None) or "human"
    channel = f"{platform}:{chat_id}" if chat_id else platform
    message_id = _event_message_id(event, source_obj)
    args: dict[str, Any] = {
        "db": db_path,
        "workflow_id": parsed["workflow_id"],
        "key": parsed["key"],
        "action": parsed["action"],
        "by": str(user_id),
        "channel": channel,
        "resume": False,
    }
    if message_id:
        args["message_id"] = message_id
        args["idempotency_key"] = f"{channel}:{message_id}:{parsed['workflow_id']}:{parsed['key']}:{parsed['action']}"
    raw = _handle_workflow_approval_decide(args)
    result = json.loads(raw)
    if not result.get("success"):
        return _gateway_rejected(str(result.get("error") or "approval decision failed"))
    return {
        "action": "skip",
        "reason": "workflow approval decision recorded",
        "receipt": result["receipt"],
        "next_step": result.get("next_step"),
    }


WORKFLOW_APPROVALS_LIST_SCHEMA = {
    "name": "workflow_approvals_list",
    "description": "List pending hermes-workflows approval requests from a configured workflow SQLite DB or explicit DB path.",
    "parameters": {
        "type": "object",
        "properties": {
            "db": {"type": "string", "description": "Configured DB alias or SQLite path."},
            "status": {"type": "string", "default": "waiting", "description": "Approval status filter."},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50},
        },
    },
}

WORKFLOW_APPROVAL_DECIDE_SCHEMA = {
    "name": "workflow_approval_decide",
    "description": "Record an approve/reject decision for a hermes-workflows approval with human provenance. Defaults to resume=false for gateway/plugin safety.",
    "parameters": {
        "type": "object",
        "required": ["db", "workflow_id", "key", "action", "by"],
        "properties": {
            "db": {"type": "string", "description": "Configured DB alias or SQLite path."},
            "workflow_id": {"type": "string"},
            "key": {"type": "string"},
            "action": {"type": "string", "enum": ["approve", "reject"]},
            "by": {"type": "string", "description": "Human approver id/name."},
            "channel": {"type": "string", "default": "hermes-plugin"},
            "message_id": {"type": "string"},
            "message_url": {"type": "string"},
            "event_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "note": {"type": "string"},
            "reason": {"type": "string"},
            "resume": {"type": "boolean", "default": False},
        },
    },
}


def register(ctx) -> None:
    ctx.register_tool(
        name="workflow_approvals_list",
        toolset=TOOLSET,
        schema=WORKFLOW_APPROVALS_LIST_SCHEMA,
        handler=_handle_workflow_approvals_list,
        description="List pending hermes-workflows approvals.",
        emoji="🧾",
    )
    ctx.register_tool(
        name="workflow_approval_decide",
        toolset=TOOLSET,
        schema=WORKFLOW_APPROVAL_DECIDE_SCHEMA,
        handler=_handle_workflow_approval_decide,
        description="Record a human approval/rejection for hermes-workflows.",
        emoji="✅",
    )
    ctx.register_hook("pre_gateway_dispatch", _handle_gateway_message)

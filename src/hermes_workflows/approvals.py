from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApprovalView:
    """Runtime-agnostic approval card view for dashboards, plugins, and chat adapters."""

    db_path: str
    workflow_id: str
    workflow_name: str
    workflow_ref: str | None
    key: str
    status: str
    prompt: str | None
    artifact: Any
    approver: str | None
    allowed: list[str]
    authority: Any
    timeout: str | None
    waiting_on: str | None
    requested_seq: int | None
    source: dict[str, Any] | None
    decision: dict[str, Any] | None
    diagnostics: list[dict[str, Any]]


@dataclass(frozen=True)
class ApprovalDecisionInput:
    """Human approval decision captured by any adapter surface."""

    workflow_id: str
    key: str
    action: str
    by: str
    source: dict[str, Any]
    note: str | None = None
    reason: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ApprovalReceipt:
    """Receipt returned after the core approval state machine accepts a decision."""

    workflow_id: str
    key: str
    action: str
    by: str
    source: dict[str, Any]
    status: str
    waiting_on: str | None
    result_summary: dict[str, Any] | None

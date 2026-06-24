from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping


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
    schema: str | None
    allowed: list[str]
    timeout: str | None
    waiting_on: str | None
    requested_seq: int | None
    source: dict[str, Any] | None
    decision: dict[str, Any] | None
    diagnostics: list[dict[str, Any]]


@dataclass(frozen=True)
class ApprovalDecision(Mapping[str, Any]):
    """Typed approval decision returned to workflow authors.

    It deliberately behaves like a read-only mapping too, so existing workflows
    that use ``decision["action"]`` or ``decision.get("action")`` keep working
    while new workflows can use typed helpers like ``decision.approved``.
    """

    action: str
    by: str
    source: dict[str, Any] | None = None
    note: str | None = None
    reason: str | None = None
    message: str | None = None
    comment: str | None = None
    direct_feedback: str | None = None

    @property
    def approved(self) -> bool:
        return self.action == "approve"

    @property
    def rejected(self) -> bool:
        return self.action == "reject"

    @property
    def needs_revision(self) -> bool:
        return self.action in {"reject", "edit", "revise", "rerun"}

    @property
    def feedback(self) -> str | None:
        return self.direct_feedback or self.reason or self.note or self.message or self.comment

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"action": self.action, "by": self.by}
        for key in ("note", "reason", "message", "comment"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.direct_feedback is not None:
            data["feedback"] = self.direct_feedback
        if self.source is not None:
            data["source"] = self.source
        return data

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


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
    workflow_ref: str | None = None


# Neutral names for the general human/operator checkpoint substrate. Approval
# remains a policy preset over this surface for now; these aliases let runtime,
# dashboard, and adapter code migrate without inventing another parallel model.
OperatorStepView = ApprovalView
OperatorDecision = ApprovalDecision
OperatorResponseInput = ApprovalDecisionInput
OperatorResponseReceipt = ApprovalReceipt

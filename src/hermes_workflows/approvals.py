from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from .provenance import project_response_provenance
from .revision_validation import ValidatedRevisionActionV1, validate_revision_action


CLIENT_CONTROLLED_PROVENANCE_FIELDS = frozenset(
    {
        "actor",
        "authenticated_principal",
        "by",
        "display_label",
        "principal",
        "provenance",
        "response_provenance",
        "source",
        "user",
    }
)


def strip_client_controlled_provenance(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove identity/provenance assertions from an untrusted response body."""

    if not isinstance(payload, Mapping):
        raise TypeError("response payload must be an object")
    return {key: value for key, value in payload.items() if key not in CLIENT_CONTROLLED_PROVENANCE_FIELDS}


def is_revision_action_schema(schema_descriptor: Any) -> bool:
    """Return whether a typed operator request uses the v1 revision action contract."""

    if not isinstance(schema_descriptor, Mapping):
        return False
    fields = schema_descriptor.get("fields")
    if not isinstance(fields, list):
        return False
    action = next((field for field in fields if isinstance(field, Mapping) and field.get("name") == "action"), None)
    if not isinstance(action, Mapping):
        return False
    options = action.get("options") if isinstance(action.get("options"), list) else schema_descriptor.get("choices")
    field_names = {str(field.get("name")) for field in fields if isinstance(field, Mapping)}
    return (
        isinstance(options, list)
        and {"approve", "request_changes"}.issubset(options)
        and bool({"feedback", "edited_output"} & field_names)
    )


def validate_revision_response(payload: Mapping[str, Any]) -> tuple[dict[str, Any], ValidatedRevisionActionV1]:
    """Validate and materialize a revision response for durable adapter submission."""

    validated = validate_revision_action(payload)
    return _materialize_json(validated.normalized_payload), validated


def response_provenance_for(*, by: str | None, source: Mapping[str, Any] | None) -> dict[str, Any]:
    """Truthfully classify an adapter response without upgrading client labels."""

    return project_response_provenance({"by": by, "source": source}).to_dict()


def _materialize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _materialize_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_materialize_json(item) for item in value]
    return value


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
    by: str | None = None
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

    @property
    def response_provenance(self) -> dict[str, Any]:
        return response_provenance_for(by=self.by, source=self.source)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"action": self.action}
        if self.by is not None:
            data["by"] = self.by
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
    by: str | None = None
    source: dict[str, Any] | None = None
    note: str | None = None
    reason: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ApprovalReceipt:
    """Receipt returned after the core approval state machine accepts a decision."""

    workflow_id: str
    key: str
    action: str
    by: str | None
    source: dict[str, Any]
    status: str
    waiting_on: str | None
    result_summary: dict[str, Any] | None
    workflow_ref: str | None = None

    @property
    def response_provenance(self) -> dict[str, Any]:
        return response_provenance_for(by=self.by, source=self.source)


# Neutral names for the general human/operator checkpoint substrate. Approval
# remains a policy preset over this surface for now; these aliases let runtime,
# dashboard, and adapter code migrate without inventing another parallel model.
OperatorStepView = ApprovalView
OperatorDecision = ApprovalDecision
OperatorResponseInput = ApprovalDecisionInput
OperatorResponseReceipt = ApprovalReceipt

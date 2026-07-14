from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any


PROVENANCE_CONTRACT_VERSION = 1
PROVENANCE_SERVICE_ID = "provenance.trusted_gateway"

LEGACY_UNVERIFIED = "legacy_unverified"
UNATTRIBUTED_LOCAL_OPERATOR = "unattributed_local_operator"
AUTHENTICATED_PRINCIPAL = "authenticated_principal"

_PROVENANCE_KINDS = frozenset(
    {LEGACY_UNVERIFIED, UNATTRIBUTED_LOCAL_OPERATOR, AUTHENTICATED_PRINCIPAL}
)
_RESERVED_CLIENT_FIELDS = frozenset(
    {
        "by",
        "user",
        "display_label",
        "principal",
        "authenticated_principal",
        "provenance",
        "source",
    }
)
_PRINCIPAL_FIELDS = frozenset(
    {
        "issuer",
        "subject",
        "platform",
        "tenant_id",
        "chat_id",
        "verified_at",
        "adapter_evidence_id",
    }
)
_EVENT_FIELDS = frozenset({"channel", "message_id", "message_url", "event_id"})
_RESPONSE_PROVENANCE_FIELDS = frozenset(
    {"schema_version", "kind", "principal", "display_label", "event"}
)
_MAX_TEXT_BYTES = 1024
_MAX_HTTP_BODY_BYTES = 64 * 1024
_MAX_RESPONSE_JSON_DEPTH = 32
_MAX_RESPONSE_JSON_NODES = 4096
_MAX_RESPONSE_CANONICAL_BYTES = 64 * 1024
_RESPONSE_LIMITS_ERROR = "response payload exceeds JSON limits"


@dataclass(frozen=True)
class AuthenticatedPrincipalV1:
    """Immutable identity established by a trusted gateway verifier."""

    issuer: str
    subject: str
    platform: str
    tenant_id: str | None
    chat_id: str | None
    verified_at: str
    adapter_evidence_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _required_text(self.issuer, label="issuer"))
        object.__setattr__(self, "subject", _required_text(self.subject, label="subject"))
        object.__setattr__(self, "platform", _required_text(self.platform, label="platform"))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, label="tenant_id"))
        object.__setattr__(self, "chat_id", _optional_text(self.chat_id, label="chat_id"))
        if self.tenant_id is None and self.chat_id is None:
            raise ValueError("authenticated principal requires tenant_id or chat_id")
        verified_at = _required_text(self.verified_at, label="verified_at")
        _validate_aware_timestamp(verified_at)
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(
            self,
            "adapter_evidence_id",
            _required_text(self.adapter_evidence_id, label="adapter_evidence_id"),
        )

    @property
    def identity_key(self) -> tuple[str, str, str, str | None, str | None]:
        return (self.issuer, self.subject, self.platform, self.tenant_id, self.chat_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "subject": self.subject,
            "platform": self.platform,
            "tenant_id": self.tenant_id,
            "chat_id": self.chat_id,
            "verified_at": self.verified_at,
            "adapter_evidence_id": self.adapter_evidence_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuthenticatedPrincipalV1":
        _require_exact_fields(payload, _PRINCIPAL_FIELDS, label="authenticated principal")
        return cls(
            issuer=payload["issuer"],
            subject=payload["subject"],
            platform=payload["platform"],
            tenant_id=payload["tenant_id"],
            chat_id=payload["chat_id"],
            verified_at=payload["verified_at"],
            adapter_evidence_id=payload["adapter_evidence_id"],
        )


@dataclass(frozen=True)
class EventProvenanceV1:
    """Non-identity evidence locating the response event."""

    channel: str
    message_id: str | None = None
    message_url: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel", _required_text(self.channel, label="channel"))
        for field_name in ("message_id", "message_url", "event_id"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), label=field_name),
            )
        if self.message_id is None and self.message_url is None and self.event_id is None:
            raise ValueError("event provenance requires message_id, message_url, or event_id")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "channel": self.channel,
            "message_id": self.message_id,
            "message_url": self.message_url,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EventProvenanceV1":
        _require_exact_fields(payload, _EVENT_FIELDS, label="event provenance")
        return cls(
            channel=payload["channel"],
            message_id=payload["message_id"],
            message_url=payload["message_url"],
            event_id=payload["event_id"],
        )


@dataclass(frozen=True)
class ResponseProvenanceV1:
    """Truthful classification of identity, display, and event evidence."""

    kind: str
    principal: AuthenticatedPrincipalV1 | None = None
    display_label: str | None = None
    event: EventProvenanceV1 | None = None
    schema_version: int = PROVENANCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be an integer")
        if self.schema_version != PROVENANCE_CONTRACT_VERSION:
            raise ValueError(f"schema_version must equal {PROVENANCE_CONTRACT_VERSION}")
        if self.kind not in _PROVENANCE_KINDS:
            raise ValueError(f"unknown provenance kind: {self.kind!r}")
        if self.principal is not None and not isinstance(self.principal, AuthenticatedPrincipalV1):
            raise TypeError("principal must be AuthenticatedPrincipalV1 or None")
        if self.event is not None and not isinstance(self.event, EventProvenanceV1):
            raise TypeError("event must be EventProvenanceV1 or None")
        object.__setattr__(
            self,
            "display_label",
            _optional_text(self.display_label, label="display_label"),
        )
        if self.kind == AUTHENTICATED_PRINCIPAL:
            if self.principal is None:
                raise ValueError("authenticated_principal provenance requires a principal")
            if self.event is None:
                raise ValueError("authenticated_principal provenance requires event evidence")
        elif self.principal is not None:
            raise ValueError(f"{self.kind} provenance cannot contain a principal")
        if self.kind == UNATTRIBUTED_LOCAL_OPERATOR:
            if self.display_label is not None:
                raise ValueError("unattributed_local_operator provenance cannot contain a display label")
            if self.event is None:
                raise ValueError("unattributed_local_operator provenance requires event evidence")

    @classmethod
    def authenticated(
        cls,
        principal: AuthenticatedPrincipalV1,
        display_label: str | None,
        event: EventProvenanceV1,
    ) -> "ResponseProvenanceV1":
        return cls(
            kind=AUTHENTICATED_PRINCIPAL,
            principal=principal,
            display_label=display_label,
            event=event,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "principal": self.principal.to_dict() if self.principal is not None else None,
            "display_label": self.display_label,
            "event": self.event.to_dict() if self.event is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResponseProvenanceV1":
        _require_exact_fields(payload, _RESPONSE_PROVENANCE_FIELDS, label="response provenance")
        principal_payload = payload["principal"]
        event_payload = payload["event"]
        if principal_payload is not None and not isinstance(principal_payload, Mapping):
            raise TypeError("response provenance principal must be an object or null")
        if event_payload is not None and not isinstance(event_payload, Mapping):
            raise TypeError("response provenance event must be an object or null")
        return cls(
            schema_version=payload["schema_version"],
            kind=payload["kind"],
            principal=(
                AuthenticatedPrincipalV1.from_dict(principal_payload)
                if principal_payload is not None
                else None
            ),
            display_label=payload["display_label"],
            event=EventProvenanceV1.from_dict(event_payload) if event_payload is not None else None,
        )


@dataclass(frozen=True)
class TrustedGatewayContextV1:
    """Server-owned context produced only after gateway authentication."""

    principal: AuthenticatedPrincipalV1
    display_label: str | None
    event: EventProvenanceV1

    def __post_init__(self) -> None:
        if not isinstance(self.principal, AuthenticatedPrincipalV1):
            raise TypeError("principal must be AuthenticatedPrincipalV1")
        if not isinstance(self.event, EventProvenanceV1):
            raise TypeError("event must be EventProvenanceV1")
        object.__setattr__(
            self,
            "display_label",
            _optional_text(self.display_label, label="display_label"),
        )


@dataclass(frozen=True, init=False)
class StampedResponseV1:
    """Sanitized client response paired with server-owned provenance."""

    payload: Mapping[str, Any]
    provenance: ResponseProvenanceV1

    def __init__(self, payload: Mapping[str, Any], provenance: ResponseProvenanceV1) -> None:
        if not isinstance(payload, Mapping):
            raise TypeError("response payload must be a JSON object")
        try:
            normalized = _normalize_json(payload)
        except RecursionError as exc:
            raise ValueError(_RESPONSE_LIMITS_ERROR) from exc
        if not isinstance(normalized, dict):
            raise TypeError("response payload must be a JSON object")
        if not isinstance(provenance, ResponseProvenanceV1):
            raise TypeError("provenance must be ResponseProvenanceV1")
        object.__setattr__(self, "payload", _freeze_json_object(normalized))
        object.__setattr__(self, "provenance", provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "payload": _thaw_json_object(self.payload),
            "provenance": self.provenance.to_dict(),
        }


class TrustedGatewayHTTPHookV1:
    """Owned HTTP-body hook; trusted context is supplied out-of-band by the server."""

    service_id = PROVENANCE_SERVICE_ID
    contract_version = PROVENANCE_CONTRACT_VERSION

    def handle_http(
        self,
        body: bytes | bytearray,
        *,
        context: TrustedGatewayContextV1,
        expected_principal: AuthenticatedPrincipalV1 | None = None,
    ) -> StampedResponseV1:
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("HTTP body must be bytes")
        if len(body) > _MAX_HTTP_BODY_BYTES:
            raise ValueError(f"HTTP body must be <= {_MAX_HTTP_BODY_BYTES} bytes")
        if not isinstance(context, TrustedGatewayContextV1):
            raise TypeError("trusted gateway context is required")
        try:
            payload = json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("HTTP body must be a valid bounded UTF-8 JSON object") from exc
        if not isinstance(payload, dict):
            raise TypeError("HTTP body must be a JSON object")

        sanitized = {key: value for key, value in payload.items() if key not in _RESERVED_CLIENT_FIELDS}
        provenance = ResponseProvenanceV1.authenticated(
            context.principal,
            context.display_label,
            context.event,
        )
        if expected_principal is not None:
            require_authenticated_principal(
                provenance,
                expected_principal=expected_principal,
            )
        return StampedResponseV1(sanitized, provenance)


def legacy_unverified_provenance(row: Mapping[str, Any]) -> ResponseProvenanceV1:
    """Classify free-form/old actor labels without inventing trust."""

    if not isinstance(row, Mapping):
        raise TypeError("legacy response row must be a mapping")
    source = row.get("source")
    display_label = _first_optional_text(row.get("by"), row.get("user"))
    if display_label is None and isinstance(source, Mapping):
        display_label = _first_optional_text(source.get("id"))
    return ResponseProvenanceV1(
        kind=LEGACY_UNVERIFIED,
        display_label=display_label,
        event=_project_legacy_event(source),
    )


def local_operator_provenance(*, event_id: str) -> ResponseProvenanceV1:
    """Create an honest receipt for an unattributed local dashboard action."""

    return ResponseProvenanceV1(
        kind=UNATTRIBUTED_LOCAL_OPERATOR,
        event=EventProvenanceV1(channel="local-dashboard", event_id=event_id),
    )


def project_response_provenance(row: Mapping[str, Any]) -> ResponseProvenanceV1:
    """Project canonical v1 provenance or conservatively classify an old row."""

    if not isinstance(row, Mapping):
        raise TypeError("response row must be a mapping")
    canonical = row.get("response_provenance")
    if canonical is None:
        source = row.get("source")
        event = _project_legacy_event(source)
        if (
            event is not None
            and isinstance(source, Mapping)
            and _first_optional_text(source.get("channel")) == "local-dashboard"
        ):
            return ResponseProvenanceV1(
                kind=UNATTRIBUTED_LOCAL_OPERATOR,
                event=event,
            )
        return legacy_unverified_provenance(row)
    if not isinstance(canonical, Mapping):
        raise TypeError("response_provenance must be an object")
    return ResponseProvenanceV1.from_dict(canonical)


def require_authenticated_principal(
    provenance: ResponseProvenanceV1,
    *,
    expected_principal: AuthenticatedPrincipalV1 | None = None,
) -> AuthenticatedPrincipalV1:
    """Fail closed unless provenance carries the expected authenticated identity."""

    if not isinstance(provenance, ResponseProvenanceV1):
        raise TypeError("provenance must be ResponseProvenanceV1")
    if provenance.kind != AUTHENTICATED_PRINCIPAL or provenance.principal is None:
        raise PermissionError(
            f"identity-required effect requires an authenticated principal; got {provenance.kind}"
        )
    if expected_principal is not None:
        if not isinstance(expected_principal, AuthenticatedPrincipalV1):
            raise TypeError("expected_principal must be AuthenticatedPrincipalV1")
        if provenance.principal.identity_key != expected_principal.identity_key:
            raise PermissionError("authenticated principal mismatch for identity-required effect")
    return provenance.principal


def _project_legacy_event(source: object) -> EventProvenanceV1 | None:
    if not isinstance(source, Mapping):
        return None
    channel = _first_optional_text(source.get("channel"))
    message_id = _first_optional_text(source.get("message_id"))
    message_url = _first_optional_text(source.get("message_url"))
    event_id = _first_optional_text(source.get("event_id"))
    if channel is None or (message_id is None and message_url is None and event_id is None):
        return None
    return EventProvenanceV1(
        channel=channel,
        message_id=message_id,
        message_url=message_url,
        event_id=event_id,
    )


def _required_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(f"{label} must be <= {_MAX_TEXT_BYTES} UTF-8 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{label} cannot contain control characters")
    return normalized


def _optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label=label)


def _first_optional_text(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            try:
                return _required_text(value, label="legacy display/event value")
            except ValueError:
                return None
    return None


def _validate_aware_timestamp(value: str) -> None:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("verified_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("verified_at must be timezone-aware")


def _require_exact_fields(payload: Mapping[str, Any], fields: frozenset[str], *, label: str) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be an object")
    unknown = set(payload) - fields
    missing = fields - set(payload)
    if unknown:
        raise ValueError(f"unknown {label} fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing {label} fields: {sorted(missing)}")


def _normalize_json(value: object) -> Any:
    nodes = 0

    def normalize(item: object, *, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_RESPONSE_JSON_DEPTH or nodes > _MAX_RESPONSE_JSON_NODES:
            raise ValueError(_RESPONSE_LIMITS_ERROR)
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if item != item or item in (float("inf"), float("-inf")):
                raise ValueError("response payload JSON numbers must be finite")
            return item
        if isinstance(item, Mapping):
            normalized: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TypeError("response payload JSON object keys must be strings")
                normalized[key] = normalize(child, depth=depth + 1)
            return normalized
        if isinstance(item, (list, tuple)):
            return [normalize(child, depth=depth + 1) for child in item]
        raise TypeError(f"response payload value of type {type(item).__name__} is not JSON")

    normalized = normalize(value, depth=0)
    try:
        canonical = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, ValueError) as exc:
        raise ValueError(_RESPONSE_LIMITS_ERROR) from exc
    if len(canonical) > _MAX_RESPONSE_CANONICAL_BYTES:
        raise ValueError(_RESPONSE_LIMITS_ERROR)
    return normalized


def _freeze_json_object(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_json_object(value)
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _thaw_json(item) for key, item in value.items()}


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_json_object(value)
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value

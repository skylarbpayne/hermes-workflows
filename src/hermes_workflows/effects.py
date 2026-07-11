from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Protocol, Union, cast, runtime_checkable


EFFECT_ADAPTER_SERVICE_ID = "effects.adapters"
EFFECT_ADAPTER_CONTRACT_VERSION = 1
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_STATE_VALUES = frozenset({"pending", "claimed", "completed", "failed"})


class EffectPolicy(str, Enum):
    PURE = "pure"
    IDEMPOTENT = "idempotent"
    UNSAFE = "unsafe"
    UNCLASSIFIED = "unclassified"


JsonScalar = Union[None, bool, int, float, str]
JsonValue = Union[JsonScalar, list["JsonValue"], dict[str, "JsonValue"]]


@dataclass(frozen=True)
class OperationIdentity:
    operation_id: str
    workflow_id: str
    effect_key: str
    adapter_id: str
    input_hash: str


@dataclass(frozen=True)
class EffectClaim:
    operation_id: str
    token: str
    attempt: int
    expires_at: float


@dataclass(frozen=True)
class EffectReceipt:
    operation_id: str
    adapter_receipt_id: str
    payload: Mapping[str, Any]
    receipt_hash: str
    sensitive: bool
    completed_at: float
    claim_token: str

    def descriptor(self) -> dict[str, Any]:
        """Return a bounded projection which never includes the receipt payload."""
        return {
            "operation_id": self.operation_id,
            "adapter_receipt_id": self.adapter_receipt_id,
            "receipt_hash": self.receipt_hash,
            "receipt_present": True,
            "sensitive": self.sensitive,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class EffectRecord:
    identity: OperationIdentity
    policy: EffectPolicy
    unsafe_authorized: bool
    state: str
    attempts: int
    claim_token: str | None
    claim_expires_at: float | None
    receipt: EffectReceipt | None
    error: Mapping[str, Any] | None


@runtime_checkable
class EffectAdapter(Protocol):
    adapter_id: str

    def lookup_receipt(self, operation_id: str) -> Mapping[str, Any] | None: ...

    def perform(self, operation_id: str, input_value: JsonValue) -> Mapping[str, Any]: ...


@runtime_checkable
class EffectAdapterResolver(Protocol):
    def resolve_adapter(self, adapter_id: str) -> EffectAdapter | None: ...


def canonical_json(value: Any) -> str:
    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def operation_identity(
    *,
    workflow_id: str,
    effect_key: str,
    adapter_id: str,
    input_value: Any,
    attempt: int | None = None,
) -> OperationIdentity:
    """Build an attempt-independent identity from logical effect coordinates and input."""
    del attempt
    input_json = canonical_json(input_value)
    return _operation_identity_from_input_json(
        workflow_id=workflow_id,
        effect_key=effect_key,
        adapter_id=adapter_id,
        input_json=input_json,
    )


def _operation_identity_from_input_json(
    *, workflow_id: str, effect_key: str, adapter_id: str, input_json: str
) -> OperationIdentity:
    _validate_id(workflow_id, "workflow_id")
    _validate_id(effect_key, "effect_key")
    _validate_id(adapter_id, "adapter_id")
    input_hash = _sha256(input_json)
    operation_document = canonical_json(
        {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "effect_key": effect_key,
            "adapter_id": adapter_id,
            "input_hash": input_hash,
        }
    )
    return OperationIdentity(
        operation_id=f"op_{_sha256(operation_document)}",
        workflow_id=workflow_id,
        effect_key=effect_key,
        adapter_id=adapter_id,
        input_hash=input_hash,
    )


def resolve_effect_adapter(runtime_services: Any, adapter_id: str) -> EffectAdapter:
    """Resolve the issue-owned adapter service through the FND-RT generic seam."""
    _validate_id(adapter_id, "adapter_id")
    service = runtime_services.resolve(EFFECT_ADAPTER_SERVICE_ID, EFFECT_ADAPTER_CONTRACT_VERSION)
    if service is None:
        raise LookupError("effect adapter resolver service is unavailable")
    resolve_adapter = getattr(service, "resolve_adapter", None)
    if not callable(resolve_adapter):
        raise TypeError("effect adapter resolver does not implement contract version 1")
    adapter = resolve_adapter(adapter_id)
    if adapter is None:
        raise LookupError(f"effect adapter is unavailable: {adapter_id}")
    if getattr(adapter, "adapter_id", None) != adapter_id:
        raise ValueError("resolved effect adapter identity mismatch")
    if not callable(getattr(adapter, "lookup_receipt", None)) or not callable(
        getattr(adapter, "perform", None)
    ):
        raise TypeError("effect adapter does not implement contract version 1")
    return cast(EffectAdapter, adapter)


class SQLiteEffectStore:
    """Durable intent, fencing claim, and receipt storage for effect adapters."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def ensure_intent(
        self,
        identity: OperationIdentity,
        policy: EffectPolicy | str,
        input_value: Any,
        *,
        allow_unsafe: bool = False,
        now: float | None = None,
    ) -> EffectRecord:
        policy_value = _coerce_policy(policy)
        if not isinstance(allow_unsafe, bool):
            raise TypeError("allow_unsafe must be a boolean")
        unsafe_authorized = policy_value is EffectPolicy.UNSAFE and allow_unsafe
        input_json = canonical_json(input_value)
        expected_identity = _operation_identity_from_input_json(
            workflow_id=identity.workflow_id,
            effect_key=identity.effect_key,
            adapter_id=identity.adapter_id,
            input_json=input_json,
        )
        if identity.input_hash != expected_identity.input_hash:
            raise ValueError("input conflicts with operation identity")
        if (
            identity.operation_id != expected_identity.operation_id
            or identity.workflow_id != expected_identity.workflow_id
            or identity.effect_key != expected_identity.effect_key
            or identity.adapter_id != expected_identity.adapter_id
        ):
            raise ValueError("operation identity mismatch")
        timestamp = _timestamp(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO effect_intents (
                    operation_id, workflow_id, effect_key, adapter_id, policy, unsafe_authorized,
                    input_hash, input_json, state, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    identity.operation_id,
                    identity.workflow_id,
                    identity.effect_key,
                    identity.adapter_id,
                    policy_value.value,
                    int(unsafe_authorized),
                    identity.input_hash,
                    input_json,
                    timestamp,
                    timestamp,
                ),
            )
            row = self._intent_row(conn, identity.operation_id)
            expected = (
                identity.workflow_id,
                identity.effect_key,
                identity.adapter_id,
                policy_value.value,
                int(unsafe_authorized),
                identity.input_hash,
                input_json,
            )
            actual = tuple(row[key] for key in expected_intent_columns())
            if actual != expected:
                raise ValueError("operation identity conflicts with durable intent")
            conn.commit()
        return self.get(identity.operation_id)

    def claim(
        self,
        operation_id: str,
        *,
        ttl_seconds: float = 30.0,
        now: float | None = None,
        token: str | None = None,
    ) -> EffectClaim:
        _validate_operation_id(operation_id)
        if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool):
            raise TypeError("ttl_seconds must be numeric")
        if not math.isfinite(float(ttl_seconds)) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and greater than zero")
        timestamp = _timestamp(now)
        claim_token = token or secrets.token_urlsafe(24)
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("claim token must be a nonempty string")
        expires_at = timestamp + float(ttl_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE effect_intents
                SET state = 'claimed', claim_token = ?, claim_expires_at = ?,
                    attempts = attempts + 1, updated_at = ?, error_json = NULL
                WHERE operation_id = ?
                  AND (
                    (state = 'pending' AND policy IN ('pure', 'idempotent', 'unsafe'))
                    OR (
                      state = 'claimed' AND claim_expires_at <= ?
                      AND policy IN ('pure', 'idempotent')
                    )
                  )
                """,
                (claim_token, expires_at, timestamp, operation_id, timestamp),
            )
            if cursor.rowcount != 1:
                if conn.execute(
                    "SELECT 1 FROM effect_intents WHERE operation_id = ?", (operation_id,)
                ).fetchone() is None:
                    raise KeyError(f"unknown operation_id: {operation_id}")
                raise RuntimeError("effect intent is not claimable")
            attempt = int(
                conn.execute(
                    "SELECT attempts FROM effect_intents WHERE operation_id = ?", (operation_id,)
                ).fetchone()[0]
            )
            conn.commit()
        return EffectClaim(operation_id, claim_token, attempt, expires_at)

    def complete(
        self,
        operation_id: str,
        claim_token: str,
        receipt_payload: Mapping[str, Any],
        *,
        adapter_receipt_id: str | None = None,
        sensitive: bool = False,
        now: float | None = None,
    ) -> EffectRecord:
        _validate_operation_id(operation_id)
        payload_json = canonical_json(receipt_payload)
        timestamp = _timestamp(now)
        receipt_hash = _sha256(payload_json)
        receipt_id = adapter_receipt_id or receipt_hash
        if not isinstance(receipt_id, str) or not receipt_id:
            raise ValueError("adapter_receipt_id must be a nonempty string")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE effect_intents
                SET state = 'completed', updated_at = ?
                WHERE operation_id = ? AND state = 'claimed' AND claim_token = ?
                  AND claim_expires_at > ?
                """,
                (timestamp, operation_id, claim_token, timestamp),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale claim token cannot complete effect")
            conn.execute(
                """
                INSERT INTO effect_receipts (
                    operation_id, adapter_receipt_id, receipt_json, receipt_hash,
                    sensitive, completed_at, claim_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    receipt_id,
                    payload_json,
                    receipt_hash,
                    int(bool(sensitive)),
                    timestamp,
                    claim_token,
                ),
            )
            conn.commit()
        return self.get(operation_id)

    def fail(
        self,
        operation_id: str,
        claim_token: str,
        error: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> EffectRecord:
        _validate_operation_id(operation_id)
        error_json = canonical_json(error)
        timestamp = _timestamp(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE effect_intents
                SET state = 'failed', error_json = ?, updated_at = ?
                WHERE operation_id = ? AND state = 'claimed' AND claim_token = ?
                  AND claim_expires_at > ?
                """,
                (error_json, timestamp, operation_id, claim_token, timestamp),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("stale claim token cannot fail effect")
            conn.commit()
        return self.get(operation_id)

    def get(self, operation_id: str) -> EffectRecord:
        _validate_operation_id(operation_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM effect_intents WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown operation_id: {operation_id}")
            receipt_row = conn.execute(
                "SELECT * FROM effect_receipts WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return _record_from_rows(row, receipt_row)

    def lookup_receipt(self, operation_id: str) -> EffectReceipt | None:
        return self.get(operation_id).receipt

    def require_active_claim(
        self,
        operation_id: str,
        claim_token: str,
        *,
        now: float | None = None,
    ) -> EffectRecord:
        """Return the durable intent only when the token currently owns its live claim."""
        _validate_operation_id(operation_id)
        timestamp = _timestamp(now)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM effect_intents
                WHERE operation_id = ? AND state = 'claimed' AND claim_token = ?
                  AND claim_expires_at > ?
                """,
                (operation_id, claim_token, timestamp),
            ).fetchone()
        if row is None:
            raise RuntimeError("stale claim token cannot execute effect")
        return _record_from_rows(row, None)

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS effect_intents (
                    operation_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    effect_key TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    policy TEXT NOT NULL CHECK (policy IN ('pure','idempotent','unsafe','unclassified')),
                    unsafe_authorized INTEGER NOT NULL DEFAULT 0 CHECK (unsafe_authorized IN (0,1)),
                    input_hash TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending','claimed','completed','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    claim_token TEXT,
                    claim_expires_at REAL,
                    error_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK (
                        (state = 'pending' AND claim_token IS NULL AND claim_expires_at IS NULL)
                        OR (state IN ('claimed','completed','failed') AND claim_token IS NOT NULL)
                    )
                );
                CREATE TABLE IF NOT EXISTS effect_receipts (
                    operation_id TEXT PRIMARY KEY REFERENCES effect_intents(operation_id),
                    adapter_receipt_id TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL,
                    sensitive INTEGER NOT NULL CHECK (sensitive IN (0,1)),
                    completed_at REAL NOT NULL,
                    claim_token TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS effect_intents_claimable
                    ON effect_intents(state, claim_expires_at);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _intent_row(conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM effect_intents WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown operation_id: {operation_id}")
        return row


class EffectCoordinator:
    """Explicit one-attempt coordinator; callers own retry policy and scheduling."""

    def __init__(self, store: SQLiteEffectStore):
        self.store = store

    def prepare(
        self,
        *,
        workflow_id: str,
        effect_key: str,
        adapter_id: str,
        input_value: Any,
        policy: EffectPolicy | str,
        allow_unsafe: bool = False,
    ) -> EffectRecord:
        policy_value = _coerce_policy(policy)
        if policy_value is EffectPolicy.UNCLASSIFIED:
            raise ValueError("unclassified or legacy effects refuse execution")
        if policy_value is EffectPolicy.UNSAFE and not allow_unsafe:
            raise ValueError("unsafe effects require explicit authorization")
        identity = operation_identity(
            workflow_id=workflow_id,
            effect_key=effect_key,
            adapter_id=adapter_id,
            input_value=input_value,
        )
        return self.store.ensure_intent(
            identity, policy_value, input_value, allow_unsafe=allow_unsafe
        )

    def execute_claimed(
        self,
        record: EffectRecord,
        claim: EffectClaim,
        adapter: EffectAdapter,
        input_value: Any,
        *,
        sensitive_receipt: bool = False,
    ) -> EffectRecord:
        if claim.operation_id != record.identity.operation_id:
            raise ValueError("effect claim operation_id mismatch")
        durable_record = self.store.require_active_claim(claim.operation_id, claim.token)
        if durable_record.identity != record.identity:
            raise ValueError("effect record conflicts with durable intent")
        if durable_record.policy is EffectPolicy.UNSAFE and not durable_record.unsafe_authorized:
            raise ValueError("unsafe effects require explicit authorization")
        if durable_record.identity.adapter_id != adapter.adapter_id:
            raise ValueError("effect adapter identity mismatch")
        if _sha256(canonical_json(input_value)) != durable_record.identity.input_hash:
            raise ValueError("effect input conflicts with durable intent")
        operation_id = durable_record.identity.operation_id
        existing = adapter.lookup_receipt(operation_id)
        if existing is not None:
            payload = existing
        else:
            current_record = self.store.require_active_claim(operation_id, claim.token)
            if current_record.identity != durable_record.identity:
                raise ValueError("effect record conflicts with durable intent")
            payload = adapter.perform(operation_id, input_value)
        if not isinstance(payload, Mapping):
            raise TypeError("effect adapter receipt must be a mapping")
        if "operation_id" in payload and payload["operation_id"] != operation_id:
            raise ValueError("effect adapter receipt operation_id mismatch")
        adapter_receipt_id = payload.get("adapter_receipt_id")
        return self.store.complete(
            operation_id,
            claim.token,
            payload,
            adapter_receipt_id=str(adapter_receipt_id) if adapter_receipt_id is not None else None,
            sensitive=sensitive_receipt,
        )


def expected_intent_columns() -> tuple[str, ...]:
    return (
        "workflow_id",
        "effect_key",
        "adapter_id",
        "policy",
        "unsafe_authorized",
        "input_hash",
        "input_json",
    )


def _record_from_rows(row: sqlite3.Row, receipt_row: sqlite3.Row | None) -> EffectRecord:
    state = str(row["state"])
    if state not in _STATE_VALUES:
        raise ValueError(f"invalid durable effect state: {state}")
    identity = OperationIdentity(
        operation_id=str(row["operation_id"]),
        workflow_id=str(row["workflow_id"]),
        effect_key=str(row["effect_key"]),
        adapter_id=str(row["adapter_id"]),
        input_hash=str(row["input_hash"]),
    )
    receipt = None
    if receipt_row is not None:
        payload = json.loads(str(receipt_row["receipt_json"]))
        receipt = EffectReceipt(
            operation_id=identity.operation_id,
            adapter_receipt_id=str(receipt_row["adapter_receipt_id"]),
            payload=MappingProxyType(payload),
            receipt_hash=str(receipt_row["receipt_hash"]),
            sensitive=bool(receipt_row["sensitive"]),
            completed_at=float(receipt_row["completed_at"]),
            claim_token=str(receipt_row["claim_token"]),
        )
    error_value = json.loads(str(row["error_json"])) if row["error_json"] is not None else None
    return EffectRecord(
        identity=identity,
        policy=EffectPolicy(str(row["policy"])),
        unsafe_authorized=bool(row["unsafe_authorized"]),
        state=state,
        attempts=int(row["attempts"]),
        claim_token=str(row["claim_token"]) if row["claim_token"] is not None else None,
        claim_expires_at=(
            float(row["claim_expires_at"]) if row["claim_expires_at"] is not None else None
        ),
        receipt=receipt,
        error=MappingProxyType(error_value) if error_value is not None else None,
    )


def _normalize_json(value: Any) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_json(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = _normalize_json(item)
        return result
    raise TypeError(f"value is not canonical JSON: {type(value).__name__}")


def _coerce_policy(value: EffectPolicy | str) -> EffectPolicy:
    try:
        return value if isinstance(value, EffectPolicy) else EffectPolicy(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("effect policy must be pure, idempotent, unsafe, or unclassified") from exc


def _validate_id(value: Any, label: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a nonempty canonical identifier")


def _validate_operation_id(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"op_[0-9a-f]{64}", value) is None:
        raise ValueError("operation_id must be an op_ prefixed SHA-256")


def _timestamp(value: float | None) -> float:
    timestamp = time.time() if value is None else value
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise TypeError("timestamp must be numeric")
    if not math.isfinite(float(timestamp)):
        raise ValueError("timestamp must be finite")
    return float(timestamp)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

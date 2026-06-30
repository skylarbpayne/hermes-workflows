from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, get_type_hints
from urllib.parse import quote

from .approvals import ApprovalDecision, ApprovalDecisionInput, ApprovalReceipt, ApprovalView, OperatorResponseReceipt
from .domain import CommandType, WorkflowStatus, decode_command_row, decode_event_row, make_command, make_event
from .input_parsing import coerce_workflow_input
from .status_projection import StatusProjection
from .types import to_json_value
from .workflow_values import Workflow


TERMINAL_WORKFLOW_STATUSES = WorkflowStatus.terminal_values()


class _ClosingSqliteConnection(sqlite3.Connection):
    """sqlite3 connection that actually closes when used as a context manager.

    Python's built-in sqlite3.Connection context manager commits/rolls back on
    exit, but it does not close the underlying file descriptor. The workflow
    engine consistently uses `with self._connect() as con:`, and resident
    workers call that path on every poll/heartbeat. If connection objects are
    retained longer than expected, the process leaks SQLite file handles until
    it hits macOS's low default fd limit.
    """

    def __exit__(self, exc_type, exc, traceback):  # type: ignore[override]
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class WorkflowWaiting(Exception):
    def __init__(self, waiting_on: str):
        super().__init__(waiting_on)
        self.waiting_on = waiting_on


class WorkflowCancelled(Exception):
    """Internal control-flow signal: stop decider work after cancellation."""


class CommandClaimLost(Exception):
    """Internal signal: a worker-owned command lost its lease/fence."""


@dataclass(frozen=True)
class ActiveCommandClaim:
    workflow_id: str
    command: Dict[str, Any]


@dataclass(frozen=True)
class PendingStep:
    key: str


@dataclass(frozen=True)
class RunResult:
    workflow_id: str
    status: str
    waiting_on: Optional[str] = None
    result: Any = None
    error: Optional[str] = None


@dataclass(frozen=True)
class StepOutput:
    """Step body return wrapper for durable metadata that is not user output."""

    output: Any
    metadata: Optional[Dict[str, Any]] = None


class JsonCodec:
    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(_to_jsonable(value), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def loads(value: Optional[str]) -> Any:
        if value is None or value == "":
            return None
        return _from_jsonable(json.loads(value))


class WorkflowEngine:
    def __init__(
        self,
        db_path: Union[Path, str],
        *,
        agent_runner: Optional[Callable[[Dict[str, Any]], Any]] = None,
        read_only: bool = False,
    ):
        self.db_path = Path(db_path)
        self.agent_runner = agent_runner
        self.read_only = read_only
        self._status_projection = StatusProjection(self)
        self._active_command_claim = threading.local()
        if read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(f"workflow DB does not exist: {self.db_path}")
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def _ensure_writable(self, operation: str) -> None:
        if self.read_only:
            raise RuntimeError(f"WorkflowEngine is read-only; cannot {operation}")

    def start(
        self,
        workflow_fn: Callable[..., Any],
        inputs: Any,
        *,
        workflow_id: str,
        workflow_ref: str | None = None,
    ) -> RunResult:
        self._ensure_writable("start workflows")
        workflow_name = getattr(workflow_fn, "__workflow_name__", workflow_fn.__name__)
        with self._connect() as con:
            existing = con.execute("SELECT id FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if existing is None:
                input_sanitizer = getattr(workflow_fn, "__workflow_input_sanitizer__", None)
                if callable(input_sanitizer):
                    sanitizer_signature = inspect.signature(input_sanitizer)
                    if "workflow_id" in sanitizer_signature.parameters:
                        inputs = input_sanitizer(inputs, workflow_id=workflow_id)
                    else:
                        inputs = input_sanitizer(inputs)
                now = _now()
                con.execute(
                    """
                    INSERT INTO workflow_instances(id, workflow_name, workflow_ref, status, input_json, created_at, updated_at)
                    VALUES (?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (workflow_id, workflow_name, workflow_ref, JsonCodec.dumps(inputs), now, now),
                )
                self._append_event(
                    con,
                    workflow_id,
                    "WorkflowStarted",
                    key="workflow:start",
                    payload={"workflow_name": workflow_name, "workflow_ref": workflow_ref, "input": inputs},
                    idempotency_key="workflow:start",
                )
            elif workflow_ref:
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET workflow_ref = COALESCE(workflow_ref, ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (workflow_ref, _now(), workflow_id),
                )
            self._enqueue_workflow_run_row(con, workflow_id, reason="start")
        return self._result_from_instance(workflow_id)

    def run_until_idle(
        self,
        workflow_fn: Callable[..., Any],
        inputs: Any,
        *,
        workflow_id: str,
        workflow_ref: str | None = None,
    ) -> RunResult:
        """Start a workflow and execute local run_step commands until blocked.

        This is the first practical test-drive runner: it proves real step bodies
        can run out-of-band while the workflow decider still exits cleanly at
        durable waits.
        """

        self._ensure_writable("run workflows")
        result = self.start(workflow_fn, inputs, workflow_id=workflow_id, workflow_ref=workflow_ref)
        return self.drain(workflow_id, initial=result)

    def drain(self, workflow_id: str, *, initial: Optional[RunResult] = None) -> RunResult:
        """Execute pending local run_step commands until no runnable command remains."""

        self._ensure_writable("drain workflow commands")
        result = initial or self._result_from_instance(workflow_id)
        while True:
            command = self.claim_command(workflow_id, worker_id="local-drain", lease_seconds=30, command_type=None)
            if command is None:
                return self._result_from_instance(workflow_id) if result is None else self._result_from_instance(workflow_id)
            result = self._execute_command(workflow_id, command)
            if result.status in {"failed", "completed"}:
                # There might still be historical pending commands from a corrupt
                # test DB, but v0 stops on terminal status.
                return result

    def complete_step(
        self,
        workflow_id: str,
        step_key: str,
        output: Any,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RunResult:
        self._ensure_writable("complete workflow steps")
        instance = self._instance(workflow_id)
        if instance["status"] in TERMINAL_WORKFLOW_STATUSES:
            return self._result_from_instance(workflow_id)

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if row["status"] in TERMINAL_WORKFLOW_STATUSES:
                return self._result_from_row(row)

            payload = {"output": output}
            if metadata is not None:
                payload["metadata"] = metadata
            self._append_event(
                con,
                workflow_id,
                "StepCompleted",
                key=step_key,
                payload=payload,
                idempotency_key=f"completed:{step_key}",
                ignore_duplicate=True,
            )
            con.execute(
                """
                UPDATE workflow_instances
                SET status = 'running', waiting_on = NULL, updated_at = ?
                WHERE id = ? AND status != 'cancelled'
                """,
                (_now(), workflow_id),
            )
            self._enqueue_workflow_run_row(con, workflow_id, reason="step_completed", source_key=step_key)
            con.execute(
                """
                UPDATE workflow_commands_outbox
                SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE workflow_id = ? AND key = ? AND type = 'run_step' AND status != 'cancelled'
                """,
                (_now(), workflow_id, step_key),
            )
        return self._result_from_instance(workflow_id)

    def _approval_request_kind(self, workflow_id: str, key: str) -> str | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT payload_json
                FROM workflow_events
                WHERE workflow_id = ? AND type = 'ApprovalRequested' AND key = ?
                ORDER BY seq DESC LIMIT 1
                """,
                (workflow_id, f"approval:{key}"),
            ).fetchone()
        if row is None:
            return None
        payload = JsonCodec.loads(row["payload_json"])
        if not isinstance(payload, dict):
            return None
        return str(payload.get("kind")) if payload.get("kind") is not None else None

    def signal(
        self,
        workflow_id: str,
        signal_type: str,
        *,
        key: str,
        payload: Any,
        source: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> RunResult:
        self._ensure_writable("record workflow signals")
        instance = self._instance(workflow_id)
        if signal_type in {"approval.decision", "operator.response"}:
            if signal_type == "approval.decision":
                payload = _normalize_approval_decision_payload(payload)
                source = _normalize_operator_source(source)
            else:
                source = _normalize_operator_source(source)
        dedupe = idempotency_key or f"signal:{signal_type}:{key}:{JsonCodec.dumps(payload)}"
        if instance["status"] in TERMINAL_WORKFLOW_STATUSES and signal_type not in {"approval.decision", "operator.response"}:
            return self._result_from_instance(workflow_id)

        inserted = False
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if row["status"] in TERMINAL_WORKFLOW_STATUSES:
                if signal_type in {"approval.decision", "operator.response"}:
                    self._validate_operator_response_signal(
                        workflow_id,
                        key,
                        payload,
                        source,
                        dedupe,
                        signal_type=signal_type,
                        con=con,
                        require_existing=row["status"] == "completed",
                    )
                return self._result_from_row(row)
            if signal_type in {"approval.decision", "operator.response"}:
                self._validate_operator_response_signal(workflow_id, key, payload, source, dedupe, signal_type=signal_type, con=con)

            inserted = self._append_event(
                con,
                workflow_id,
                "SignalReceived",
                key=f"signal:{signal_type}:{key}",
                payload={"signal_type": signal_type, "key": key, "payload": payload, "source": source},
                idempotency_key=dedupe,
                ignore_duplicate=True,
            )
            if inserted:
                self._append_signal_step_completed(
                    con,
                    workflow_id,
                    signal_type=signal_type,
                    key=key,
                    output=payload,
                    source=source,
                    idempotency_key=dedupe,
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'running', updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (_now(), workflow_id),
                )
                self._enqueue_workflow_run_row(con, workflow_id, reason=f"signal:{signal_type}", source_key=key)
                if signal_type in {"approval.decision", "operator.response"}:
                    con.execute(
                        """
                        UPDATE workflow_commands_outbox
                        SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                        WHERE workflow_id = ? AND type = 'notify_approval' AND key = ? AND status != 'cancelled'
                        """,
                        (_now(), workflow_id, f"approval:{key}"),
                    )
                elif signal_type == "agent.completed":
                    con.execute(
                        """
                        UPDATE workflow_commands_outbox
                        SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                        WHERE workflow_id = ? AND type = 'external_agent' AND key = ? AND status != 'cancelled'
                        """,
                        (_now(), workflow_id, f"agent:{key}"),
                    )
        if inserted:
            result = self._result_from_instance(workflow_id)
        else:
            result = self._result_from_instance(workflow_id)
        return result

    def resume(self, workflow_fn: Callable[..., Any], workflow_id: str) -> RunResult:
        """Resume a workflow decider without recording a new external event."""

        self._ensure_writable("resume workflows")
        instance = self._instance(workflow_id)
        if instance["status"] in TERMINAL_WORKFLOW_STATUSES:
            return self._result_from_row(instance)
        result = self._run_decider(workflow_id, workflow_fn)
        return self.drain(workflow_id, initial=result)

    def list_approvals(self, *, status: str | None = "waiting") -> list[ApprovalView]:
        """Return approval-card views for plugins, dashboards, CLIs, and chat adapters."""

        with self._connect() as con:
            if status == "waiting":
                rows = con.execute(
                    """
                    SELECT id, workflow_name, workflow_ref, status, waiting_on
                    FROM workflow_instances
                    WHERE status NOT IN ('completed', 'failed', 'cancelled')
                    ORDER BY updated_at DESC, created_at DESC, id ASC
                    """
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT id, workflow_name, workflow_ref, status, waiting_on
                    FROM workflow_instances
                    ORDER BY updated_at DESC, created_at DESC, id ASC
                    """
                ).fetchall()
        approvals: list[ApprovalView] = []
        for row in rows:
            workflow_approvals = self._approval_views_for_workflow(row)
            if status is not None:
                workflow_approvals = [approval for approval in workflow_approvals if approval.status == status]
            approvals.extend(workflow_approvals)
        return approvals

    def list_operator_steps(self, *, status: str | None = "waiting") -> list[dict[str, Any]]:
        """Return typed human-input steps across workflows.

        Approval remains as a compatibility/policy preset. Typed human input
        and future human checkpoints should appear here, not as approval cards.
        """

        with self._connect() as con:
            if status == "waiting":
                rows = con.execute(
                    """
                    SELECT id, workflow_name, workflow_ref, status, waiting_on
                    FROM workflow_instances
                    WHERE status NOT IN ('completed', 'failed', 'cancelled')
                    ORDER BY updated_at DESC, created_at DESC, id ASC
                    """
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT id, workflow_name, workflow_ref, status, waiting_on
                    FROM workflow_instances
                    ORDER BY updated_at DESC, created_at DESC, id ASC
                    """
                ).fetchall()
        operator_steps: list[dict[str, Any]] = []
        for row in rows:
            for step in self._operator_step_summaries(self.events(row["id"])):
                if status is not None and step.get("status") != status:
                    continue
                operator_steps.append(
                    {
                        "db_path": str(self.db_path),
                        "workflow_id": row["id"],
                        "workflow_name": row["workflow_name"],
                        "workflow_ref": row["workflow_ref"],
                        "waiting_on": row["waiting_on"],
                        **step,
                    }
                )
        return operator_steps

    def get_approval(self, workflow_id: str, key: str) -> ApprovalView:
        row = self._instance(workflow_id)
        for approval in self._approval_views_for_workflow(row):
            if approval.key == key:
                return approval
        raise KeyError(f"unknown approval {key} for workflow_id: {workflow_id}")

    def submit_approval_decision(
        self,
        decision: ApprovalDecisionInput,
        *,
        resume: bool = True,
    ) -> ApprovalReceipt:
        """Validate and record a human approval decision through the canonical signal path."""

        self._ensure_writable("submit approval decisions")
        sanitized_source = _sanitize_approval_source(decision.source)
        payload: dict[str, Any] = {"action": decision.action}
        if decision.by:
            payload["by"] = decision.by
        if decision.note is not None:
            payload["note"] = _sanitize_approval_text(decision.note)
        if decision.reason is not None:
            payload["reason"] = _sanitize_approval_text(decision.reason)
        dedupe = decision.idempotency_key or (
            f"approval:{decision.workflow_id}:{decision.key}:{decision.action}:"
            f"{sanitized_source.get('message_url') or sanitized_source.get('message_id') or sanitized_source.get('event_id')}"
        )

        if resume:
            result = self.signal(
                decision.workflow_id,
                "approval.decision",
                key=decision.key,
                payload=payload,
                source=sanitized_source,
                idempotency_key=dedupe,
            )
            with self._connect() as con:
                row = con.execute("SELECT workflow_ref FROM workflow_instances WHERE id = ?", (decision.workflow_id,)).fetchone()
                workflow_ref = row["workflow_ref"] if row is not None else None
            return ApprovalReceipt(
                workflow_id=decision.workflow_id,
                key=decision.key,
                action=decision.action,
                by=decision.by,
                source=sanitized_source,
                status=result.status,
                waiting_on=result.waiting_on,
                result_summary=result.result if isinstance(result.result, dict) else None,
                workflow_ref=workflow_ref,
            )

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (decision.workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {decision.workflow_id}")
            if row["status"] in TERMINAL_WORKFLOW_STATUSES:
                self._validate_approval_decision_signal(
                    decision.workflow_id,
                    decision.key,
                    payload,
                    sanitized_source,
                    dedupe,
                    con=con,
                    require_existing=row["status"] == "completed",
                )
                result = self._result_from_row(row)
                return ApprovalReceipt(
                    workflow_id=decision.workflow_id,
                    key=decision.key,
                    action=decision.action,
                    by=decision.by,
                    source=sanitized_source,
                    status=result.status,
                    waiting_on=result.waiting_on,
                    result_summary=result.result if isinstance(result.result, dict) else None,
                    workflow_ref=row["workflow_ref"],
                )
            self._validate_approval_decision_signal(decision.workflow_id, decision.key, payload, sanitized_source, dedupe, con=con)
            inserted = self._append_event(
                con,
                decision.workflow_id,
                "SignalReceived",
                key=f"signal:approval.decision:{decision.key}",
                payload={"signal_type": "approval.decision", "key": decision.key, "payload": payload, "source": sanitized_source},
                idempotency_key=dedupe,
                ignore_duplicate=True,
            )
            if inserted:
                self._append_signal_step_completed(
                    con,
                    decision.workflow_id,
                    signal_type="approval.decision",
                    key=decision.key,
                    output=payload,
                    source=sanitized_source,
                    idempotency_key=dedupe,
                )
                con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE workflow_id = ? AND type = 'notify_approval' AND key = ? AND status != 'cancelled'
                    """,
                    (_now(), decision.workflow_id, f"approval:{decision.key}"),
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'running', updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (_now(), decision.workflow_id),
                )
                self._enqueue_workflow_run_row(
                    con,
                    decision.workflow_id,
                    reason="approval_decision",
                    source_key=decision.key,
                )
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (decision.workflow_id,)).fetchone()

        return ApprovalReceipt(
            workflow_id=decision.workflow_id,
            key=decision.key,
            action=decision.action,
            by=decision.by,
            source=sanitized_source,
            status="decision_recorded",
            waiting_on=row["waiting_on"],
            result_summary=None,
            workflow_ref=row["workflow_ref"],
        )

    def submit_operator_response(
        self,
        *,
        workflow_id: str,
        key: str,
        payload: Dict[str, Any],
        source: Dict[str, Any],
        idempotency_key: str | None = None,
        resume: bool = True,
    ) -> OperatorResponseReceipt:
        """Record a general human/operator response.

        This is the neutral substrate for ask(...). Approval decisions are a
        preset wrapper over the same operator-step lifecycle, not the base
        concept.
        """

        normalized_source = _normalize_operator_source(source)
        if resume:
            result = self.signal(
                workflow_id,
                "operator.response",
                key=key,
                payload=payload,
                source=normalized_source,
                idempotency_key=idempotency_key,
            )
            with self._connect() as con:
                row = con.execute("SELECT workflow_ref FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                workflow_ref = row["workflow_ref"] if row is not None else None
            return OperatorResponseReceipt(
                workflow_id=workflow_id,
                key=key,
                action=str(payload.get("action") or "answered"),
                by=str(payload.get("by") or normalized_source.get("id") or "operator"),
                source=normalized_source,
                status=result.status,
                waiting_on=result.waiting_on,
                result_summary=result.result if isinstance(result.result, dict) else None,
                workflow_ref=workflow_ref,
            )

        dedupe = idempotency_key or f"operator:{workflow_id}:{key}:{JsonCodec.dumps(payload)}"
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            self._validate_operator_response_signal(workflow_id, key, payload, normalized_source, dedupe, signal_type="operator.response", con=con)
            inserted = self._append_event(
                con,
                workflow_id,
                "SignalReceived",
                key=f"signal:operator.response:{key}",
                payload={"signal_type": "operator.response", "key": key, "payload": payload, "source": normalized_source},
                idempotency_key=dedupe,
                ignore_duplicate=True,
            )
            if inserted:
                self._append_signal_step_completed(
                    con,
                    workflow_id,
                    signal_type="operator.response",
                    key=key,
                    output=payload,
                    source=normalized_source,
                    idempotency_key=dedupe,
                )
                con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE workflow_id = ? AND type = 'notify_approval' AND key = ? AND status != 'cancelled'
                    """,
                    (_now(), workflow_id, f"approval:{key}"),
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'running', updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (_now(), workflow_id),
                )
                self._enqueue_workflow_run_row(
                    con,
                    workflow_id,
                    reason="operator_response",
                    source_key=key,
                )
            row = con.execute("SELECT workflow_ref, waiting_on FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            workflow_ref = row["workflow_ref"] if row is not None else None
            waiting_on = row["waiting_on"] if row is not None else None
        return OperatorResponseReceipt(
            workflow_id=workflow_id,
            key=key,
            action=str(payload.get("action") or "answered"),
            by=str(payload.get("by") or normalized_source.get("id") or "operator"),
            source=normalized_source,
            status="response_recorded",
            waiting_on=waiting_on,
            result_summary=None,
            workflow_ref=workflow_ref,
        )

    def _approval_views_for_workflow(self, row: sqlite3.Row) -> list[ApprovalView]:
        events = self.events(row["id"])
        summaries = self._approval_summaries(events)
        active_commands = self._active_commands(row["id"])
        diagnostics_by_approval_key: dict[str, list[dict[str, Any]]] = {}
        for diagnostic in self._command_diagnostics(active_commands):
            command_key = str(diagnostic.get("command_key") or "")
            if command_key.startswith("approval:"):
                approval_key = command_key.split(":", 1)[1]
                diagnostics_by_approval_key.setdefault(approval_key, []).append(diagnostic)

        views: list[ApprovalView] = []
        for summary in summaries:
            key = str(summary.get("key") or "")
            views.append(
                ApprovalView(
                    db_path=str(self.db_path),
                    workflow_id=row["id"],
                    workflow_name=row["workflow_name"],
                    workflow_ref=row["workflow_ref"],
                    key=key,
                    status=str(summary.get("status") or "waiting"),
                    prompt=summary.get("prompt"),
                    artifact=summary.get("artifact"),
                    schema=summary.get("schema"),
                    allowed=list(summary.get("allowed") or []),
                    timeout=summary.get("timeout"),
                    waiting_on=row["waiting_on"],
                    requested_seq=summary.get("requested_seq"),
                    source=summary.get("source"),
                    decision=summary.get("decision"),
                    diagnostics=diagnostics_by_approval_key.get(key, []),
                )
            )
        return views

    def _validate_operator_response_signal(
        self,
        workflow_id: str,
        key: str,
        payload: Any,
        source: Any,
        idempotency_key: str,
        *,
        signal_type: str,
        con: sqlite3.Connection | None = None,
        require_existing: bool = False,
    ) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"operator step {key} response payload must be an object")

        event_key = f"approval:{key}"
        if con is None:
            with self._connect() as read_con:
                self._validate_operator_response_signal(
                    workflow_id,
                    key,
                    payload,
                    source,
                    idempotency_key,
                    signal_type=signal_type,
                    con=read_con,
                    require_existing=require_existing,
                )
            return

        row = con.execute(
            """
            SELECT payload_json
            FROM workflow_events
            WHERE workflow_id = ? AND type = 'ApprovalRequested' AND key = ?
            ORDER BY seq DESC LIMIT 1
            """,
            (workflow_id, event_key),
        ).fetchone()
        existing_decision = con.execute(
            """
            SELECT payload_json, idempotency_key
            FROM workflow_events
            WHERE workflow_id = ? AND type = 'SignalReceived' AND key = ?
            ORDER BY seq DESC LIMIT 1
            """,
            (workflow_id, f"signal:{signal_type}:{key}"),
        ).fetchone()

        if existing_decision is not None:
            if existing_decision["idempotency_key"] == idempotency_key:
                existing_payload = JsonCodec.loads(existing_decision["payload_json"])
                expected_payload = {"signal_type": signal_type, "key": key, "payload": payload, "source": source}
                if existing_payload == expected_payload:
                    return
                raise ValueError(f"operator step {key} idempotency key was reused with a different decision/response")
            raise ValueError(f"operator step {key} already has a recorded decision/response")

        if require_existing:
            raise ValueError(f"operator step {key} has no recorded response to replay")

        if row is None:
            raise ValueError(f"operator step {key} has no matching ApprovalRequested event/request")

        request_payload = JsonCodec.loads(row["payload_json"])
        if not isinstance(request_payload, dict):
            raise ValueError(f"operator step {key} has invalid ApprovalRequested/request payload")

        is_human_input = request_payload.get("kind") in {"human_input.request.v1", "operator.request.v1"}
        allowed = request_payload.get("allowed") or ["approve", "reject"]
        if not is_human_input and payload.get("action") not in allowed:
            raise ValueError(f"operator step {key} action is not allowed: {payload.get('action')}")

        _validate_operator_source(key, payload, source)

    def _validate_approval_decision_signal(self, workflow_id: str, key: str, payload: Any, source: Any, idempotency_key: str, **kwargs: Any) -> None:
        self._validate_operator_response_signal(
            workflow_id,
            key,
            payload,
            source,
            idempotency_key,
            signal_type="approval.decision",
            **kwargs,
        )

    def cancel_workflow(
        self,
        workflow_id: str,
        *,
        reason: str,
        source: Optional[Dict[str, Any]] = None,
        superseded_by: Optional[str] = None,
    ) -> RunResult:
        """Mark a workflow terminal-cancelled while preserving an audit event."""

        self._ensure_writable("cancel workflows")

        payload = {
            "type": "cancelled",
            "reason": reason,
            "source": source,
            "superseded_by": superseded_by,
        }
        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if row["status"] in {"completed", "failed", "cancelled"}:
                full_row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                return self._result_from_row(full_row)

            self._append_event(
                con,
                workflow_id,
                "WorkflowCancelled",
                key="workflow:cancelled",
                payload=payload,
                idempotency_key="workflow:cancelled",
                ignore_duplicate=True,
            )
            con.execute(
                """
                UPDATE workflow_instances
                SET status = 'cancelled', waiting_on = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, workflow_id),
            )
            con.execute(
                """
                UPDATE workflow_commands_outbox
                SET status = 'cancelled', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE workflow_id = ? AND status IN ('pending', 'running')
                """,
                (now, workflow_id),
            )
        return self._result_from_instance(workflow_id)

    def pending_commands(self, workflow_id: str) -> List[Dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT type, key, payload_json
                FROM workflow_commands_outbox
                WHERE workflow_id = ?
                ORDER BY id ASC
                """,
                (workflow_id,),
            ).fetchall()
        return [
            {"type": row["type"], "key": row["key"], "payload": JsonCodec.loads(row["payload_json"])}
            for row in rows
        ]

    def claim_command(
        self,
        workflow_id: str,
        *,
        worker_id: str,
        worker_instance_id: str | None = None,
        lease_seconds: int = 30,
        command_type: Optional[str] = "run_step",
        include_external_agent: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Claim one pending or lease-expired command for a worker."""

        self._ensure_writable("claim workflow commands")

        now = _now()
        if command_type is not None:
            type_clause = "AND c.type = ?"
        else:
            runnable_types = CommandType.worker_runnable_values(include_external_agent=include_external_agent)
            quoted_types = ", ".join(f"'{command_type}'" for command_type in runnable_types)
            type_clause = f"AND c.type IN ({quoted_types})"
        params: list[Any] = [workflow_id]
        if command_type is not None:
            params.append(command_type)
        params.append(now)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                f"""
                SELECT c.*
                FROM workflow_commands_outbox c
                JOIN workflow_instances wi ON wi.id = c.workflow_id
                WHERE c.workflow_id = ?
                  {type_clause}
                  AND wi.status NOT IN ('completed', 'failed', 'cancelled')
                  AND (
                    c.status = 'pending'
                    OR (c.status = 'running' AND COALESCE(c.lease_expires_at, 0) <= ?)
                  )
                ORDER BY c.id ASC LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None

            attempts = int(row["attempts"] or 0) + 1
            claim_token = secrets.token_urlsafe(32)
            lease_expires_at = now + lease_seconds
            con.execute(
                """
                UPDATE workflow_commands_outbox
                SET status = 'running', claimed_by = ?, claimed_by_instance_id = ?,
                    claim_token = ?, lease_expires_at = ?, attempts = ?, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, worker_instance_id, claim_token, lease_expires_at, attempts, now, row["id"]),
            )
            self._append_event(
                con,
                workflow_id,
                "CommandClaimed",
                key=row["key"],
                payload={
                    "command_id": row["id"],
                    "command_type": row["type"],
                    "worker_id": worker_id,
                    "worker_instance_id": worker_instance_id,
                    "claim_token_hash": _claim_token_hash(claim_token),
                    "attempt": attempts,
                    "lease_expires_at": lease_expires_at,
                },
                idempotency_key=f"claimed:{row['id']}:{attempts}",
                ignore_duplicate=True,
            )
            claimed = con.execute("SELECT * FROM workflow_commands_outbox WHERE id = ?", (row["id"],)).fetchone()

        return self._command_payload(claimed, include_claim_token=True)

    def _claim_guard_params(self, workflow_id: str, command: Union[sqlite3.Row, Dict[str, Any]], *, now: int) -> tuple[Any, ...] | None:
        claim_token = _command_value(command, "claim_token")
        if not claim_token:
            return None
        worker_instance_id = _command_value(command, "claimed_by_instance_id")
        return (
            _command_value(command, "id"),
            workflow_id,
            _command_value(command, "type"),
            _command_value(command, "claimed_by"),
            worker_instance_id,
            worker_instance_id,
            claim_token,
            _command_value(command, "attempts"),
            now,
        )

    def _command_claim_is_live(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        command: Union[sqlite3.Row, Dict[str, Any]],
        *,
        now: int | None = None,
    ) -> bool:
        now = _now() if now is None else now
        params = self._claim_guard_params(workflow_id, command, now=now)
        if params is None:
            return False
        row = con.execute(
            """
            SELECT c.id
            FROM workflow_commands_outbox c
            JOIN workflow_instances wi ON wi.id = c.workflow_id
            WHERE c.id = ?
              AND c.workflow_id = ?
              AND c.type = ?
              AND c.status = 'running'
              AND c.claimed_by = ?
              AND ((c.claimed_by_instance_id IS NULL AND ? IS NULL) OR c.claimed_by_instance_id = ?)
              AND c.claim_token = ?
              AND c.attempts = ?
              AND COALESCE(c.lease_expires_at, 0) > ?
              AND wi.status NOT IN ('completed', 'failed', 'cancelled')
            """,
            params,
        ).fetchone()
        return row is not None

    @contextmanager
    def _command_claim_scope(self, workflow_id: str, command: Union[sqlite3.Row, Dict[str, Any]]):
        stack = list(getattr(self._active_command_claim, "stack", []))
        stack.append(ActiveCommandClaim(workflow_id=workflow_id, command=dict(command)))
        self._active_command_claim.stack = stack
        try:
            yield
        finally:
            self._active_command_claim.stack = stack[:-1]

    def _active_command_claims(self) -> list[ActiveCommandClaim]:
        return list(getattr(self._active_command_claim, "stack", []))

    def _require_active_command_claim_live(self, con: sqlite3.Connection) -> None:
        for claim in self._active_command_claims():
            if not self._command_claim_is_live(con, claim.workflow_id, claim.command):
                raise CommandClaimLost()

    def renew_command_lease(
        self,
        workflow_id: str,
        command: Union[sqlite3.Row, Dict[str, Any]],
        *,
        lease_seconds: int,
    ) -> bool:
        """Extend the lease for the currently claimed command attempt.

        Long-running workflow/step commands can outlive short worker leases. Renewal is
        guarded by claimed_by + attempts so a stale worker cannot extend or complete a
        command after another worker has legitimately reclaimed it.
        """

        self._ensure_writable("renew workflow command lease")
        if lease_seconds <= 0:
            return False
        now = _now()
        lease_expires_at = now + lease_seconds
        params = self._claim_guard_params(workflow_id, command, now=now)
        if params is None:
            return False
        with self._connect() as con:
            changed = con.execute(
                """
                UPDATE workflow_commands_outbox
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                  AND workflow_id = ?
                  AND type = ?
                  AND status = 'running'
                  AND claimed_by = ?
                  AND ((claimed_by_instance_id IS NULL AND ? IS NULL) OR claimed_by_instance_id = ?)
                  AND claim_token = ?
                  AND attempts = ?
                  AND COALESCE(lease_expires_at, 0) > ?
                """,
                (lease_expires_at, now, *params),
            ).rowcount
        return changed > 0

    @contextmanager
    def _command_lease_heartbeat(
        self,
        workflow_id: str,
        command: Union[sqlite3.Row, Dict[str, Any]],
    ):
        lease_seconds = _lease_seconds_from_command(command)
        if lease_seconds <= 0:
            yield
            return

        interval = max(0.1, min(float(lease_seconds) / 3.0, 10.0))
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    renewed = self.renew_command_lease(workflow_id, command, lease_seconds=lease_seconds)
                except Exception:
                    continue
                if not renewed:
                    return

        thread = threading.Thread(
            target=heartbeat,
            name=f"workflow-command-lease-heartbeat:{workflow_id}:{command['id']}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1.0)

    def worker_once(
        self,
        workflow_id: str,
        *,
        worker_id: str,
        worker_instance_id: str | None = None,
        lease_seconds: int = 30,
    ) -> RunResult:
        command = self.claim_command(
            workflow_id,
            worker_id=worker_id,
            worker_instance_id=worker_instance_id,
            lease_seconds=lease_seconds,
            command_type=None,
            include_external_agent=self.agent_runner is not None,
        )
        if command is None:
            return self._result_from_instance(workflow_id)
        return self._execute_command(workflow_id, command)

    def worker_until_idle(
        self,
        workflow_id: str,
        *,
        worker_id: str,
        worker_instance_id: str | None = None,
        lease_seconds: int = 30,
        max_commands: Optional[int] = None,
    ) -> RunResult:
        result = self._result_from_instance(workflow_id)
        executed = 0
        while max_commands is None or executed < max_commands:
            command = self.claim_command(
                workflow_id,
                worker_id=worker_id,
                worker_instance_id=worker_instance_id,
                lease_seconds=lease_seconds,
                command_type=None,
                include_external_agent=self.agent_runner is not None,
            )
            if command is None:
                return self._result_from_instance(workflow_id)
            result = self._execute_command(workflow_id, command)
            executed += 1
            if result.status in {"completed", "failed"}:
                return result
        return result

    def runnable_workflows(
        self,
        *,
        limit: Optional[int] = None,
        include_external_agent: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return workflow instances with commands runnable by the Workflow Worker.

        This intentionally spans all workflow instances in one DB. The per-workflow
        `worker` command still drains a known workflow id; resident workers use this
        to lease pending or expired run_workflow/run_step/start_child_workflow commands without
        knowing workflow ids in advance.
        """

        now = _now()
        command_types = CommandType.worker_runnable_values(include_external_agent=include_external_agent)
        quoted_types = ", ".join(f"'{type_name}'" for type_name in command_types)
        query = f"""
            SELECT
              wi.id AS workflow_id,
              wi.workflow_name AS workflow_name,
              wi.workflow_ref AS workflow_ref,
              wi.status AS workflow_status,
              wi.waiting_on AS waiting_on,
              c.id AS command_id,
              c.type AS command_type,
              c.key AS command_key,
              c.status AS command_status,
              c.lease_expires_at AS lease_expires_at
            FROM workflow_commands_outbox c
            JOIN workflow_instances wi ON wi.id = c.workflow_id
            WHERE c.type IN ({quoted_types})
              AND wi.status NOT IN ('completed', 'failed', 'cancelled')
              AND (
                c.status = 'pending'
                OR (c.status = 'running' AND COALESCE(c.lease_expires_at, 0) <= ?)
              )
            ORDER BY c.id ASC
        """
        params: list[Any] = [now]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_command_error(
        self,
        workflow_id: str,
        command_id: int,
        error: Dict[str, Any],
        *,
        requeue: bool = True,
        allow_running: bool = False,
    ) -> bool:
        """Record a runner-side command error that happened before command execution.

        Resident runners can fail while loading the stored workflow ref, before
        `worker_once()` has a chance to claim and execute the command. Persist a
        small safe error summary on the command so status/dashboard surfaces show
        an actionable failure instead of leaving the problem only in process logs.
        """

        self._ensure_writable("record workflow command error")
        safe_error = {
            "type": str(error.get("type") or "Error")[:120],
            "message": str(error.get("message") or "")[:500],
        }
        now = _now()
        next_status = "pending" if requeue else "failed"
        status_clause = "status IN ('pending', 'running')" if allow_running else "status = 'pending'"
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                f"""
                UPDATE workflow_commands_outbox
                SET status = ?, claimed_by = NULL, claimed_by_instance_id = NULL,
                    claim_token = NULL, lease_expires_at = NULL,
                    last_error_json = ?, updated_at = ?
                WHERE id = ?
                  AND workflow_id = ?
                  AND {status_clause}
                  AND workflow_id IN (
                    SELECT id FROM workflow_instances
                    WHERE id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
                  )
                """,
                (next_status, JsonCodec.dumps(safe_error), now, command_id, workflow_id, workflow_id),
            ).rowcount
        return changed > 0

    def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        worker_instance_id: str,
        heartbeat_ttl_seconds: int = 90,
        identity: Optional[Dict[str, Any]] = None,
        status: str = "running",
    ) -> Dict[str, Any]:
        """Upsert a durable worker-process heartbeat for dashboard/runtime diagnostics."""

        self._ensure_writable("record workflow worker heartbeat")
        if not worker_id:
            raise ValueError("worker_id is required")
        if not worker_instance_id:
            raise ValueError("worker_instance_id is required")
        now = _now()
        ttl = max(1, int(heartbeat_ttl_seconds))
        identity = identity or {}
        row_values = {
            "worker_instance_id": worker_instance_id,
            "worker_id": worker_id,
            "status": status,
            "first_seen_at": now,
            "last_heartbeat_at": now,
            "heartbeat_expires_at": now + ttl,
            "hostname": identity.get("hostname"),
            "pid": identity.get("pid"),
            "cwd": identity.get("cwd"),
            "python_executable": identity.get("python_executable"),
            "python_version": identity.get("python_version"),
            "platform": identity.get("platform"),
            "hermes_version": identity.get("hermes_version"),
            "agent_runner_enabled": 1 if identity.get("agent_runner_enabled") else 0,
            "metadata_json": JsonCodec.dumps(_safe_worker_metadata(identity.get("metadata"))),
        }
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO workflow_workers(
                  worker_instance_id, worker_id, status, first_seen_at, last_heartbeat_at,
                  heartbeat_expires_at, hostname, pid, cwd, python_executable,
                  python_version, platform, hermes_version, agent_runner_enabled, metadata_json
                )
                VALUES (
                  :worker_instance_id, :worker_id, :status, :first_seen_at, :last_heartbeat_at,
                  :heartbeat_expires_at, :hostname, :pid, :cwd, :python_executable,
                  :python_version, :platform, :hermes_version, :agent_runner_enabled, :metadata_json
                )
                ON CONFLICT(worker_instance_id) DO UPDATE SET
                  worker_id = excluded.worker_id,
                  status = excluded.status,
                  last_heartbeat_at = excluded.last_heartbeat_at,
                  heartbeat_expires_at = excluded.heartbeat_expires_at,
                  hostname = excluded.hostname,
                  pid = excluded.pid,
                  cwd = excluded.cwd,
                  python_executable = excluded.python_executable,
                  python_version = excluded.python_version,
                  platform = excluded.platform,
                  hermes_version = excluded.hermes_version,
                  agent_runner_enabled = excluded.agent_runner_enabled,
                  metadata_json = excluded.metadata_json
                """,
                row_values,
            )
            row = con.execute(
                "SELECT * FROM workflow_workers WHERE worker_instance_id = ?",
                (worker_instance_id,),
            ).fetchone()
        return _worker_payload(row, now=now)

    def mark_worker_stopped(self, *, worker_instance_id: str, worker_id: str | None = None) -> None:
        self._ensure_writable("mark workflow worker stopped")
        now = _now()
        with self._connect() as con:
            if worker_id is None:
                con.execute(
                    """
                    UPDATE workflow_workers
                    SET status = 'stopped', last_heartbeat_at = ?, heartbeat_expires_at = ?
                    WHERE worker_instance_id = ?
                    """,
                    (now, now, worker_instance_id),
                )
            else:
                con.execute(
                    """
                    UPDATE workflow_workers
                    SET status = 'stopped', last_heartbeat_at = ?, heartbeat_expires_at = ?
                    WHERE worker_instance_id = ? AND worker_id = ?
                    """,
                    (now, now, worker_instance_id, worker_id),
                )

    def list_workers(self, *, active_only: bool = False) -> List[Dict[str, Any]]:
        now = _now()
        where = ""
        params: list[Any] = []
        if active_only:
            where = "WHERE status != 'stopped' AND heartbeat_expires_at > ?"
            params.append(now)
        with self._connect() as con:
            try:
                rows = con.execute(
                    f"""
                    SELECT *
                    FROM workflow_workers
                    {where}
                    ORDER BY last_heartbeat_at DESC, worker_instance_id ASC
                    """,
                    params,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table: workflow_workers" in str(exc):
                    return []
                raise
        return [_worker_payload(row, now=now) for row in rows]

    def events(self, workflow_id: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        self._instance(workflow_id)
        query = """
            SELECT seq, type, key, payload_json, idempotency_key, created_at
            FROM workflow_events
            WHERE workflow_id = ?
        """
        params: list[Any] = [workflow_id]
        if limit is not None:
            query += " ORDER BY seq DESC LIMIT ?"
            params.append(limit)
        else:
            query += " ORDER BY seq ASC"
        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
        if limit is not None:
            rows = list(reversed(rows))
        return [decode_event_row(row).to_public_dict() for row in rows]

    def pending_child_workflow_keys(self, workflow_id: str) -> list[str]:
        self._instance(workflow_id)
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT type, key
                FROM workflow_events
                WHERE workflow_id = ?
                  AND type IN ('ChildWorkflowRequested', 'ChildWorkflowCompleted', 'ChildWorkflowFailed')
                ORDER BY seq ASC
                """,
                (workflow_id,),
            ).fetchall()
        requested: list[str] = []
        terminal: set[str] = set()
        for row in rows:
            if row["type"] == "ChildWorkflowRequested" and row["key"] not in requested:
                requested.append(row["key"])
            elif row["type"] in {"ChildWorkflowCompleted", "ChildWorkflowFailed"}:
                terminal.add(row["key"])
        return [key for key in requested if key not in terminal]

    def reconcile_children(self, workflow_id: str) -> RunResult:
        pending = self.pending_child_workflow_keys(workflow_id)
        result = self._result_from_instance(workflow_id)
        for child_key in pending:
            result = self.reconcile_child_result(workflow_id, child_key)
            if result.status in {"failed", "cancelled"}:
                return result
        if not pending and result.status == "running" and self._has_terminal_child_workflow_events(workflow_id):
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._enqueue_workflow_run_row(con, workflow_id, reason="child_reconciled")
            return self._result_from_instance(workflow_id)
        return result

    def _has_terminal_child_workflow_events(self, workflow_id: str) -> bool:
        self._instance(workflow_id)
        with self._connect() as con:
            row = con.execute(
                """
                SELECT 1
                FROM workflow_events
                WHERE workflow_id = ?
                  AND type IN ('ChildWorkflowCompleted', 'ChildWorkflowFailed')
                LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
        return row is not None

    def reconcile_child_result(self, workflow_id: str, child_key: str) -> RunResult:
        requested = self._last_event_payload(workflow_id, "ChildWorkflowRequested", child_key)
        if requested is None:
            raise KeyError(f"no child workflow requested for key: {child_key}")
        child_id = requested["child_workflow_id"]

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            parent = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if parent is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if parent["status"] in {"completed", "failed", "cancelled"}:
                return self._result_from_row(parent)
            parent_wait_key = _parent_wait_key_for_child_wait(
                parent_row=parent,
                child_event_key=child_key,
                child_group=requested.get("group"),
            )

        try:
            child_result = self._result_from_instance(child_id)
        except KeyError:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                parent = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                if parent is None:
                    raise KeyError(f"unknown workflow_id: {workflow_id}")
                if parent["status"] in {"completed", "failed", "cancelled"}:
                    return self._result_from_row(parent)
                self._record_child_waiting(
                    con,
                    parent_workflow_id=workflow_id,
                    child_event_key=child_key,
                    child_workflow_id=child_id,
                    child_status="pending",
                    child_waiting_on=None,
                    parent_waiting_on=parent_wait_key,
                )
            return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=parent_wait_key)

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            parent = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if parent is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if parent["status"] in {"completed", "failed", "cancelled"}:
                return self._result_from_row(parent)
            parent_wait_key = _parent_wait_key_for_child_wait(
                parent_row=parent,
                child_event_key=child_key,
                child_group=requested.get("group"),
            )

            if child_result.status == "completed":
                self._append_event(
                    con,
                    workflow_id,
                    "ChildWorkflowCompleted",
                    key=child_key,
                    payload={"child_workflow_id": child_id, "result": child_result.result},
                    idempotency_key=f"child-completed:{child_key}",
                    ignore_duplicate=True,
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'running', updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (_now(), workflow_id),
                )
                self._enqueue_workflow_run_row(con, workflow_id, reason="child_reconciled", source_key=child_key)
            elif child_result.status in {"failed", "cancelled"}:
                error_type = "ChildWorkflowCancelled" if child_result.status == "cancelled" else "ChildWorkflowFailed"
                error = {"type": error_type, "message": child_result.error or f"child {child_result.status}: {child_id}"}
                self._append_event(
                    con,
                    workflow_id,
                    "ChildWorkflowFailed",
                    key=child_key,
                    payload={"child_workflow_id": child_id, "error": error},
                    idempotency_key=f"child-failed:{child_key}",
                    ignore_duplicate=True,
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'failed', waiting_on = NULL, error_json = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (JsonCodec.dumps(error), _now(), workflow_id),
                )
                return RunResult(workflow_id=workflow_id, status="failed", error=_format_error(error))
            else:
                self._record_child_waiting(
                    con,
                    parent_workflow_id=workflow_id,
                    child_event_key=child_key,
                    child_workflow_id=child_id,
                    child_status=child_result.status,
                    child_waiting_on=child_result.waiting_on,
                    parent_waiting_on=parent_wait_key,
                )
                return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=parent_wait_key)

        parent = self._instance(workflow_id)
        return self._result_from_row(parent)

    def list_workflows(self, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._status_projection.list_workflows(status=status)

    def _list_workflow_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        return self._status_projection._list_workflow_payload(row)

    def outbox_commands(
        self,
        *,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._status_projection.outbox_commands(workflow_id=workflow_id, status=status)

    def workflow_status(
        self,
        workflow_id: str,
        *,
        recent_events: int = 20,
        command_history: Optional[str] = None,
        command_limit: int = 20,
        command_payload_chars: int = 500,
    ) -> Dict[str, Any]:
        return self._status_projection.workflow_status(
            workflow_id,
            recent_events=recent_events,
            command_history=command_history,
            command_limit=command_limit,
            command_payload_chars=command_payload_chars,
        )

    def _command_history(
        self,
        workflow_id: str,
        *,
        mode: str,
        limit: int,
        payload_chars: int,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        return self._status_projection._command_history(
            workflow_id, mode=mode, limit=limit, payload_chars=payload_chars
        )

    def _terminal_reason(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        return self._status_projection._terminal_reason(workflow_id)

    def _approval_summaries(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self._status_projection._approval_summaries(events)

    def _review_request_summaries(
        self,
        human_inputs: list[dict[str, Any]],
        *,
        approvals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self._status_projection._review_request_summaries(human_inputs, approvals=approvals)

    def _operator_step_summaries(
        self,
        events: List[Dict[str, Any]],
        *,
        steps: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        return self._status_projection._operator_step_summaries(events, steps=steps)

    def _step_summaries(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self._status_projection._step_summaries(events)

    def _append_step_requested(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        step_key: str,
        *,
        completion_mode: str,
        step_type: str,
        label: str | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> None:
        existing_rows = con.execute(
            """
            SELECT payload_json
            FROM workflow_events
            WHERE workflow_id = ? AND type = 'StepRequested' AND key = ?
            ORDER BY seq ASC
            """,
            (workflow_id, step_key),
        ).fetchall()
        for row in existing_rows:
            existing = JsonCodec.loads(row["payload_json"])
            existing_mode = existing.get("completion_mode") if isinstance(existing, dict) else None
            existing_type = existing.get("step_type") if isinstance(existing, dict) else None
            if existing_mode != completion_mode or existing_type != step_type:
                raise ValueError(
                    "public step key conflict: "
                    f"{step_key!r} is already used as {existing_type or 'unknown'}"
                    f"/{existing_mode or 'unknown'} and cannot also be used as "
                    f"{step_type}/{completion_mode}. Use a distinct step key before "
                    "runtime plumbing prefixes are collapsed for operator-facing topology."
                )

        step_payload: Dict[str, Any] = {
            "key": step_key,
            "step_name": label or step_key,
            "completion_mode": completion_mode,
            "step_type": step_type,
        }
        if payload is not None:
            step_payload["request"] = payload
            for field in ("public_name", "public_label", "name_source"):
                if payload.get(field) is not None:
                    step_payload[field] = payload.get(field)
        self._append_event(
            con,
            workflow_id,
            "StepRequested",
            key=step_key,
            payload=step_payload,
            idempotency_key=f"step-requested:{step_type}:{step_key}",
            ignore_duplicate=True,
        )

    def _append_signal_step_completed(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        *,
        signal_type: str,
        key: str,
        output: Any,
        source: Optional[Dict[str, Any]],
        idempotency_key: str,
    ) -> None:
        if signal_type == "approval.decision":
            completion_mode = "approval"
            step_type = "approval"
        elif signal_type == "operator.response":
            completion_mode = "operator"
            step_type = "operator"
        elif signal_type == "agent.completed":
            completion_mode = "agent"
            step_type = "agent"
        else:
            return
        self._append_event(
            con,
            workflow_id,
            "StepCompleted",
            key=key,
            payload={
                "output": output,
                "completion_mode": completion_mode,
                "step_type": step_type,
                "source": source,
            },
            idempotency_key=f"step-completed:{signal_type}:{key}:{idempotency_key}",
            ignore_duplicate=True,
        )

    def _active_commands(self, workflow_id: str) -> List[Dict[str, Any]]:
        return self._status_projection._active_commands(workflow_id)

    def _enrich_command_payloads(self, commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self._status_projection._enrich_command_payloads(commands)

    def _workflow_command_summaries(self, workflow_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        return self._status_projection._workflow_command_summaries(workflow_ids)

    def _workflow_child_status_summaries(self, workflow_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        return self._status_projection._workflow_child_status_summaries(workflow_ids)

    def _signal_keys_by_workflow(self, workflow_ids: List[str]) -> Dict[str, set[str]]:
        return self._status_projection._signal_keys_by_workflow(workflow_ids)

    def _diagnostic_labels_for_command(
        self,
        command: Dict[str, Any],
        summary: Dict[str, Any],
        signal_keys: set[str],
    ) -> List[str]:
        return self._status_projection._diagnostic_labels_for_command(command, summary, signal_keys)

    def _child_workflow_summaries(self, parent_row: sqlite3.Row, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self._status_projection._child_workflow_summaries(parent_row, events)

    def _command_diagnostics(self, commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return self._status_projection._command_diagnostics(commands)

    def _run_decider(self, workflow_id: str, workflow_fn: Callable[..., Any]) -> RunResult:
        instance = self._instance(workflow_id)
        if instance["status"] == "cancelled":
            return self._result_from_instance(workflow_id)

        ctx = WorkflowContext(self, workflow_id)
        inputs = JsonCodec.loads(instance["input_json"])
        try:
            input_type = getattr(workflow_fn, "__workflow_input_type__", None)
            if input_type is not None:
                from .input_parsing import coerce_workflow_input

                inputs = coerce_workflow_input(inputs, input_type)
            from .authoring import bind_workflow_context, reset_workflow_context

            token = bind_workflow_context(ctx)
            try:
                signature = inspect.signature(workflow_fn)
                positional = [
                    parameter
                    for parameter in signature.parameters.values()
                    if parameter.kind
                    in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                if len(positional) <= 1:
                    result = _run_maybe_async(workflow_fn(inputs))
                else:
                    result = _run_maybe_async(workflow_fn(ctx, inputs))
            finally:
                reset_workflow_context(token)
        except WorkflowCancelled:
            return self._result_from_instance(workflow_id)
        except WorkflowWaiting as waiting:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._require_active_command_claim_live(con)
                row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown workflow_id: {workflow_id}")
                if row["status"] == "cancelled":
                    return self._result_from_row(row)
                changed = con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'waiting', waiting_on = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (waiting.waiting_on, _now(), workflow_id),
                ).rowcount
                if changed == 0:
                    row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                    return self._result_from_row(row)
            return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=waiting.waiting_on)
        except CommandClaimLost:
            return self._result_from_instance(workflow_id)
        except Exception as exc:  # v0/v1: fail closed and keep the error inspectable.
            error = _error_from_exception(exc)
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._require_active_command_claim_live(con)
                row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown workflow_id: {workflow_id}")
                if row["status"] == "cancelled":
                    return self._result_from_row(row)
                changed = con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'failed', error_json = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (JsonCodec.dumps(error), _now(), workflow_id),
                ).rowcount
                if changed == 0:
                    row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                    return self._result_from_row(row)
            return RunResult(workflow_id=workflow_id, status="failed", error=f"{type(exc).__name__}: {exc}")

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_active_command_claim_live(con)
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if row["status"] == "cancelled":
                return self._result_from_row(row)
            self._append_event(
                con,
                workflow_id,
                "WorkflowCompleted",
                key="workflow:completed",
                payload={"result": result},
                idempotency_key="workflow:completed",
                ignore_duplicate=True,
            )
            changed = con.execute(
                """
                UPDATE workflow_instances
                SET status = 'completed', waiting_on = NULL, result_json = ?, updated_at = ?
                WHERE id = ? AND status != 'cancelled'
                """,
                (JsonCodec.dumps(result), _now(), workflow_id),
            ).rowcount
            if changed == 0:
                row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                return self._result_from_row(row)
        return RunResult(workflow_id=workflow_id, status="completed", result=result)

    def _result_from_instance(self, workflow_id: str) -> RunResult:
        row = self._instance(workflow_id)
        return self._result_from_row(row)

    def _result_from_row(self, row: sqlite3.Row) -> RunResult:
        return RunResult(
            workflow_id=row["id"],
            status=row["status"],
            waiting_on=row["waiting_on"],
            result=JsonCodec.loads(row["result_json"]),
            error=_format_error(JsonCodec.loads(row["error_json"])),
        )

    def _workflow_fn_for_instance(self, instance: sqlite3.Row) -> Callable[..., Any]:
        workflow_name = instance["workflow_name"]
        workflow_fn = _WORKFLOW_REGISTRY.get(workflow_name)
        if workflow_fn is not None:
            return workflow_fn

        workflow_ref = instance["workflow_ref"]
        if workflow_ref:
            workflow_fn = self._workflow_fn_from_ref(str(workflow_ref), expected_workflow_name=workflow_name)
            if workflow_fn is not None:
                return workflow_fn

        self._load_generated_child_workflow_from_parent_history(instance["id"], expected_workflow_name=workflow_name)
        workflow_fn = _WORKFLOW_REGISTRY.get(workflow_name)
        if workflow_fn is None:
            raise KeyError(workflow_name)
        return workflow_fn

    def _workflow_fn_from_ref(self, workflow_ref: str, *, expected_workflow_name: str) -> Callable[..., Any] | None:
        if ":" not in workflow_ref and not workflow_ref.endswith(".py"):
            return None
        try:
            from .workflow_loading import load_workflow_ref

            workflow_fn = load_workflow_ref(workflow_ref)
        except Exception:
            return None
        registered_name = getattr(workflow_fn, "__workflow_name__", getattr(workflow_fn, "__name__", None))
        if registered_name != expected_workflow_name:
            return None
        _WORKFLOW_REGISTRY.setdefault(expected_workflow_name, workflow_fn)
        return workflow_fn

    def _load_generated_child_workflow_from_parent_history(self, child_workflow_id: str, *, expected_workflow_name: str) -> None:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT payload_json
                FROM workflow_events
                WHERE type = 'ChildWorkflowRequested'
                ORDER BY workflow_id ASC, seq ASC
                """
            ).fetchall()
        for row in rows:
            payload = JsonCodec.loads(row["payload_json"])
            if not isinstance(payload, dict) or payload.get("child_workflow_id") != child_workflow_id:
                continue
            workflow_ref = payload.get("workflow")
            if not isinstance(workflow_ref, Workflow):
                continue
            workflow_ref.with_base_dir(self.db_path.parent).load(approved=True)
            if expected_workflow_name in _WORKFLOW_REGISTRY:
                return

    def _record_child_waiting(
        self,
        con: sqlite3.Connection,
        *,
        parent_workflow_id: str,
        child_event_key: str,
        child_workflow_id: str,
        child_status: str,
        child_waiting_on: str | None,
        parent_waiting_on: str,
    ) -> None:
        self._append_event(
            con,
            parent_workflow_id,
            "ChildWorkflowWaiting",
            key=child_event_key,
            payload={
                "child_workflow_id": child_workflow_id,
                "status": child_status,
                "waiting_on": child_waiting_on,
            },
            idempotency_key=f"child-waiting:{child_event_key}:{child_status}:{child_waiting_on or ''}",
            ignore_duplicate=True,
        )
        con.execute(
            """
            UPDATE workflow_instances
            SET status = 'waiting', waiting_on = ?, updated_at = ?
            WHERE id = ? AND status != 'cancelled'
            """,
            (parent_waiting_on, _now(), parent_workflow_id),
        )

    def _execute_command(self, workflow_id: str, command: Union[sqlite3.Row, Dict[str, Any]]) -> RunResult:
        command_model = decode_command_row(command)
        payload = command_model.payload
        command_type = command_model.command_type
        with self._command_claim_scope(workflow_id, command):
            try:
                if command_type is CommandType.RUN_WORKFLOW:
                    return self._execute_run_workflow_command(workflow_id, command)
                if command_type is CommandType.RUN_STEP:
                    return self._execute_run_step_command(workflow_id, command)
                if command_type is CommandType.START_CHILD_WORKFLOW:
                    return self._execute_start_child_workflow_command(workflow_id, command, payload)
                if command_type is CommandType.EXTERNAL_AGENT:
                    return self._execute_external_agent_command(workflow_id, command, payload)
                raise ValueError(f"unknown workflow command type: {command_type.value}")
            except CommandClaimLost:
                return self._result_from_instance(workflow_id)

    def _execute_run_workflow_command(self, workflow_id: str, command: Union[sqlite3.Row, Dict[str, Any]]) -> RunResult:
        with self._connect() as con:
            if not self._command_claim_is_live(con, workflow_id, command):
                return self._result_from_instance(workflow_id)

        workflow_fn = self._workflow_fn_for_instance(self._instance(workflow_id))
        with self._command_lease_heartbeat(workflow_id, command):
            result = self._run_decider(workflow_id, workflow_fn)
        now = _now()
        params = self._claim_guard_params(workflow_id, command, now=now)
        if params is None:
            return self._result_from_instance(workflow_id)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                """
                SELECT c.payload_json, wi.status AS workflow_status
                FROM workflow_commands_outbox c
                JOIN workflow_instances wi ON wi.id = c.workflow_id
                WHERE c.id = ?
                  AND c.workflow_id = ?
                  AND c.type = ?
                  AND c.status = 'running'
                  AND c.claimed_by = ?
                  AND ((c.claimed_by_instance_id IS NULL AND ? IS NULL) OR c.claimed_by_instance_id = ?)
                  AND c.claim_token = ?
                  AND c.attempts = ?
                """,
                params[:-1],
            ).fetchone()
            if current is None:
                return self._result_from_instance(workflow_id)
            payload = JsonCodec.loads(current["payload_json"])
            rerun_requested = isinstance(payload, dict) and payload.get("rerun_requested") is True
            workflow_terminal = current["workflow_status"] in TERMINAL_WORKFLOW_STATUSES
            if rerun_requested and not workflow_terminal and result.status != "failed":
                next_payload = {
                    "reason": payload.get("rerun_reason") or "wakeup_during_run",
                    "rerun_from_running": True,
                }
                if payload.get("rerun_source_key") is not None:
                    next_payload["source_key"] = payload.get("rerun_source_key")
                changed = con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'pending', payload_json = ?, claimed_by = NULL, claimed_by_instance_id = NULL,
                        claim_token = NULL, lease_expires_at = NULL,
                        last_error_json = NULL, updated_at = ?
                    WHERE id = ?
                      AND workflow_id = ?
                      AND type = ?
                      AND status = 'running'
                      AND claimed_by = ?
                      AND ((claimed_by_instance_id IS NULL AND ? IS NULL) OR claimed_by_instance_id = ?)
                      AND claim_token = ?
                      AND attempts = ?
                    """,
                    (JsonCodec.dumps(next_payload), now, *params[:-1]),
                ).rowcount
            else:
                status = "failed" if result.status == "failed" else "completed"
                last_error = {"type": "WorkflowRunFailed", "message": result.error} if result.status == "failed" else None
                changed = con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = ?, claim_token = NULL, last_error_json = ?, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                      AND workflow_id = ?
                      AND type = ?
                      AND status = 'running'
                      AND claimed_by = ?
                      AND ((claimed_by_instance_id IS NULL AND ? IS NULL) OR claimed_by_instance_id = ?)
                      AND claim_token = ?
                      AND attempts = ?
                    """,
                    (
                        status,
                        JsonCodec.dumps(last_error) if last_error is not None else None,
                        now,
                        *params[:-1],
                    ),
                ).rowcount
        if changed == 0:
            return self._result_from_instance(workflow_id)
        return result

    def _execute_start_child_workflow_command(
        self,
        workflow_id: str,
        command: Union[sqlite3.Row, Dict[str, Any]],
        payload: Dict[str, Any],
    ) -> RunResult:
        key = command["key"]
        workflow_ref = payload["workflow"]
        if not isinstance(workflow_ref, Workflow):
            raise TypeError("start_child_workflow command payload must include a Workflow value")
        workflow_ref = workflow_ref.with_base_dir(self.db_path.parent)

        child_id = payload["child_workflow_id"]
        child_fn = workflow_ref.load(approved=True)
        with self._connect() as con:
            if not self._command_claim_is_live(con, workflow_id, command):
                return self._result_from_instance(workflow_id)
        with self._command_lease_heartbeat(workflow_id, command):
            child_result = self.run_until_idle(child_fn, payload["inputs"], workflow_id=child_id)

        now = _now()
        params = self._claim_guard_params(workflow_id, command, now=now)
        if params is None:
            return self._result_from_instance(workflow_id)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_active_command_claim_live(con)

            if child_result.status == "completed":
                self._append_event(
                    con,
                    workflow_id,
                    "ChildWorkflowCompleted",
                    key=key,
                    payload={"child_workflow_id": child_id, "result": child_result.result},
                    idempotency_key=f"child-completed:{key}",
                    ignore_duplicate=True,
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'running', updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (_now(), workflow_id),
                )
                self._enqueue_workflow_run_row(con, workflow_id, reason="child_completed", source_key=key)
                changed = con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                      AND workflow_id = ?
                      AND type = ?
                      AND status = 'running'
                      AND claimed_by = ?
                      AND ((claimed_by_instance_id IS NULL AND ? IS NULL) OR claimed_by_instance_id = ?)
                      AND claim_token = ?
                      AND attempts = ?
                      AND COALESCE(lease_expires_at, 0) > ?
                    """,
                    (now, *params),
                ).rowcount
                if changed == 0:
                    raise CommandClaimLost()
            elif child_result.status == "failed":
                error = {"type": "ChildWorkflowFailed", "message": child_result.error or f"child failed: {child_id}"}
                self._append_event(
                    con,
                    workflow_id,
                    "ChildWorkflowFailed",
                    key=key,
                    payload={"child_workflow_id": child_id, "error": error},
                    idempotency_key=f"child-failed:{key}",
                    ignore_duplicate=True,
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'failed', error_json = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (JsonCodec.dumps(error), _now(), workflow_id),
                )
                changed = con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                      AND workflow_id = ?
                      AND type = ?
                      AND status = 'running'
                      AND claimed_by = ?
                      AND ((claimed_by_instance_id IS NULL AND ? IS NULL) OR claimed_by_instance_id = ?)
                      AND claim_token = ?
                      AND attempts = ?
                      AND COALESCE(lease_expires_at, 0) > ?
                    """,
                    (now, *params),
                ).rowcount
                if changed == 0:
                    raise CommandClaimLost()
                return RunResult(workflow_id=workflow_id, status="failed", error=_format_error(error))
            else:
                parent_row = con.execute("SELECT waiting_on FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                parent_wait_key = _parent_wait_key_for_child_wait(
                    parent_row=parent_row,
                    child_event_key=key,
                    child_group=payload.get("group"),
                )
                self._record_child_waiting(
                    con,
                    parent_workflow_id=workflow_id,
                    child_event_key=key,
                    child_workflow_id=child_id,
                    child_status=child_result.status,
                    child_waiting_on=child_result.waiting_on,
                    parent_waiting_on=parent_wait_key,
                )
                changed = con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                      AND workflow_id = ?
                      AND type = ?
                      AND status = 'running'
                      AND claimed_by = ?
                      AND ((claimed_by_instance_id IS NULL AND ? IS NULL) OR claimed_by_instance_id = ?)
                      AND claim_token = ?
                      AND attempts = ?
                      AND COALESCE(lease_expires_at, 0) > ?
                    """,
                    (now, *params),
                ).rowcount
                if changed == 0:
                    raise CommandClaimLost()
                return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=parent_wait_key)

        parent = self._instance(workflow_id)
        return self._result_from_row(parent)

    def _fail_running_command(
        self,
        workflow_id: str,
        command: Union[sqlite3.Row, Dict[str, Any]],
        *,
        key: str,
        error: Dict[str, Any],
    ) -> RunResult:
        now = _now()
        params = self._claim_guard_params(workflow_id, command, now=now)
        if params is None:
            return self._result_from_instance(workflow_id)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_active_command_claim_live(con)
            self._append_event(
                con,
                workflow_id,
                "StepFailed",
                key=key,
                payload={"error": error},
                idempotency_key=f"failed:{key}:{command['id']}:{command['attempts']}",
                ignore_duplicate=True,
            )
            con.execute(
                """
                UPDATE workflow_instances
                SET status = 'failed', error_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (JsonCodec.dumps(error), _now(), workflow_id),
            )
            changed = con.execute(
                """
                UPDATE workflow_commands_outbox
                SET status = 'failed', claim_token = NULL, last_error_json = ?, lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                  AND workflow_id = ?
                  AND type = ?
                  AND status = 'running'
                  AND claimed_by = ?
                  AND ((claimed_by_instance_id IS NULL AND ? IS NULL) OR claimed_by_instance_id = ?)
                  AND claim_token = ?
                  AND attempts = ?
                  AND COALESCE(lease_expires_at, 0) > ?
                """,
                (JsonCodec.dumps(error), now, *params),
            ).rowcount
            if changed == 0:
                raise CommandClaimLost()
        return RunResult(workflow_id=workflow_id, status="failed", error=_format_error(error))

    def _execute_external_agent_command(
        self,
        workflow_id: str,
        command: Union[sqlite3.Row, Dict[str, Any]],
        payload: Dict[str, Any],
    ) -> RunResult:
        if self.agent_runner is None:
            error = {"type": "AgentRunnerMissing", "message": "external_agent command requires WorkflowEngine.agent_runner"}
            return self._fail_running_command(workflow_id, command, key=command["key"], error=error)

        agent_key = str(payload.get("key") or str(command["key"]).removeprefix("agent:"))
        request = payload.get("artifact")
        if not isinstance(request, dict):
            request = {
                "kind": "agent.request.v1",
                "name": payload.get("assignee") or "agent",
                "prompt": payload.get("prompt") or "",
                "rendered_prompt": payload.get("prompt") or "",
                "returns": "json",
                "input": None,
                "step_key": agent_key,
            }

        with self._connect() as con:
            if not self._command_claim_is_live(con, workflow_id, command):
                return self._result_from_instance(workflow_id)

        try:
            from .prompts import _build_runner_request

            runner_ctx = type("AgentRunnerContext", (), {"workflow_id": workflow_id, "step_key": agent_key})()
            runner_request = _build_runner_request(runner_ctx, request)
            with self._command_lease_heartbeat(workflow_id, command):
                runner_response = _run_maybe_async(self.agent_runner(runner_request))
            if isinstance(runner_response, dict) and "output" in runner_response:
                output = runner_response["output"]
                provenance = runner_response.get("provenance")
            else:
                output = runner_response
                provenance = None
            JsonCodec.dumps(output)
        except Exception as exc:
            error = _error_from_exception(exc)
            return self._fail_running_command(workflow_id, command, key=agent_key, error=error)

        source: Dict[str, Any] = {"kind": "agent", "id": str(command["claimed_by"] or "workflow-worker")}
        if provenance is not None:
            source["provenance"] = provenance
        with self._connect() as con:
            if not self._command_claim_is_live(con, workflow_id, command):
                return self._result_from_instance(workflow_id)
        return self.signal(
            workflow_id,
            str(payload.get("signal_type") or "agent.completed"),
            key=agent_key,
            payload=output,
            source=source,
            idempotency_key=f"agent-runner:{command['id']}:{command['attempts']}:{agent_key}",
        )

    def _execute_run_step_command(self, workflow_id: str, command: Union[sqlite3.Row, Dict[str, Any]]) -> RunResult:
        from .decorators import get_step_body

        key = command["key"]
        payload = command["payload"] if isinstance(command, dict) else JsonCodec.loads(command["payload_json"])
        step_name = payload["step_name"]
        args = payload.get("args", [])
        kwargs = payload.get("kwargs", {})
        body = get_step_body(step_name)

        with self._connect() as con:
            if not self._command_claim_is_live(con, workflow_id, command):
                return self._result_from_instance(workflow_id)

        from .authoring import bind_workflow_context, reset_workflow_context

        step_context = StepExecutionContext(self, workflow_id, key)
        token = bind_workflow_context(step_context)
        try:
            with self._command_lease_heartbeat(workflow_id, command):
                output = _run_maybe_async(_call_step_body(body, step_context, *args, **kwargs))
        except Exception as exc:
            error = _error_from_exception(exc)
            return self._fail_running_command(workflow_id, command, key=key, error=error)
        finally:
            reset_workflow_context(token)

        metadata = None
        if isinstance(output, StepOutput):
            metadata = output.metadata
            output = output.output
        completion_payload = {"output": output}
        if metadata is not None:
            completion_payload["metadata"] = metadata
        try:
            JsonCodec.dumps(completion_payload)
        except Exception as exc:
            error = _error_from_exception(exc)
            return self._fail_running_command(workflow_id, command, key=key, error=error)

        now = _now()
        params = self._claim_guard_params(workflow_id, command, now=now)
        if params is None:
            return self._result_from_instance(workflow_id)
        try:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._require_active_command_claim_live(con)
                row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown workflow_id: {workflow_id}")
                if row["status"] in TERMINAL_WORKFLOW_STATUSES:
                    return self._result_from_row(row)
                self._append_event(
                    con,
                    workflow_id,
                    "StepCompleted",
                    key=key,
                    payload=completion_payload,
                    idempotency_key=f"completed:{key}",
                    ignore_duplicate=True,
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'running', waiting_on = NULL, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (now, workflow_id),
                )
                self._enqueue_workflow_run_row(con, workflow_id, reason="step_completed", source_key=key)
                changed = con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'completed', claim_token = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                      AND workflow_id = ?
                      AND type = ?
                      AND status = 'running'
                      AND claimed_by = ?
                      AND ((claimed_by_instance_id IS NULL AND ? IS NULL) OR claimed_by_instance_id = ?)
                      AND claim_token = ?
                      AND attempts = ?
                      AND COALESCE(lease_expires_at, 0) > ?
                    """,
                    (now, *params),
                ).rowcount
                if changed == 0:
                    raise CommandClaimLost()
        except CommandClaimLost:
            return self._result_from_instance(workflow_id)
        return self._result_from_instance(workflow_id)

    def _next_pending_command(self, workflow_id: str, *, command_type: str) -> Optional[sqlite3.Row]:
        with self._connect() as con:
            return con.execute(
                """
                SELECT * FROM workflow_commands_outbox
                WHERE workflow_id = ? AND type = ? AND status = 'pending'
                ORDER BY id ASC LIMIT 1
                """,
                (workflow_id, command_type),
            ).fetchone()

    def _instance(self, workflow_id: str) -> sqlite3.Row:
        with self._connect() as con:
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown workflow_id: {workflow_id}")
        return row

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_instances(
                  id TEXT PRIMARY KEY,
                  workflow_name TEXT NOT NULL,
                  workflow_ref TEXT,
                  status TEXT NOT NULL,
                  waiting_on TEXT,
                  input_json TEXT NOT NULL,
                  result_json TEXT,
                  error_json TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  workflow_id TEXT NOT NULL,
                  seq INTEGER NOT NULL,
                  type TEXT NOT NULL,
                  key TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  idempotency_key TEXT,
                  created_at INTEGER NOT NULL,
                  UNIQUE(workflow_id, seq),
                  UNIQUE(workflow_id, idempotency_key)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_events_one_approval_decision
                ON workflow_events(workflow_id, key)
                WHERE type = 'SignalReceived' AND key LIKE 'signal:approval.decision:%';

                CREATE TABLE IF NOT EXISTS workflow_commands_outbox(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  workflow_id TEXT NOT NULL,
                  type TEXT NOT NULL,
                  key TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  claimed_by TEXT,
                  claimed_by_instance_id TEXT,
                  claim_token TEXT,
                  lease_expires_at INTEGER,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  last_error_json TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER,
                  UNIQUE(workflow_id, key)
                );

                CREATE TABLE IF NOT EXISTS workflow_workers(
                  worker_instance_id TEXT PRIMARY KEY,
                  worker_id TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'running',
                  first_seen_at INTEGER NOT NULL,
                  last_heartbeat_at INTEGER NOT NULL,
                  heartbeat_expires_at INTEGER NOT NULL,
                  hostname TEXT,
                  pid INTEGER,
                  cwd TEXT,
                  python_executable TEXT,
                  python_version TEXT,
                  platform TEXT,
                  hermes_version TEXT,
                  agent_runner_enabled INTEGER NOT NULL DEFAULT 0,
                  metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_workers_worker_id
                ON workflow_workers(worker_id, last_heartbeat_at DESC);

                CREATE INDEX IF NOT EXISTS idx_workflow_workers_active
                ON workflow_workers(status, heartbeat_expires_at);

                """
            )
            self._ensure_instance_columns(con)
            self._ensure_command_columns(con)
            self._ensure_command_indexes(con)

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            uri_path = quote(str(self.db_path.resolve()), safe="/")
            con = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, factory=_ClosingSqliteConnection)
        else:
            con = sqlite3.connect(self.db_path, factory=_ClosingSqliteConnection)
        con.row_factory = sqlite3.Row
        return con

    def _last_event_payload(self, workflow_id: str, event_type: str, key: str) -> Any | None:
        self._instance(workflow_id)
        with self._connect() as con:
            row = con.execute(
                """
                SELECT payload_json FROM workflow_events
                WHERE workflow_id = ? AND type = ? AND key = ?
                ORDER BY seq DESC LIMIT 1
                """,
                (workflow_id, event_type, key),
            ).fetchone()
        if row is None:
            return None
        return JsonCodec.loads(row["payload_json"])

    def _append_event(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        event_type: str,
        *,
        key: str,
        payload: Any,
        idempotency_key: Optional[str],
        ignore_duplicate: bool = False,
    ) -> bool:
        self._require_active_command_claim_live(con)
        event = make_event(event_type, key=key, payload=payload, idempotency_key=idempotency_key, created_at=_now())
        next_seq = con.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM workflow_events WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()[0]
        try:
            con.execute(
                """
                INSERT INTO workflow_events(workflow_id, seq, type, key, payload_json, idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (workflow_id, next_seq, event.event_type.value, event.key, JsonCodec.dumps(event.payload), event.idempotency_key, event.created_at),
            )
            return True
        except sqlite3.IntegrityError:
            if ignore_duplicate:
                return False
            raise

    def _insert_command(self, workflow_id: str, command_type: str, key: str, payload: Any) -> bool:
        with self._connect() as con:
            try:
                return self._insert_command_row(con, workflow_id, command_type, key, payload)
            except sqlite3.IntegrityError:
                return False

    def _insert_command_row(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        command_type: str,
        key: str,
        payload: Any,
    ) -> bool:
        self._require_active_command_claim_live(con)
        command = make_command(command_type, workflow_id=workflow_id, key=key, payload=payload)
        con.execute(
            """
            INSERT INTO workflow_commands_outbox(workflow_id, type, key, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workflow_id, command.command_type.value, command.key, JsonCodec.dumps(command.payload), _now(), _now()),
        )
        return True

    def _enqueue_workflow_run_row(
        self,
        con: sqlite3.Connection,
        workflow_id: str,
        *,
        reason: str,
        source_key: str | None = None,
    ) -> bool:
        self._require_active_command_claim_live(con)
        row = con.execute("SELECT status FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown workflow_id: {workflow_id}")
        if row["status"] in TERMINAL_WORKFLOW_STATUSES:
            return False

        payload: Dict[str, Any] = {"reason": reason}
        if source_key is not None:
            payload["source_key"] = source_key
        payload_json = JsonCodec.dumps(payload)
        now = _now()
        existing = con.execute(
            """
            SELECT id, status, payload_json
            FROM workflow_commands_outbox
            WHERE workflow_id = ? AND type = 'run_workflow' AND key = 'workflow:run'
            """,
            (workflow_id,),
        ).fetchone()
        if existing is None:
            con.execute(
                """
                INSERT INTO workflow_commands_outbox(workflow_id, type, key, payload_json, status, created_at, updated_at)
                VALUES (?, 'run_workflow', 'workflow:run', ?, 'pending', ?, ?)
                """,
                (workflow_id, payload_json, now, now),
            )
            return True
        if existing["status"] == "pending":
            return False
        if existing["status"] == "running":
            running_payload = JsonCodec.loads(existing["payload_json"])
            if not isinstance(running_payload, dict):
                running_payload = {}
            running_payload["rerun_requested"] = True
            running_payload["rerun_requested_at"] = now
            running_payload["rerun_reason"] = reason
            if source_key is not None:
                running_payload["rerun_source_key"] = source_key
            changed = con.execute(
                """
                UPDATE workflow_commands_outbox
                SET payload_json = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (JsonCodec.dumps(running_payload), now, existing["id"]),
            ).rowcount
            return changed > 0
        changed = con.execute(
            """
            UPDATE workflow_commands_outbox
            SET status = 'pending', payload_json = ?, claimed_by = NULL, claimed_by_instance_id = NULL,
                claim_token = NULL, lease_expires_at = NULL,
                last_error_json = NULL, updated_at = ?
            WHERE id = ? AND status NOT IN ('pending', 'running')
            """,
            (payload_json, now, existing["id"]),
        ).rowcount
        return changed > 0


    def _ensure_instance_columns(self, con: sqlite3.Connection) -> None:
        existing = {row["name"] for row in con.execute("PRAGMA table_info(workflow_instances)").fetchall()}
        migrations = {
            "workflow_ref": "ALTER TABLE workflow_instances ADD COLUMN workflow_ref TEXT",
        }
        for column, sql in migrations.items():
            if column not in existing:
                con.execute(sql)

    def _ensure_command_columns(self, con: sqlite3.Connection) -> None:
        existing = {row["name"] for row in con.execute("PRAGMA table_info(workflow_commands_outbox)").fetchall()}
        migrations = {
            "claimed_by": "ALTER TABLE workflow_commands_outbox ADD COLUMN claimed_by TEXT",
            "claimed_by_instance_id": "ALTER TABLE workflow_commands_outbox ADD COLUMN claimed_by_instance_id TEXT",
            "claim_token": "ALTER TABLE workflow_commands_outbox ADD COLUMN claim_token TEXT",
            "lease_expires_at": "ALTER TABLE workflow_commands_outbox ADD COLUMN lease_expires_at INTEGER",
            "attempts": "ALTER TABLE workflow_commands_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "last_error_json": "ALTER TABLE workflow_commands_outbox ADD COLUMN last_error_json TEXT",
            "updated_at": "ALTER TABLE workflow_commands_outbox ADD COLUMN updated_at INTEGER",
        }
        for column, sql in migrations.items():
            if column not in existing:
                con.execute(sql)
        con.execute(
            """
            UPDATE workflow_commands_outbox
            SET status = 'pending', claimed_by = NULL, claimed_by_instance_id = NULL,
                claim_token = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE status = 'running' AND claim_token IS NULL
            """,
            (_now(),),
        )

    def _ensure_command_indexes(self, con: sqlite3.Connection) -> None:
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_commands_claimed_by_instance
            ON workflow_commands_outbox(claimed_by_instance_id)
            WHERE claimed_by_instance_id IS NOT NULL
            """
        )

    def _command_payload(self, row: sqlite3.Row, *, include_claim_token: bool = False) -> Dict[str, Any]:
        payload = decode_command_row(row).to_public_dict()
        if include_claim_token:
            payload["claim_token"] = row["claim_token"]
        return payload


class WorkflowContext:
    def __init__(self, engine: WorkflowEngine, workflow_id: str):
        self.engine = engine
        self.workflow_id = workflow_id
        self._step_call_counts: Dict[str, int] = {}
        self._gather_call_count = 0
        self._approval_call_counts: Dict[str, int] = {}
        self.approval = ApprovalClient(self)

    def _raise_if_cancelled(self) -> None:
        if self.engine._instance(self.workflow_id)["status"] == "cancelled":
            raise WorkflowCancelled()

    def _raise_if_cancelled_in_connection(self, con: sqlite3.Connection) -> None:
        row = con.execute("SELECT status FROM workflow_instances WHERE id = ?", (self.workflow_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown workflow_id: {self.workflow_id}")
        if row["status"] == "cancelled":
            raise WorkflowCancelled()

    async def run_step(
        self,
        step_name: str,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        *,
        block: bool = True,
        payload_builder: Optional[Callable[[], Dict[str, Any]]] = None,
        key: str | None = None,
    ) -> Any:
        self._raise_if_cancelled()
        if key is None:
            call_index = self._step_call_counts.get(step_name, 0)
            self._step_call_counts[step_name] = call_index + 1
            key = f"step:{step_name}:{call_index}"

        payload: Dict[str, Any] | None = None
        completed = self._last_event("StepCompleted", key)
        if completed is not None:
            if payload_builder is not None:
                payload = payload_builder()
                if payload.get("step_name") != step_name:
                    raise ValueError("payload_builder step_name must match durable step name")
                requested = self._last_event("StepRequested", key)
                if requested is not None:
                    _validate_step_request_fingerprint(key, requested, payload)
            return _coerce_completed_step_output(step_name, completed["output"])

        requested = self._last_event("StepRequested", key)
        if requested is not None and payload_builder is not None:
            payload = payload_builder()
            if payload.get("step_name") != step_name:
                raise ValueError("payload_builder step_name must match durable step name")
            _validate_step_request_fingerprint(key, requested, payload)

        if requested is None:
            if payload_builder is None:
                payload = {"step_name": step_name, "args": list(args), "kwargs": kwargs}
            else:
                payload = payload_builder()
                if payload.get("step_name") != step_name:
                    raise ValueError("payload_builder step_name must match durable step name")
            with self.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._raise_if_cancelled_in_connection(con)
                inserted = self.engine._append_event(
                    con,
                    self.workflow_id,
                    "StepRequested",
                    key=key,
                    payload=payload,
                    idempotency_key=f"requested:{key}",
                    ignore_duplicate=True,
                )
                if inserted:
                    self.engine._insert_command_row(con, self.workflow_id, "run_step", key, payload)

        if block:
            raise WorkflowWaiting(key)
        return PendingStep(key)

    async def gather(self, *calls: Any) -> List[Any]:
        """Durably fan out multiple step calls before exiting.

        `ctx.gather(step_a(ctx), step_b(ctx))` enqueues every missing step in the
        group on the same decider pass, then exits on a synthetic gather wait.
        Once all children have StepCompleted events, results resolve in argument
        order without re-running completed steps.
        """

        gather_index = self._gather_call_count
        self._gather_call_count += 1
        gather_key = f"gather:{gather_index}"
        results: List[Any] = []
        pending: List[str] = []

        for call in calls:
            if not getattr(call, "__durable_step_call__", False):
                if inspect.iscoroutine(call):
                    call.close()
                raise TypeError("ctx.gather only supports @step calls in this spike")
            result = await self.run_step(
                call.step_name,
                call.args,
                call.kwargs,
                block=False,
                payload_builder=getattr(call, "payload_builder", None),
            )
            if isinstance(result, PendingStep):
                pending.append(result.key)
                results.append(None)
            else:
                results.append(result)

        if pending:
            with self.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._raise_if_cancelled_in_connection(con)
                self.engine._append_event(
                    con,
                    self.workflow_id,
                    "GatherWaiting",
                    key=gather_key,
                    payload={"pending": pending},
                    idempotency_key=f"gather-waiting:{gather_key}",
                    ignore_duplicate=True,
                )
            raise WorkflowWaiting(gather_key)

        return results


    async def wait_for_pending_group(
        self,
        wait_key: str,
        pending: List[str],
        *,
        kind: str = "parallel",
        limit: int | None = None,
    ) -> None:
        if pending:
            with self.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._raise_if_cancelled_in_connection(con)
                self.engine._append_event(
                    con,
                    self.workflow_id,
                    "ParallelWaiting" if kind == "parallel" else "GroupWaiting",
                    key=wait_key,
                    payload={"pending": pending, "limit": limit, "kind": kind},
                    idempotency_key=f"{kind}-waiting:{wait_key}",
                    ignore_duplicate=True,
                )
            raise WorkflowWaiting(wait_key)

    async def start_child(
        self,
        workflow_ref: Workflow,
        inputs: Any,
        *,
        key: str | None = None,
        group: str | None = None,
        block: bool = True,
    ) -> Any:
        self._raise_if_cancelled()
        if not isinstance(workflow_ref, Workflow):
            raise TypeError("ctx.start_child expects a Workflow value")
        child_group = _workflow_child_group(workflow_ref, group=group)
        child_key_part = _safe_child_key(key if key is not None else str(self._child_call_count_for(child_group)))
        event_key = f"child:{child_group}:{child_key_part}"

        completed = self._last_event("ChildWorkflowCompleted", event_key)
        if completed is not None:
            return completed["result"]
        failed = self._last_event("ChildWorkflowFailed", event_key)
        if failed is not None:
            raise RuntimeError(failed.get("error", {}).get("message") or f"child workflow failed: {event_key}")

        workflow_ref = workflow_ref.with_base_dir(self.engine.db_path.parent)
        await self._require_generated_workflow_approval(workflow_ref)
        workflow_ref.load(approved=workflow_ref.approval_required)

        child_workflow_id = f"{self.workflow_id}.child.{child_group}.{child_key_part}"
        request_payload = {
            "workflow": workflow_ref,
            "workflow_name": workflow_ref.workflow_name,
            "symbol": workflow_ref.symbol,
            "source_sha256": workflow_ref.source_sha256,
            "inputs": inputs,
            "child_workflow_id": child_workflow_id,
            "child_key": child_key_part,
            "group": child_group,
        }
        if self._last_event("ChildWorkflowRequested", event_key) is None:
            with self.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._raise_if_cancelled_in_connection(con)
                inserted = self.engine._append_event(
                    con,
                    self.workflow_id,
                    "ChildWorkflowRequested",
                    key=event_key,
                    payload=request_payload,
                    idempotency_key=f"child-requested:{event_key}",
                    ignore_duplicate=True,
                )
                if inserted:
                    self.engine._insert_command_row(con, self.workflow_id, "start_child_workflow", event_key, request_payload)

        if block:
            raise WorkflowWaiting(event_key)
        return PendingStep(event_key)

    async def _require_generated_workflow_approval(self, workflow_ref: Workflow) -> None:
        if not workflow_ref.approval_required:
            return
        approval_key = workflow_ref.approval_key or f"generated-workflow:{workflow_ref.source_sha256}:{workflow_ref.symbol}"
        provenance = workflow_ref.provenance or {}
        decision = await self.approval.request(
            "Approve generated Python workflow before running it as a child workflow.",
            key=approval_key,
            artifact={
                "kind": "generated_workflow.approval.v1",
                "workflow_name": workflow_ref.workflow_name,
                "symbol": workflow_ref.symbol,
                "source_sha256": workflow_ref.source_sha256,
                "source": workflow_ref.source,
                "runner_provenance": provenance.get("runner_provenance"),
                "agent_request": provenance.get("request"),
                "agent_response": provenance.get("response"),
            },
            allowed=["approve", "reject"],
        )
        if decision.get("action") != "approve":
            raise ValueError(f"generated workflow approval {approval_key} was not approved")

    async def map_workflow(
        self,
        workflow_ref: Workflow,
        items: List[Any],
        *,
        key_fn: Callable[[Any], str],
        concurrency: int | None = None,
    ) -> List[Any]:
        map_index = self._gather_call_count
        self._gather_call_count += 1
        group = f"map:{map_index}"
        results: List[Any] = []
        pending: List[str] = []
        seen_keys: set[str] = set()
        for item in items:
            raw_child_key = str(key_fn(item))
            child_key = _safe_child_key(raw_child_key)
            if child_key in seen_keys:
                raise ValueError(f"duplicate child workflow key in map_workflow: {child_key}")
            seen_keys.add(child_key)
            result = await self.start_child(workflow_ref, item, key=raw_child_key, group=group, block=False)
            if isinstance(result, PendingStep):
                pending.append(result.key)
                results.append(None)
            else:
                results.append(result)

        if pending:
            wait_key = f"child-gather:{group}"
            with self.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._raise_if_cancelled_in_connection(con)
                self.engine._append_event(
                    con,
                    self.workflow_id,
                    "ChildWorkflowGatherWaiting",
                    key=wait_key,
                    payload={"pending": pending, "concurrency": concurrency},
                    idempotency_key=f"child-gather-waiting:{wait_key}",
                    ignore_duplicate=True,
                )
            raise WorkflowWaiting(wait_key)

        return results

    def _child_call_count_for(self, group: str) -> int:
        attr = "_child_call_counts"
        counts = getattr(self, attr, None)
        if counts is None:
            counts = {}
            setattr(self, attr, counts)
        call_index = counts.get(group, 0)
        counts[group] = call_index + 1
        return call_index

    async def wait_for(self, signal_type: str, *, key: str) -> Any:
        self._raise_if_cancelled()
        wait_key = f"signal:{signal_type}:{key}"
        signal = self._last_event("SignalReceived", wait_key)
        if signal is not None:
            return signal["payload"]

        request_key = f"wait:{signal_type}:{key}"
        if self._last_event("WaitRequested", request_key) is None:
            with self.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._raise_if_cancelled_in_connection(con)
                self.engine._append_event(
                    con,
                    self.workflow_id,
                    "WaitRequested",
                    key=request_key,
                    payload={"signal_type": signal_type, "key": key},
                    idempotency_key=f"requested:{request_key}",
                    ignore_duplicate=True,
                )

        raise WorkflowWaiting(wait_key)

    async def approve(
        self,
        prompt: str,
        *,
        key: str | None = None,
        artifact: Any = None,
        allowed: Optional[List[str]] = None,
        timeout: Optional[str] = None,
        feedback_loop: bool = False,
    ) -> ApprovalDecision:
        """Request an approval with the ergonomic ctx.approve(...) primitive."""

        return await self.approval.request(
            prompt,
            key=key,
            artifact=artifact,
            allowed=allowed,
            timeout=timeout,
            feedback_loop=feedback_loop,
        )

    async def _request_agent_work(
        self,
        prompt: str,
        *,
        key: str,
        artifact: Any = None,
        assignee: str | None = None,
        instructions: str | None = None,
        block: bool = True,
        public_name: str | None = None,
        public_label: str | None = None,
        name_source: str | None = None,
    ) -> Any:
        """Private substrate for agent(...): record durable agent work and wait for completion."""

        self._raise_if_cancelled()
        agent_key = _safe_approval_key(key, prefix="")
        event_key = f"agent:{agent_key}"
        signal_type = "agent.completed"
        completed = self._last_event("SignalReceived", f"signal:{signal_type}:{agent_key}")
        if completed is not None:
            return completed["payload"]

        payload = {
            "prompt": prompt,
            "key": agent_key,
            "artifact": artifact,
            "assignee": assignee,
            "instructions": instructions,
            "signal_type": signal_type,
            "public_name": public_name or assignee or agent_key,
            "public_label": public_label or public_name or assignee or agent_key,
            "name_source": name_source or "explicit",
        }
        if self._last_event("AgentRequested", event_key) is None:
            with self.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._raise_if_cancelled_in_connection(con)
                self.engine._append_step_requested(
                    con,
                    self.workflow_id,
                    agent_key,
                    completion_mode="agent",
                    step_type="agent",
                    label=public_label or public_name or assignee or prompt,
                    payload=payload,
                )
                inserted = self.engine._append_event(
                    con,
                    self.workflow_id,
                    "AgentRequested",
                    key=event_key,
                    payload=payload,
                    idempotency_key=f"agent-requested:{agent_key}",
                    ignore_duplicate=True,
                )
                if inserted:
                    self.engine._insert_command_row(con, self.workflow_id, "external_agent", event_key, payload)
        if not block:
            return PendingStep(agent_key)
        return await self.wait_for(signal_type, key=agent_key)

    async def _request_human_input(
        self,
        prompt: str,
        *,
        key: str,
        artifact: Any = None,
        schema: str = "json",
        schema_descriptor: Optional[dict[str, Any]] = None,
        timeout: Optional[str] = None,
        block: bool = True,
    ) -> Any:
        """Private substrate for ask(...): request typed human/operator input."""

        return await self.approval.request_input(
            prompt,
            key=key,
            artifact=artifact,
            schema=schema,
            schema_descriptor=schema_descriptor,
            timeout=timeout,
            block=block,
        )

    def _last_event(self, event_type: str, key: str) -> Optional[Any]:
        with self.engine._connect() as con:
            row = con.execute(
                """
                SELECT payload_json FROM workflow_events
                WHERE workflow_id = ? AND type = ? AND key = ?
                ORDER BY seq DESC LIMIT 1
                """,
                (self.workflow_id, event_type, key),
            ).fetchone()
        if row is None:
            return None
        return JsonCodec.loads(row["payload_json"])


class ApprovalClient:
    def __init__(self, ctx: WorkflowContext):
        self.ctx = ctx

    def _key_for_request(self, prompt: str, key: str | None, *, feedback_loop: bool = False) -> str:
        if key is not None and not feedback_loop:
            return _safe_approval_key(key, prefix="")
        base = _safe_approval_key(key, prefix="") if key is not None else _safe_approval_key(prompt, prefix="approve")
        call_index = self.ctx._approval_call_counts.get(base, 0)
        self.ctx._approval_call_counts[base] = call_index + 1
        if feedback_loop and call_index > 0:
            return f"{base}_retry_{call_index}"
        if key is None and call_index > 0:
            return f"{base}_{call_index}"
        return base

    def _payload(
        self,
        prompt: str,
        *,
        key: str,
        artifact: Any = None,
        allowed: Optional[List[str]] = None,
        timeout: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "prompt": prompt,
            "key": key,
            "artifact": artifact,
            "allowed": allowed or ["approve", "reject"],
            "timeout": timeout,
        }

    def _emit_request_if_missing(self, con: sqlite3.Connection, *, key: str, payload: Dict[str, Any]) -> None:
        event_key = f"approval:{key}"
        request_kind = payload.get("kind")
        is_human_input = request_kind in {"human_input.request.v1", "operator.request.v1"}
        self.ctx.engine._append_step_requested(
            con,
            self.ctx.workflow_id,
            key,
            completion_mode="operator" if is_human_input else "approval",
            step_type="operator" if is_human_input else "approval",
            label=str(payload.get("prompt") or key),
            payload=payload,
        )
        inserted = self.ctx.engine._append_event(
            con,
            self.ctx.workflow_id,
            "ApprovalRequested",
            key=event_key,
            payload=payload,
            idempotency_key=f"approval-requested:{key}",
            ignore_duplicate=True,
        )
        if inserted:
            self.ctx.engine._insert_command_row(con, self.ctx.workflow_id, "notify_approval", event_key, payload)

    def _decision_event(self, key: str) -> Optional[Any]:
        return self.ctx._last_event("SignalReceived", f"signal:approval.decision:{key}")

    def _operator_response_event(self, key: str) -> Optional[Any]:
        return self.ctx._last_event("SignalReceived", f"signal:operator.response:{key}")

    def _validate_decision(self, *, key: str, allowed: List[str], decision_event: Dict[str, Any]) -> ApprovalDecision:
        decision = decision_event["payload"]
        if decision.get("action") not in allowed:
            raise ValueError(f"approval {key} action is not allowed: {decision.get('action')}")
        source = _validate_approval_source(key, decision, decision_event.get("source"))
        return ApprovalDecision(
            action=str(decision.get("action") or ""),
            by=decision.get("by") if isinstance(decision.get("by"), str) and decision.get("by") else None,
            source=source,
            note=decision.get("note"),
            reason=decision.get("reason"),
            message=decision.get("message"),
            comment=decision.get("comment"),
            direct_feedback=decision.get("feedback"),
        )

    async def request(
        self,
        prompt: str,
        *,
        key: str | None = None,
        artifact: Any = None,
        allowed: Optional[List[str]] = None,
        timeout: Optional[str] = None,
        feedback_loop: bool = False,
    ) -> ApprovalDecision:
        self.ctx._raise_if_cancelled()
        key = self._key_for_request(prompt, key, feedback_loop=feedback_loop)
        event_key = f"approval:{key}"
        allowed_values = allowed or ["approve", "reject"]
        if self.ctx._last_event("ApprovalRequested", event_key) is None:
            payload = self._payload(
                prompt,
                key=key,
                artifact=artifact,
                allowed=allowed_values,
                timeout=timeout,
            )
            with self.ctx.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self.ctx._raise_if_cancelled_in_connection(con)
                self._emit_request_if_missing(con, key=key, payload=payload)

        decision_event = self._decision_event(key)
        if decision_event is None:
            return await self.ctx.wait_for("approval.decision", key=key)

        return self._validate_decision(key=key, allowed=allowed_values, decision_event=decision_event)

    async def request_input(
        self,
        prompt: str,
        *,
        key: str,
        artifact: Any = None,
        schema: str = "json",
        schema_descriptor: Optional[dict[str, Any]] = None,
        timeout: Optional[str] = None,
        block: bool = True,
    ) -> Any:
        """Request typed human/operator input using the approval decision substrate."""

        self.ctx._raise_if_cancelled()
        key = self._key_for_request(prompt, key)
        event_key = f"approval:{key}"
        payload = {
            "kind": "operator.request.v1",
            "prompt": prompt,
            "key": key,
            "artifact": artifact,
            "schema": schema,
            "schema_descriptor": schema_descriptor,
            "allowed": None,
            "timeout": timeout,
        }
        if self.ctx._last_event("ApprovalRequested", event_key) is None:
            with self.ctx.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self.ctx._raise_if_cancelled_in_connection(con)
                self._emit_request_if_missing(con, key=key, payload=payload)

        decision_event = self._operator_response_event(key)
        if decision_event is None:
            if not block:
                return PendingStep(key)
            return await self.ctx.wait_for("operator.response", key=key)

        raw_payload = decision_event["payload"]
        _validate_operator_source(key, raw_payload if isinstance(raw_payload, dict) else {}, decision_event.get("source"))
        return raw_payload

    async def request_many(
        self,
        requests: List[Dict[str, Any]],
        *,
        allowed: Optional[List[str]] = None,
        timeout: Optional[str] = None,
        feedback_loop: bool = False,
    ) -> List[Dict[str, Any]]:
        """Emit every approval request before waiting for any decision.

        Approval semantics remain atomic: each request has its own durable key,
        artifact, decision signal, and provenance. This method only changes the
        operator experience by making all pending cards visible at once.
        """

        self.ctx._raise_if_cancelled()
        normalized: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        for index, request in enumerate(requests):
            raw_key = request.get("key")
            key = self._key_for_request(
                str(request.get("prompt") or f"approval {index}"),
                str(raw_key) if raw_key else None,
                feedback_loop=feedback_loop,
            )
            if not key:
                raise ValueError("approval request_many entries require key or prompt")
            if key in seen_keys:
                raise ValueError(f"duplicate approval key in request_many: {key}")
            seen_keys.add(key)
            request_allowed = list(request.get("allowed") or allowed or ["approve", "reject"])
            normalized.append(
                {
                    "key": key,
                    "allowed": request_allowed,
                    "payload": self._payload(
                        str(request.get("prompt") or "Review approval?"),
                        key=key,
                        artifact=request.get("artifact"),
                        allowed=request_allowed,
                        timeout=request.get("timeout") or timeout,
                    ),
                }
            )

        with self.ctx.engine._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self.ctx._raise_if_cancelled_in_connection(con)
            for item in normalized:
                self._emit_request_if_missing(con, key=item["key"], payload=item["payload"])
            for item in normalized:
                if self._decision_event(item["key"]) is not None:
                    continue
                wait_key = f"wait:approval.decision:{item['key']}"
                self.ctx.engine._append_event(
                    con,
                    self.ctx.workflow_id,
                    "WaitRequested",
                    key=wait_key,
                    payload={"signal_type": "approval.decision", "key": item["key"]},
                    idempotency_key=f"requested:{wait_key}",
                    ignore_duplicate=True,
                )

        decisions: List[Dict[str, Any]] = []
        missing: List[str] = []
        for item in normalized:
            decision_event = self._decision_event(item["key"])
            if decision_event is None:
                missing.append(item["key"])
                continue
            decision = self._validate_decision(
                key=item["key"],
                allowed=item["allowed"],
                decision_event=decision_event,
            )
            decisions.append({"key": item["key"], **decision.to_dict()})

        if missing:
            raise WorkflowWaiting(f"signals:approval.decision:{','.join(missing)}")
        return decisions


_OPERATOR_SOURCE_ALLOWLIST = ("channel", "message_url", "message_id", "event_id")


def _normalize_approval_decision_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    normalized: Dict[str, Any] = {}
    for key in ("action", "by"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            normalized[key] = value
    for key in ("feedback", "note", "reason", "message", "comment"):
        value = payload.get(key)
        if value is not None:
            normalized[key] = str(value)
    return normalized


def _normalize_operator_source(source: Any) -> Dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    normalized: Dict[str, Any] = {}
    for key in _OPERATOR_SOURCE_ALLOWLIST:
        value = source.get(key)
        if not isinstance(value, str) or not value:
            continue
        normalized[key] = value
    return normalized


def _sanitize_approval_decision_payload(payload: Any) -> Any:
    return _normalize_approval_decision_payload(payload)


def _sanitize_approval_source(source: Any) -> Dict[str, Any]:
    return _normalize_operator_source(source)


def _sanitize_approval_text(value: str) -> str:
    return value


def _validate_operator_source(
    key: str,
    decision: Dict[str, Any],
    source: Any,
) -> Optional[Dict[str, Any]]:
    """Validate decision provenance without request-time identities."""

    if not isinstance(source, dict):
        raise ValueError(f"operator step {key} requires decision provenance")
    if not source.get("channel") or not any(source.get(field) for field in ("message_url", "message_id", "event_id")):
        raise ValueError(f"operator step {key} requires external decision provenance")
    return source


def _validate_approval_source(key: str, decision: Dict[str, Any], source: Any) -> Optional[Dict[str, Any]]:
    return _validate_operator_source(key, decision, source)


@dataclass(frozen=True)
class StepExecutionContext:
    engine: WorkflowEngine
    workflow_id: str
    step_key: str










def _parent_wait_key_for_child_wait(
    *,
    parent_row: sqlite3.Row | None,
    child_event_key: str,
    child_group: Any,
) -> str:
    group = str(child_group or "")
    existing_wait = parent_row["waiting_on"] if parent_row is not None and "waiting_on" in parent_row.keys() else None
    if group.startswith("map:"):
        map_parts = group.split(":", 2)
        gather_group = ":".join(map_parts[:2]) if len(map_parts) >= 2 else group
        gather_key = f"child-gather:{gather_group}"
        if existing_wait == gather_key:
            return str(existing_wait)
        return gather_key
    return child_event_key






def _step_request_fingerprint(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    args = payload.get("args")
    if not isinstance(args, list) or not args:
        return None
    request = args[0]
    if not isinstance(request, dict):
        return None
    fingerprint = request.get("fingerprint")
    return str(fingerprint) if fingerprint is not None else None


def _validate_step_request_fingerprint(step_key: str, stored_payload: Any, current_payload: Any) -> None:
    stored = _step_request_fingerprint(stored_payload)
    current = _step_request_fingerprint(current_payload)
    if stored is None or current is None:
        return
    if stored != current:
        raise ValueError(f"step {step_key} fingerprint changed; refusing to replay saved output")










def _to_jsonable(value: Any) -> Any:
    return to_json_value(value)


def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("__hermes_type__") == "Workflow":
            return Workflow.from_json(value)
        if value.get("__hermes_type__") == "Artifact":
            from .artifacts import Artifact

            return Artifact.from_json(value)
        return {key: _from_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_jsonable(item) for item in value]
    return value


def _safe_approval_key(value: Any, *, prefix: str = "approve") -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("approval key source must be non-empty")
    if not prefix and all(char.isalnum() or char in "._-:" for char in text):
        return text
    lowered = text.lower()
    safe = "".join(char if char.isalnum() else "_" for char in lowered)
    safe = "_".join(part for part in safe.split("_") if part)
    if not safe:
        safe = prefix
    if not safe.startswith(("approve", "approval", "handoff")) and prefix:
        safe = f"{prefix}_{safe}"
    digest = _hash_text(text)[:8]
    if len(safe) <= 64 and safe == lowered.replace(" ", "_").strip("_"):
        return safe
    return f"{safe[:56].strip('_') or prefix}_{digest}"


def _safe_child_key(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("child workflow key must be non-empty")
    safe = "".join(char if char.isalnum() or char in "._-:" else "_" for char in text)
    digest = _hash_text(text)[:12]
    if safe == text and len(safe) <= 80:
        return safe
    prefix = safe[:80].strip("._-:") or "key"
    return f"{prefix}-{digest}"


def _workflow_child_group(workflow_ref: Workflow, *, group: str | None) -> str:
    symbol = _safe_child_key(workflow_ref.symbol)
    digest = workflow_ref.source_sha256[:12]
    if group is None:
        return f"{symbol}:{digest}"
    base = _safe_child_key(group)
    return f"{base}:{symbol}:{digest}"


def _hash_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _error_from_exception(exc: Exception) -> Dict[str, Any]:
    error: Dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    details = getattr(exc, "details", None)
    if details is not None:
        error["details"] = details
    return error


def _format_error(error: Any) -> Optional[str]:
    if error is None:
        return None
    if isinstance(error, dict):
        error_type = error.get("type")
        message = error.get("message")
        if error_type and message:
            return f"{error_type}: {message}"
    return JsonCodec.dumps(error)




def _now() -> int:
    return int(time.time())


def _claim_token_hash(claim_token: str | None) -> str | None:
    if not claim_token:
        return None
    return hashlib.sha256(claim_token.encode("utf-8")).hexdigest()


def _command_value(command: Union[sqlite3.Row, Dict[str, Any]], key: str) -> Any:
    try:
        return command[key]
    except (KeyError, IndexError):
        return None


def _worker_payload(row: sqlite3.Row, *, now: int | None = None) -> Dict[str, Any]:
    now = _now() if now is None else now
    raw_status = str(row["status"] or "running")
    heartbeat_expires_at = row["heartbeat_expires_at"]
    active = raw_status != "stopped" and isinstance(heartbeat_expires_at, int) and heartbeat_expires_at > now
    projected_status = "stopped" if raw_status == "stopped" else ("running" if active else "stale")
    environment = {
        "hostname": row["hostname"],
        "pid": row["pid"],
        "cwd": row["cwd"],
        "python_executable": row["python_executable"],
        "python_version": row["python_version"],
        "platform": row["platform"],
        "hermes_version": row["hermes_version"],
        "agent_runner_enabled": bool(row["agent_runner_enabled"]),
    }
    metadata = JsonCodec.loads(row["metadata_json"])
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "worker_instance_id": row["worker_instance_id"],
        "worker_id": row["worker_id"],
        "status": projected_status,
        "active": active,
        "first_seen_at": row["first_seen_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "heartbeat_expires_at": heartbeat_expires_at,
        "heartbeat_age_seconds": max(0, now - int(row["last_heartbeat_at"])),
        "environment": environment,
        "metadata": metadata,
    }


def _safe_worker_metadata(raw_metadata: Any) -> Dict[str, Any]:
    """Persist only runner-owned, public-safe worker heartbeat metadata.

    Callers may accidentally pass arbitrary/user-supplied metadata. Heartbeats are
    surfaced in status/dashboard output, so keep this to a small allowlist used by
    the resident worker service and drop everything else.
    """

    if not isinstance(raw_metadata, dict):
        return {}
    metadata: Dict[str, Any] = {}
    for key in ("source_db_name", "source_db_path"):
        value = raw_metadata.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    allowed_count = raw_metadata.get("allowed_workflow_refs_count")
    if isinstance(allowed_count, int) and allowed_count >= 0:
        metadata["allowed_workflow_refs_count"] = allowed_count
    package_fingerprint = raw_metadata.get("package_fingerprint")
    if isinstance(package_fingerprint, dict):
        safe_fingerprint = {
            str(key): value
            for key, value in package_fingerprint.items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool, type(None)))
        }
        if safe_fingerprint:
            metadata["package_fingerprint"] = safe_fingerprint
    active_command = raw_metadata.get("active_command")
    if isinstance(active_command, dict):
        safe_command: Dict[str, Any] = {}
        for key in ("command_id", "command_type", "command_key", "workflow_id"):
            value = active_command.get(key)
            if isinstance(value, (str, int)) and value != "":
                safe_command[key] = value
        if safe_command:
            metadata["active_command"] = safe_command
    return metadata


def _lease_seconds_from_row(row: sqlite3.Row) -> int:
    expires_at = row["lease_expires_at"]
    updated_at = row["updated_at"]
    if expires_at is None or updated_at is None:
        return 0
    return max(0, int(expires_at) - int(updated_at))


def _lease_seconds_from_command(command: Union[sqlite3.Row, Dict[str, Any]]) -> int:
    if isinstance(command, dict) and command.get("lease_seconds") is not None:
        return max(0, int(command["lease_seconds"]))
    try:
        expires_at = command["lease_expires_at"]
        updated_at = command["updated_at"]
    except (KeyError, IndexError):
        return 0
    if expires_at is None or updated_at is None:
        return 0
    return max(0, int(expires_at) - int(updated_at))


def _call_step_body(body: Callable[..., Any], step_context: StepExecutionContext, *args: Any, **kwargs: Any) -> Any:
    try:
        signature = inspect.signature(body)
    except (TypeError, ValueError):
        return body(step_context, *args, **kwargs)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    required_positional = [
        parameter
        for parameter in positional
        if parameter.default is inspect.Parameter.empty and parameter.name not in kwargs
    ]
    should_inject_context = False
    if positional and len(args) < len(required_positional):
        should_inject_context = True
    type_hints = _safe_step_type_hints(body)
    if should_inject_context:
        coerced_args, coerced_kwargs = _coerce_step_call(signature, type_hints, args, kwargs, skip_positional=1)
        result = body(step_context, *coerced_args, **coerced_kwargs)
    else:
        coerced_args, coerced_kwargs = _coerce_step_call(signature, type_hints, args, kwargs, skip_positional=0)
        result = body(*coerced_args, **coerced_kwargs)
    return _coerce_step_return_maybe_async(result, _step_return_annotation(body, type_hints=type_hints))


def _coerce_step_call(
    signature: inspect.Signature,
    type_hints: Dict[str, Any],
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
    *,
    skip_positional: int,
) -> tuple[tuple[Any, ...], Dict[str, Any]]:
    parameters = list(signature.parameters.values())
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ][skip_positional:]
    coerced_args = [
        _coerce_step_value(
            arg,
            type_hints.get(positional[index].name, positional[index].annotation) if index < len(positional) else inspect.Signature.empty,
        )
        for index, arg in enumerate(args)
    ]
    by_name = {parameter.name: parameter for parameter in parameters}
    coerced_kwargs = {
        key: _coerce_step_value(value, type_hints.get(key, by_name[key].annotation) if key in by_name else inspect.Signature.empty)
        for key, value in kwargs.items()
    }
    return tuple(coerced_args), coerced_kwargs


def _coerce_step_value(value: Any, annotation: Any) -> Any:
    if annotation is inspect.Signature.empty:
        return value
    return coerce_workflow_input(value, annotation)


def _safe_step_type_hints(body: Callable[..., Any]) -> Dict[str, Any]:
    try:
        return get_type_hints(body, include_extras=True)
    except Exception:
        return dict(getattr(body, "__annotations__", {}) or {})


def _step_return_annotation(body: Callable[..., Any], *, type_hints: Dict[str, Any] | None = None) -> Any:
    if type_hints is None:
        type_hints = _safe_step_type_hints(body)
    if "return" in type_hints:
        return type_hints["return"]
    try:
        signature = inspect.signature(body)
    except (TypeError, ValueError):
        return inspect.Signature.empty
    return signature.return_annotation


async def _coerce_step_return_async(value: Any, annotation: Any) -> Any:
    return _coerce_step_value(await value, annotation)


def _coerce_step_return_maybe_async(value: Any, annotation: Any) -> Any:
    if annotation is inspect.Signature.empty:
        return value
    if inspect.isawaitable(value):
        return _coerce_step_return_async(value, annotation)
    return _coerce_step_value(value, annotation)


def _coerce_completed_step_output(step_name: str, output: Any) -> Any:
    try:
        from .decorators import get_step_body

        body = get_step_body(step_name)
    except Exception:
        return output
    return _coerce_step_value(output, _step_return_annotation(body))


def _run_maybe_async(value: Any) -> Any:
    if inspect.isawaitable(value):
        try:
            return asyncio.run(value)
        except RuntimeError as exc:
            if "asyncio.run() cannot be called from a running event loop" in str(exc):
                raise RuntimeError("WorkflowEngine v0/v1 must be called outside an active event loop") from exc
            raise
    return value


_WORKFLOW_REGISTRY: Dict[str, Callable[..., Any]] = {}


def register_workflow(fn: Callable[..., Any]) -> Callable[..., Any]:
    _WORKFLOW_REGISTRY[getattr(fn, "__workflow_name__", fn.__name__)] = fn
    return fn

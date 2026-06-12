from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sqlite3
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

from .approvals import ApprovalDecision, ApprovalDecisionInput, ApprovalReceipt, ApprovalView
from .workflow_values import Workflow


TERMINAL_WORKFLOW_STATUSES = {"completed", "failed", "cancelled"}


class WorkflowWaiting(Exception):
    def __init__(self, waiting_on: str):
        super().__init__(waiting_on)
        self.waiting_on = waiting_on


class WorkflowCancelled(Exception):
    """Internal control-flow signal: stop decider work after cancellation."""


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
        return self._run_decider(workflow_id, workflow_fn)

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

        workflow_fn = self._workflow_fn_for_instance(instance)
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
                UPDATE workflow_commands_outbox
                SET status = 'completed'
                WHERE workflow_id = ? AND key = ? AND type = 'run_step' AND status != 'cancelled'
                """,
                (workflow_id, step_key),
            )
            con.execute(
                """
                UPDATE workflow_instances
                SET status = 'running', updated_at = ?
                WHERE id = ? AND status != 'cancelled'
                """,
                (_now(), workflow_id),
            )
        return self._run_decider(workflow_id, workflow_fn)

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
        if signal_type == "approval.decision":
            payload = _sanitize_approval_decision_payload(payload)
            source = _sanitize_approval_source(source)
        dedupe = idempotency_key or f"signal:{signal_type}:{key}:{JsonCodec.dumps(payload)}"
        if instance["status"] in TERMINAL_WORKFLOW_STATUSES and signal_type != "approval.decision":
            return self._result_from_instance(workflow_id)

        workflow_fn = None if instance["status"] in TERMINAL_WORKFLOW_STATUSES else self._workflow_fn_for_instance(instance)
        inserted = False
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if row["status"] in TERMINAL_WORKFLOW_STATUSES:
                if signal_type == "approval.decision":
                    self._validate_approval_decision_signal(
                        workflow_id,
                        key,
                        payload,
                        source,
                        dedupe,
                        con=con,
                        require_existing=row["status"] == "completed",
                    )
                return self._result_from_row(row)
            if signal_type == "approval.decision":
                self._validate_approval_decision_signal(workflow_id, key, payload, source, dedupe, con=con)

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
                if signal_type == "approval.decision":
                    con.execute(
                        """
                        UPDATE workflow_commands_outbox
                        SET status = 'completed', lease_expires_at = NULL, updated_at = ?
                        WHERE workflow_id = ? AND type = 'notify_approval' AND key = ? AND status != 'cancelled'
                        """,
                        (_now(), workflow_id, f"approval:{key}"),
                    )
                elif signal_type == "handoff.completed":
                    con.execute(
                        """
                        UPDATE workflow_commands_outbox
                        SET status = 'completed', lease_expires_at = NULL, updated_at = ?
                        WHERE workflow_id = ? AND type = 'external_handoff' AND key = ? AND status != 'cancelled'
                        """,
                        (_now(), workflow_id, f"handoff:{key}"),
                    )
        if inserted:
            assert workflow_fn is not None
            result = self._run_decider(workflow_id, workflow_fn)
        else:
            result = self._result_from_instance(workflow_id)
        return self.drain(workflow_id, initial=result)

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
        payload: dict[str, Any] = {"action": decision.action, "by": decision.by}
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
                    SET status = 'completed', lease_expires_at = NULL, updated_at = ?
                    WHERE workflow_id = ? AND type = 'notify_approval' AND key = ? AND status != 'cancelled'
                    """,
                    (_now(), decision.workflow_id, f"approval:{decision.key}"),
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
                    approver=summary.get("approver"),
                    allowed=list(summary.get("allowed") or ["approve", "reject"]),
                    authority=summary.get("authority"),
                    timeout=summary.get("timeout"),
                    waiting_on=row["waiting_on"],
                    requested_seq=summary.get("requested_seq"),
                    source=summary.get("source"),
                    decision=summary.get("decision"),
                    diagnostics=diagnostics_by_approval_key.get(key, []),
                )
            )
        return views

    def _validate_approval_decision_signal(
        self,
        workflow_id: str,
        key: str,
        payload: Any,
        source: Any,
        idempotency_key: str,
        *,
        con: sqlite3.Connection | None = None,
        require_existing: bool = False,
    ) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"approval {key} decision payload must be an object")

        event_key = f"approval:{key}"
        if con is None:
            with self._connect() as read_con:
                self._validate_approval_decision_signal(
                    workflow_id,
                    key,
                    payload,
                    source,
                    idempotency_key,
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
            (workflow_id, f"signal:approval.decision:{key}"),
        ).fetchone()

        if existing_decision is not None:
            if existing_decision["idempotency_key"] == idempotency_key:
                existing_payload = JsonCodec.loads(existing_decision["payload_json"])
                expected_payload = {"signal_type": "approval.decision", "key": key, "payload": payload, "source": source}
                if existing_payload == expected_payload:
                    return
                raise ValueError(f"approval {key} idempotency key was reused with a different decision")
            raise ValueError(f"approval {key} already has a recorded decision")

        if require_existing:
            raise ValueError(f"approval {key} has no recorded decision to replay")

        if row is None:
            raise ValueError(f"approval {key} has no matching ApprovalRequested event")

        request_payload = JsonCodec.loads(row["payload_json"])
        if not isinstance(request_payload, dict):
            raise ValueError(f"approval {key} has invalid ApprovalRequested payload")

        allowed = request_payload.get("allowed") or ["approve", "reject"]
        if payload.get("action") not in allowed:
            raise ValueError(f"approval {key} action is not allowed: {payload.get('action')}")

        _validate_approval_source(
            key,
            str(request_payload.get("approver") or "human"),
            payload,
            source,
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
                SET status = 'cancelled', lease_expires_at = NULL, updated_at = ?
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
        lease_seconds: int = 30,
        command_type: Optional[str] = "run_step",
    ) -> Optional[Dict[str, Any]]:
        """Claim one pending or lease-expired command for a worker."""

        self._ensure_writable("claim workflow commands")

        now = _now()
        type_clause = "AND c.type = ?" if command_type is not None else "AND c.type IN ('run_step', 'start_child_workflow')"
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
            lease_expires_at = now + lease_seconds
            con.execute(
                """
                UPDATE workflow_commands_outbox
                SET status = 'running', claimed_by = ?, lease_expires_at = ?, attempts = ?, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, lease_expires_at, attempts, now, row["id"]),
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
                    "attempt": attempts,
                    "lease_expires_at": lease_expires_at,
                },
                idempotency_key=f"claimed:{row['id']}:{attempts}",
                ignore_duplicate=True,
            )
            claimed = con.execute("SELECT * FROM workflow_commands_outbox WHERE id = ?", (row["id"],)).fetchone()

        return self._command_payload(claimed)

    def worker_once(self, workflow_id: str, *, worker_id: str, lease_seconds: int = 30) -> RunResult:
        command = self.claim_command(workflow_id, worker_id=worker_id, lease_seconds=lease_seconds, command_type=None)
        if command is None:
            return self._result_from_instance(workflow_id)
        return self._execute_command(workflow_id, command)

    def worker_until_idle(
        self,
        workflow_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        max_commands: Optional[int] = None,
    ) -> RunResult:
        result = self._result_from_instance(workflow_id)
        executed = 0
        while max_commands is None or executed < max_commands:
            command = self.claim_command(workflow_id, worker_id=worker_id, lease_seconds=lease_seconds, command_type=None)
            if command is None:
                return self._result_from_instance(workflow_id)
            result = self._execute_command(workflow_id, command)
            executed += 1
            if result.status in {"completed", "failed"}:
                return result
        return result

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
        return [
            {
                "seq": row["seq"],
                "type": row["type"],
                "key": row["key"],
                "payload": JsonCodec.loads(row["payload_json"]),
                "idempotency_key": row["idempotency_key"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

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
            parent = self._instance(workflow_id)
            return self._run_decider(workflow_id, self._workflow_fn_for_instance(parent))
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
                    SET status = 'running', waiting_on = NULL, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (_now(), workflow_id),
                )
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
        return self._run_decider(workflow_id, self._workflow_fn_for_instance(parent))

    def list_workflows(self, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT id, workflow_name, workflow_ref, status, waiting_on
            FROM workflow_instances
        """
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC, created_at DESC, id ASC"

        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
        return [
            self._list_workflow_payload(row)
            for row in rows
        ]

    def _list_workflow_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload = {
            "workflow_id": row["id"],
            "workflow_name": row["workflow_name"],
            "status": row["status"],
            "waiting_on": row["waiting_on"],
        }
        if row["workflow_ref"] is not None:
            payload["workflow_ref"] = row["workflow_ref"]
        terminal_reason = self._terminal_reason(row["id"])
        if terminal_reason is not None:
            payload["terminal_reason"] = terminal_reason
        return payload

    def outbox_commands(
        self,
        *,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM workflow_commands_outbox"
        clauses = []
        params: list[Any] = []
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY workflow_id ASC, id ASC"

        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
        return self._enrich_command_payloads([self._command_payload(row) for row in rows])

    def workflow_status(
        self,
        workflow_id: str,
        *,
        recent_events: int = 20,
        command_history: Optional[str] = None,
        command_limit: int = 20,
        command_payload_chars: int = 500,
    ) -> Dict[str, Any]:
        row = self._instance(workflow_id)
        events = self.events(workflow_id)
        pending_commands = self._active_commands(workflow_id)
        child_workflows = self._child_workflow_summaries(row, events)
        status = {
            "workflow_id": row["id"],
            "workflow_name": row["workflow_name"],
            "workflow_ref": row["workflow_ref"],
            "status": row["status"],
            "waiting_on": row["waiting_on"],
            "result": JsonCodec.loads(row["result_json"]),
            "error": _format_error(JsonCodec.loads(row["error_json"])),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "terminal_reason": self._terminal_reason(workflow_id),
            "event_count": len(events),
            "events": events[-recent_events:],
            "pending_commands": pending_commands,
            "diagnostics": self._command_diagnostics(pending_commands),
            "child_workflows": child_workflows,
            "approvals": self._approval_summaries(events),
            "steps": self._step_summaries(events),
        }
        if command_history is not None:
            history, truncated = self._command_history(
                workflow_id,
                mode=command_history,
                limit=command_limit,
                payload_chars=command_payload_chars,
            )
            status["command_history_mode"] = command_history
            status["command_history_truncated"] = truncated
            status["command_history"] = history
        return status

    def _command_history(
        self,
        workflow_id: str,
        *,
        mode: str,
        limit: int,
        payload_chars: int,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        if mode not in {"failed", "recent", "all"}:
            raise ValueError("command_history mode must be one of: failed, recent, all")
        if limit < 1:
            raise ValueError("command_limit must be positive")
        if payload_chars < 1:
            raise ValueError("command_payload_chars must be positive")

        where = "WHERE workflow_id = ?"
        params: list[Any] = [workflow_id]
        if mode == "failed":
            where += " AND status = 'failed'"

        order = "ORDER BY COALESCE(updated_at, id) DESC, id DESC"
        if mode == "all":
            order = "ORDER BY id ASC"

        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT *
                FROM workflow_commands_outbox
                {where}
                {order}
                LIMIT ?
                """,
                (*params, limit + 1),
            ).fetchall()

        truncated = len(rows) > limit
        commands = self._enrich_command_payloads([self._command_payload(row) for row in rows[:limit]])
        return [_history_command_payload(command, payload_chars=payload_chars) for command in commands], truncated

    def _terminal_reason(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT payload_json
                FROM workflow_events
                WHERE workflow_id = ? AND type = 'WorkflowCancelled'
                ORDER BY seq DESC LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
        if row is None:
            return None
        payload = JsonCodec.loads(row["payload_json"])
        return payload if isinstance(payload, dict) else None

    def _approval_summaries(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        decisions: dict[str, Dict[str, Any]] = {}
        for event in events:
            if event["type"] != "SignalReceived":
                continue
            payload = event["payload"] or {}
            if payload.get("signal_type") != "approval.decision":
                continue
            decisions[payload.get("key")] = payload

        approvals: list[Dict[str, Any]] = []
        for event in events:
            if event["type"] != "ApprovalRequested":
                continue
            payload = event["payload"] or {}
            key = payload.get("key")
            decision_event = decisions.get(key)
            decision = decision_event.get("payload") if decision_event else None
            source = decision_event.get("source") if decision_event else None
            status = (decision or {}).get("action", "waiting")
            validation_error = None
            if decision_event is not None:
                try:
                    _validate_approval_source(str(key), str(payload.get("approver") or "human"), decision or {}, source)
                except ValueError as exc:
                    status = "invalid_decision"
                    validation_error = str(exc)
            summary = {
                "key": key,
                "status": status,
                "approver": payload.get("approver"),
                "prompt": payload.get("prompt"),
                "artifact": payload.get("artifact"),
                "allowed": payload.get("allowed") or ["approve", "reject"],
                "authority": payload.get("authority"),
                "timeout": payload.get("timeout"),
                "requested_seq": event.get("seq"),
                "decision": decision,
                "source": source,
            }
            if validation_error is not None:
                summary["validation_error"] = validation_error
            approvals.append(summary)
        return approvals

    def _step_summaries(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return operator-facing step state derived from durable events.

        Runtime wait/signal/handoff records remain replay plumbing. API clients
        get a step lifecycle: requested/waiting/completed/failed, completion
        mode, output, and provenance.
        """

        steps: dict[str, Dict[str, Any]] = {}
        order: list[str] = []

        def strip_prefix(value: str, prefix: str) -> str:
            return value.split(":", 1)[1] if value.startswith(prefix) else value

        def ensure(step_id: str, *, first_seq: Any = None) -> Dict[str, Any]:
            if step_id not in steps:
                steps[step_id] = {
                    "id": step_id,
                    "key": step_id,
                    "status": "recorded",
                    "first_seq": first_seq,
                    "last_seq": first_seq,
                }
                order.append(step_id)
            return steps[step_id]

        for event in events:
            event_type = str(event.get("type") or "")
            payload = event.get("payload") or {}
            raw_key = str(event.get("key") or payload.get("key") or "")
            seq = event.get("seq")

            if event_type == "StepRequested":
                step_id = raw_key
                if not step_id:
                    continue
                step = ensure(step_id, first_seq=seq)
                mode = payload.get("completion_mode")
                step["status"] = "waiting" if mode in {"approval", "worker"} else "requested"
                step["label"] = payload.get("step_name") or payload.get("label") or step.get("label") or step_id
                if mode:
                    step["completion_mode"] = mode
                if payload.get("step_type"):
                    step["step_type"] = payload.get("step_type")
                step["last_seq"] = seq
                continue

            if event_type == "ApprovalRequested":
                step_id = strip_prefix(raw_key, "approval:")
                if not step_id:
                    continue
                step = ensure(step_id, first_seq=seq)
                step.update(
                    {
                        "status": "completed" if step.get("status") == "completed" else "waiting",
                        "label": payload.get("prompt") or step.get("label") or step_id,
                        "completion_mode": "approval",
                        "step_type": "approval",
                        "requested_seq": seq,
                    }
                )
                step["last_seq"] = seq
                continue

            if event_type == "HandoffRequested":
                step_id = strip_prefix(raw_key, "handoff:")
                if not step_id:
                    continue
                step = ensure(step_id, first_seq=seq)
                step.update(
                    {
                        "status": "completed" if step.get("status") == "completed" else "waiting",
                        "label": payload.get("prompt") or step.get("label") or step_id,
                        "completion_mode": "worker",
                        "step_type": "worker",
                        "requested_seq": seq,
                    }
                )
                if payload.get("assignee"):
                    step["assignee"] = payload.get("assignee")
                step["last_seq"] = seq
                continue

            if event_type == "StepCompleted":
                step_id = raw_key
                if not step_id:
                    continue
                step = ensure(step_id, first_seq=seq)
                step["status"] = "completed"
                step["output"] = payload.get("output")
                if payload.get("metadata") is not None:
                    step["metadata"] = payload.get("metadata")
                if payload.get("completion_mode"):
                    step["completion_mode"] = payload.get("completion_mode")
                if payload.get("step_type"):
                    step["step_type"] = payload.get("step_type")
                if payload.get("source"):
                    step["source"] = payload.get("source")
                step["last_seq"] = seq
                continue

            if event_type == "StepFailed":
                step_id = raw_key
                if not step_id:
                    continue
                step = ensure(step_id, first_seq=seq)
                step["status"] = "failed"
                step["error"] = payload.get("error")
                step["last_seq"] = seq
                continue

            if event_type == "SignalReceived":
                signal_type = str(payload.get("signal_type") or "")
                if signal_type == "approval.decision":
                    step_id = str(payload.get("key") or "")
                    mode = "approval"
                    step_type = "approval"
                elif signal_type == "handoff.completed":
                    step_id = str(payload.get("key") or "")
                    mode = "worker"
                    step_type = "worker"
                else:
                    continue
                if not step_id:
                    continue
                step = ensure(step_id, first_seq=seq)
                step["status"] = "completed"
                step["completion_mode"] = mode
                step["step_type"] = step_type
                step["output"] = payload.get("payload")
                if payload.get("source"):
                    step["source"] = payload.get("source")
                step["last_seq"] = seq

        return [steps[step_id] for step_id in order]

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
        elif signal_type == "handoff.completed":
            completion_mode = "worker"
            step_type = "worker"
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
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT *
                FROM workflow_commands_outbox
                WHERE workflow_id = ? AND status IN ('pending', 'running')
                ORDER BY id ASC
                """,
                (workflow_id,),
            ).fetchall()
        return self._enrich_command_payloads([self._command_payload(row) for row in rows])

    def _enrich_command_payloads(self, commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not commands:
            return []
        workflow_ids = sorted({command["workflow_id"] for command in commands})
        summaries = self._workflow_command_summaries(workflow_ids)
        signal_keys = self._signal_keys_by_workflow(workflow_ids)
        enriched: list[Dict[str, Any]] = []
        for command in commands:
            summary = summaries.get(command["workflow_id"], {})
            labels = self._diagnostic_labels_for_command(command, summary, signal_keys.get(command["workflow_id"], set()))
            item = dict(command)
            item["workflow_status"] = summary.get("status")
            item["waiting_on"] = summary.get("waiting_on")
            item["diagnostic_labels"] = labels
            item["diagnostic_label"] = labels[0] if labels else None
            enriched.append(item)
        return enriched

    def _workflow_command_summaries(self, workflow_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        placeholders = ",".join("?" for _ in workflow_ids)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT id, status, waiting_on FROM workflow_instances WHERE id IN ({placeholders})",
                workflow_ids,
            ).fetchall()
        return {row["id"]: {"status": row["status"], "waiting_on": row["waiting_on"]} for row in rows}

    def _workflow_child_status_summaries(self, workflow_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        workflow_ids = sorted({workflow_id for workflow_id in workflow_ids if workflow_id})
        if not workflow_ids:
            return {}
        placeholders = ",".join("?" for _ in workflow_ids)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT id, status, waiting_on FROM workflow_instances WHERE id IN ({placeholders})",
                workflow_ids,
            ).fetchall()
        return {row["id"]: {"status": row["status"], "waiting_on": row["waiting_on"]} for row in rows}

    def _signal_keys_by_workflow(self, workflow_ids: List[str]) -> Dict[str, set[str]]:
        placeholders = ",".join("?" for _ in workflow_ids)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT workflow_id, key
                FROM workflow_events
                WHERE workflow_id IN ({placeholders}) AND type = 'SignalReceived'
                """,
                workflow_ids,
            ).fetchall()
        signals: Dict[str, set[str]] = {workflow_id: set() for workflow_id in workflow_ids}
        for row in rows:
            signals.setdefault(row["workflow_id"], set()).add(row["key"])
        return signals

    def _diagnostic_labels_for_command(
        self,
        command: Dict[str, Any],
        summary: Dict[str, Any],
        signal_keys: set[str],
    ) -> List[str]:
        if command.get("status") not in {"pending", "running"}:
            return []

        labels: list[str] = []
        expected_wait = _expected_wait_for_command(command)
        if command.get("type") == "notify_approval" and expected_wait in signal_keys:
            labels.append("matching_signal_exists")
        if summary.get("status") in {"completed", "failed", "cancelled"}:
            labels.append("terminal_workflow_has_pending_command")
        if summary.get("status") == "waiting" and _command_matches_current_wait(command, str(summary.get("waiting_on") or ""), expected_wait):
            labels.append("active_wait")
        if not labels:
            labels.append("orphaned_or_inconsistent")
        return labels

    def _child_workflow_summaries(self, parent_row: sqlite3.Row, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        parent_waiting_on = parent_row["waiting_on"]
        if parent_row["status"] != "waiting" or not str(parent_waiting_on or "").startswith(("child:", "child-gather:")):
            return []

        requested: dict[str, Dict[str, Any]] = {}
        terminal: set[str] = set()

        for event in events:
            event_type = event["type"]
            key = event["key"]
            payload = event["payload"] or {}
            if event_type == "ChildWorkflowRequested" and key not in requested:
                requested[key] = payload
            elif event_type in {"ChildWorkflowCompleted", "ChildWorkflowFailed"}:
                terminal.add(key)

        child_ids = [
            str(payload.get("child_workflow_id"))
            for key, payload in requested.items()
            if key not in terminal and payload.get("child_workflow_id")
        ]
        actual_status = self._workflow_child_status_summaries(child_ids) if child_ids else {}

        summaries: list[Dict[str, Any]] = []
        for key, payload in requested.items():
            if key in terminal:
                continue

            child_workflow_id = payload.get("child_workflow_id")
            actual = actual_status.get(str(child_workflow_id)) if child_workflow_id else None
            if actual is None:
                child_status = "pending"
                child_waiting_on = None
            else:
                child_status = actual.get("status") or "pending"
                child_waiting_on = None if child_status in {"completed", "failed", "cancelled"} else actual.get("waiting_on")
            label = _child_workflow_diagnostic_label(str(child_status))
            summaries.append(
                {
                    "key": key,
                    "child_workflow_id": child_workflow_id,
                    "status": child_status,
                    "waiting_on": child_waiting_on,
                    "diagnostic_label": label,
                    "diagnostic_message": _child_workflow_diagnostic_message(label),
                }
            )
        return summaries

    def _command_diagnostics(self, commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        diagnostics: list[Dict[str, Any]] = []
        for command in commands:
            for label in command.get("diagnostic_labels", []):
                diagnostics.append(
                    {
                        "command_key": command["key"],
                        "command_type": command["type"],
                        "label": label,
                        "message": _diagnostic_message(label),
                        "severity": "info" if label == "active_wait" else "warning",
                    }
                )
        return diagnostics

    def _run_decider(self, workflow_id: str, workflow_fn: Callable[..., Any]) -> RunResult:
        instance = self._instance(workflow_id)
        if instance["status"] == "cancelled":
            return self._result_from_instance(workflow_id)

        ctx = WorkflowContext(self, workflow_id)
        try:
            result = _run_maybe_async(workflow_fn(ctx, JsonCodec.loads(instance["input_json"])))
        except WorkflowCancelled:
            return self._result_from_instance(workflow_id)
        except WorkflowWaiting as waiting:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
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
        except Exception as exc:  # v0/v1: fail closed and keep the error inspectable.
            error = {"type": type(exc).__name__, "message": str(exc)}
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
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
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if row["status"] == "cancelled":
                return self._result_from_row(row)
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
            self._append_event(
                con,
                workflow_id,
                "WorkflowCompleted",
                key="workflow:completed",
                payload={"result": result},
                idempotency_key="workflow:completed",
                ignore_duplicate=True,
            )
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
        payload = command["payload"] if isinstance(command, dict) else JsonCodec.loads(command["payload_json"])
        command_type = command["type"] if isinstance(command, dict) else command["type"]
        if command_type == "run_step":
            return self._execute_run_step_command(workflow_id, command)
        if command_type == "start_child_workflow":
            return self._execute_start_child_workflow_command(workflow_id, command, payload)
        raise ValueError(f"unknown workflow command type: {command_type}")

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
        child_result = self.run_until_idle(child_fn, payload["inputs"], workflow_id=child_id)

        with self._connect() as con:
            changed = con.execute(
                """
                UPDATE workflow_commands_outbox
                SET status = 'completed', lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                  AND status = 'running'
                  AND claimed_by = ?
                  AND attempts = ?
                """,
                (_now(), command["id"], command["claimed_by"], command["attempts"]),
            ).rowcount
            if changed == 0:
                return self._result_from_instance(workflow_id)

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
                return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=parent_wait_key)

        parent = self._instance(workflow_id)
        return self._run_decider(workflow_id, self._workflow_fn_for_instance(parent))

    def _execute_run_step_command(self, workflow_id: str, command: Union[sqlite3.Row, Dict[str, Any]]) -> RunResult:
        from .decorators import get_step_body

        key = command["key"]
        payload = command["payload"] if isinstance(command, dict) else JsonCodec.loads(command["payload_json"])
        step_name = payload["step_name"]
        args = payload.get("args", [])
        kwargs = payload.get("kwargs", {})
        body = get_step_body(step_name)

        with self._connect() as con:
            row = con.execute(
                """
                SELECT c.id
                FROM workflow_commands_outbox c
                JOIN workflow_instances wi ON wi.id = c.workflow_id
                WHERE c.id = ?
                  AND c.status = 'running'
                  AND c.claimed_by = ?
                  AND c.attempts = ?
                  AND wi.status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (command["id"], command["claimed_by"], command["attempts"]),
            ).fetchone()
        if row is None:
            return self._result_from_instance(workflow_id)

        try:
            output = _run_maybe_async(body(StepExecutionContext(self, workflow_id, key), *args, **kwargs))
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            with self._connect() as con:
                changed = con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'failed', last_error_json = ?, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                      AND status = 'running'
                      AND claimed_by = ?
                      AND attempts = ?
                    """,
                    (JsonCodec.dumps(error), _now(), command["id"], command["claimed_by"], command["attempts"]),
                ).rowcount
                if changed == 0:
                    return self._result_from_instance(workflow_id)
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
            return RunResult(workflow_id=workflow_id, status="failed", error=f"{type(exc).__name__}: {exc}")

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
            error = {"type": type(exc).__name__, "message": str(exc)}
            with self._connect() as con:
                changed = con.execute(
                    """
                    UPDATE workflow_commands_outbox
                    SET status = 'failed', last_error_json = ?, lease_expires_at = NULL, updated_at = ?
                    WHERE id = ?
                      AND status = 'running'
                      AND claimed_by = ?
                      AND attempts = ?
                    """,
                    (JsonCodec.dumps(error), _now(), command["id"], command["claimed_by"], command["attempts"]),
                ).rowcount
                if changed == 0:
                    return self._result_from_instance(workflow_id)
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
            return RunResult(workflow_id=workflow_id, status="failed", error=f"{type(exc).__name__}: {exc}")

        with self._connect() as con:
            changed = con.execute(
                """
                UPDATE workflow_commands_outbox
                SET status = 'completed', lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                  AND status = 'running'
                  AND claimed_by = ?
                  AND attempts = ?
                """,
                (_now(), command["id"], command["claimed_by"], command["attempts"]),
            ).rowcount
        if changed == 0:
            return self._result_from_instance(workflow_id)
        return self.complete_step(workflow_id, key, output, metadata=metadata)

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
                  lease_expires_at INTEGER,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  last_error_json TEXT,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER,
                  UNIQUE(workflow_id, key)
                );
                """
            )
            self._ensure_instance_columns(con)
            self._ensure_command_columns(con)

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            uri_path = quote(str(self.db_path.resolve()), safe="/")
            con = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
        else:
            con = sqlite3.connect(self.db_path)
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
                (workflow_id, next_seq, event_type, key, JsonCodec.dumps(payload), idempotency_key, _now()),
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
        con.execute(
            """
            INSERT INTO workflow_commands_outbox(workflow_id, type, key, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workflow_id, command_type, key, JsonCodec.dumps(payload), _now(), _now()),
        )
        return True


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
            "lease_expires_at": "ALTER TABLE workflow_commands_outbox ADD COLUMN lease_expires_at INTEGER",
            "attempts": "ALTER TABLE workflow_commands_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "last_error_json": "ALTER TABLE workflow_commands_outbox ADD COLUMN last_error_json TEXT",
            "updated_at": "ALTER TABLE workflow_commands_outbox ADD COLUMN updated_at INTEGER",
        }
        for column, sql in migrations.items():
            if column not in existing:
                con.execute(sql)

    def _command_payload(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "type": row["type"],
            "key": row["key"],
            "payload": JsonCodec.loads(row["payload_json"]),
            "status": row["status"],
            "claimed_by": row["claimed_by"],
            "lease_expires_at": row["lease_expires_at"],
            "attempts": row["attempts"],
            "last_error": JsonCodec.loads(row["last_error_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


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
    ) -> Any:
        self._raise_if_cancelled()
        call_index = self._step_call_counts.get(step_name, 0)
        self._step_call_counts[step_name] = call_index + 1
        key = f"step:{step_name}:{call_index}"

        completed = self._last_event("StepCompleted", key)
        if completed is not None:
            return completed["output"]

        if self._last_event("StepRequested", key) is None:
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
            approver="human:skylar",
            authority=["run_generated_python_workflow"],
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
        approver: str = "human",
        allowed: Optional[List[str]] = None,
        authority: Optional[List[str]] = None,
        timeout: Optional[str] = None,
        feedback_loop: bool = False,
    ) -> ApprovalDecision:
        """Request an approval with the ergonomic ctx.approve(...) primitive."""

        return await self.approval.request(
            prompt,
            key=key,
            artifact=artifact,
            approver=approver,
            allowed=allowed,
            authority=authority,
            timeout=timeout,
            feedback_loop=feedback_loop,
        )

    async def handoff(
        self,
        prompt: str,
        *,
        key: str | None = None,
        artifact: Any = None,
        assignee: str | None = None,
        instructions: str | None = None,
        authority: Optional[List[str]] = None,
        signal_type: str = "handoff.completed",
    ) -> Any:
        """Record external/human/agent work and wait for its completion signal."""

        self._raise_if_cancelled()
        handoff_key = _safe_approval_key(key, prefix="") if key is not None else _safe_approval_key(prompt, prefix="handoff")
        event_key = f"handoff:{handoff_key}"
        completed = self._last_event("SignalReceived", f"signal:{signal_type}:{handoff_key}")
        if completed is not None:
            return completed["payload"]

        payload = {
            "prompt": prompt,
            "key": handoff_key,
            "artifact": artifact,
            "assignee": assignee,
            "instructions": instructions,
            "authority": authority or [],
            "signal_type": signal_type,
        }
        if self._last_event("HandoffRequested", event_key) is None:
            with self.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._raise_if_cancelled_in_connection(con)
                self.engine._append_step_requested(
                    con,
                    self.workflow_id,
                    handoff_key,
                    completion_mode="worker",
                    step_type="worker",
                    label=prompt,
                    payload=payload,
                )
                inserted = self.engine._append_event(
                    con,
                    self.workflow_id,
                    "HandoffRequested",
                    key=event_key,
                    payload=payload,
                    idempotency_key=f"handoff-requested:{handoff_key}",
                    ignore_duplicate=True,
                )
                if inserted:
                    self.engine._insert_command_row(con, self.workflow_id, "external_handoff", event_key, payload)

        return await self.wait_for(signal_type, key=handoff_key)

    async def external(self, *args: Any, **kwargs: Any) -> Any:
        """Alias for ctx.handoff(...); reads better for non-agent external work."""

        return await self.handoff(*args, **kwargs)

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
        approver: str = "human",
        allowed: Optional[List[str]] = None,
        authority: Optional[List[str]] = None,
        timeout: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "prompt": prompt,
            "key": key,
            "artifact": artifact,
            "approver": approver,
            "allowed": allowed or ["approve", "reject"],
            "authority": authority or [],
            "timeout": timeout,
        }

    def _emit_request_if_missing(self, con: sqlite3.Connection, *, key: str, payload: Dict[str, Any]) -> None:
        event_key = f"approval:{key}"
        self.ctx.engine._append_step_requested(
            con,
            self.ctx.workflow_id,
            key,
            completion_mode="approval",
            step_type="approval",
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

    def _validate_decision(self, *, key: str, approver: str, allowed: List[str], decision_event: Dict[str, Any]) -> ApprovalDecision:
        decision = decision_event["payload"]
        if decision.get("action") not in allowed:
            raise ValueError(f"approval {key} action is not allowed: {decision.get('action')}")
        source = _validate_approval_source(key, approver, decision, decision_event.get("source"))
        return ApprovalDecision(
            action=str(decision.get("action") or ""),
            by=str(decision.get("by") or ""),
            source=source,
            note=decision.get("note"),
            reason=decision.get("reason"),
            message=decision.get("message"),
            comment=decision.get("comment"),
        )

    async def request(
        self,
        prompt: str,
        *,
        key: str | None = None,
        artifact: Any = None,
        approver: str = "human",
        allowed: Optional[List[str]] = None,
        authority: Optional[List[str]] = None,
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
                approver=approver,
                allowed=allowed_values,
                authority=authority,
                timeout=timeout,
            )
            with self.ctx.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self.ctx._raise_if_cancelled_in_connection(con)
                self._emit_request_if_missing(con, key=key, payload=payload)

        decision_event = self._decision_event(key)
        if decision_event is None:
            return await self.ctx.wait_for("approval.decision", key=key)

        return self._validate_decision(key=key, approver=approver, allowed=allowed_values, decision_event=decision_event)

    async def request_many(
        self,
        requests: List[Dict[str, Any]],
        *,
        approver: str = "human",
        allowed: Optional[List[str]] = None,
        authority: Optional[List[str]] = None,
        timeout: Optional[str] = None,
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
            key = self._key_for_request(str(request.get("prompt") or f"approval {index}"), str(raw_key) if raw_key else None)
            if not key:
                raise ValueError("approval request_many entries require key or prompt")
            if key in seen_keys:
                raise ValueError(f"duplicate approval key in request_many: {key}")
            seen_keys.add(key)
            request_allowed = list(request.get("allowed") or allowed or ["approve", "reject"])
            request_approver = str(request.get("approver") or approver)
            normalized.append(
                {
                    "key": key,
                    "approver": request_approver,
                    "allowed": request_allowed,
                    "payload": self._payload(
                        str(request.get("prompt") or "Review approval?"),
                        key=key,
                        artifact=request.get("artifact"),
                        approver=request_approver,
                        allowed=request_allowed,
                        authority=list(request.get("authority") or authority or []),
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
                approver=item["approver"],
                allowed=item["allowed"],
                decision_event=decision_event,
            )
            decisions.append({"key": item["key"], **decision.to_dict()})

        if missing:
            raise WorkflowWaiting(f"signals:approval.decision:{','.join(missing)}")
        return decisions


_APPROVAL_SOURCE_ALLOWLIST = ("kind", "id", "channel", "message_url", "message_id", "event_id")
_APPROVAL_PRIVATE_MARKERS = ("@", "secret", "token", "password", "credential", "raw_", "raw ", "api_key")


def _approval_value_looks_private(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(marker in lowered for marker in _APPROVAL_PRIVATE_MARKERS)


def _sanitize_approval_decision_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    sanitized: Dict[str, Any] = {}
    for key in ("action", "by"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            sanitized[key] = value
    for key in ("note", "reason", "message", "comment"):
        value = payload.get(key)
        if value is not None:
            sanitized[key] = _sanitize_approval_text(str(value))
    return sanitized


def _sanitize_approval_source(source: Any) -> Dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    sanitized: Dict[str, Any] = {}
    for key in _APPROVAL_SOURCE_ALLOWLIST:
        value = source.get(key)
        if not isinstance(value, str) or not value:
            continue
        if not _approval_value_looks_private(value):
            sanitized[key] = value
        elif key in {"message_url", "message_id", "event_id"} and "event_id" not in sanitized:
            sanitized["event_id"] = f"redacted:{_hash_text(value)[:12]}"
    return sanitized


def _sanitize_approval_text(value: str) -> str:
    return "[REDACTED]" if value else value


def _validate_approval_source(
    key: str,
    approver: str,
    decision: Dict[str, Any],
    source: Any,
) -> Optional[Dict[str, Any]]:
    if not approver.startswith("human"):
        return source if isinstance(source, dict) else None

    if not isinstance(source, dict) or source.get("kind") != "human":
        raise ValueError(f"approval {key} requires human approval source")

    expected_id = approver.split(":", 1)[1] if ":" in approver else None
    dashboard_provenance = source.get("channel") == "hermes-dashboard"
    if expected_id and not dashboard_provenance and source.get("id") != expected_id:
        raise ValueError(f"approval {key} requires approval from {approver}")
    if expected_id and not dashboard_provenance and decision.get("by") != expected_id:
        raise ValueError(f"approval {key} decision.by must match {approver}")

    if not source.get("channel") or not any(source.get(field) for field in ("message_url", "message_id", "event_id")):
        raise ValueError(f"approval {key} requires external approval provenance")

    return source


@dataclass(frozen=True)
class StepExecutionContext:
    engine: WorkflowEngine
    workflow_id: str
    step_key: str


def _command_matches_current_wait(command: Dict[str, Any], waiting_on: str, expected_wait: str) -> bool:
    if waiting_on == expected_wait:
        return True
    if command.get("type") != "notify_approval":
        return False
    key = str(command.get("key") or "")
    if not key.startswith("approval:"):
        return False
    approval_key = key.split(":", 1)[1]
    multi_prefix = "signals:approval.decision:"
    if not waiting_on.startswith(multi_prefix):
        return False
    return approval_key in {part for part in waiting_on[len(multi_prefix) :].split(",") if part}


def _expected_wait_for_command(command: Dict[str, Any]) -> str:
    key = str(command.get("key") or "")
    if command.get("type") == "notify_approval" and key.startswith("approval:"):
        approval_key = key.split(":", 1)[1]
        return f"signal:approval.decision:{approval_key}"
    return key


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


def _child_workflow_diagnostic_label(status: str) -> str:
    if status in {"completed", "failed", "cancelled"}:
        return "child_workflow_terminal_unreconciled"
    if status == "waiting":
        return "child_workflow_waiting"
    if status == "pending":
        return "child_workflow_pending"
    return "child_workflow_non_terminal"


def _child_workflow_diagnostic_message(label: str) -> str:
    messages = {
        "child_workflow_waiting": "Parent is waiting on child workflow output.",
        "child_workflow_pending": "Parent requested a child workflow that has not produced an inspectable status yet.",
        "child_workflow_non_terminal": "Parent requested a child workflow that is not terminal yet.",
        "child_workflow_terminal_unreconciled": "Child workflow is terminal; parent has not reconciled it yet.",
    }
    return messages.get(label, "Child workflow has an unknown diagnostic state.")


def _diagnostic_message(label: str) -> str:
    messages = {
        "active_wait": "Workflow is actively waiting on this approval signal.",
        "matching_signal_exists": "A matching approval signal already exists; this notification is historical/stale.",
        "terminal_workflow_has_pending_command": "Workflow is terminal but this command is still pending or running.",
        "orphaned_or_inconsistent": "Command is pending or running but does not match the workflow's current wait state.",
    }
    return messages.get(label, "Command has an unknown diagnostic state.")


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Workflow):
        return value.to_json()
    if isinstance(value, ApprovalDecision):
        return _to_jsonable(value.to_dict())
    if isinstance(value, MappingABC):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {str(key): _to_jsonable(item) for key, item in value.__dict__.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    return value


def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("__hermes_type__") == "Workflow":
            return Workflow.from_json(value)
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


def _format_error(error: Any) -> Optional[str]:
    if error is None:
        return None
    if isinstance(error, dict):
        error_type = error.get("type")
        message = error.get("message")
        if error_type and message:
            return f"{error_type}: {message}"
    return JsonCodec.dumps(error)


def _history_command_payload(command: Dict[str, Any], *, payload_chars: int) -> Dict[str, Any]:
    item = dict(command)
    payload = item.pop("payload", None)
    payload_json = JsonCodec.dumps(payload)
    if len(payload_json) > payload_chars:
        item["payload_context"] = {
            "truncated": True,
            "limit": payload_chars,
            "preview": payload_json[:payload_chars],
        }
    else:
        item["payload_context"] = {
            "truncated": False,
            "limit": payload_chars,
            "value": payload,
        }
    return item


def _now() -> int:
    return int(time.time())


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

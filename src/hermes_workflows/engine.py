from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


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


class JsonCodec:
    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def loads(value: Optional[str]) -> Any:
        if value is None or value == "":
            return None
        return json.loads(value)


class WorkflowEngine:
    def __init__(self, db_path: Union[Path, str]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def start(self, workflow_fn: Callable[..., Any], inputs: Any, *, workflow_id: str) -> RunResult:
        workflow_name = getattr(workflow_fn, "__workflow_name__", workflow_fn.__name__)
        with self._connect() as con:
            existing = con.execute("SELECT id FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if existing is None:
                now = _now()
                con.execute(
                    """
                    INSERT INTO workflow_instances(id, workflow_name, status, input_json, created_at, updated_at)
                    VALUES (?, ?, 'running', ?, ?, ?)
                    """,
                    (workflow_id, workflow_name, JsonCodec.dumps(inputs), now, now),
                )
                self._append_event(
                    con,
                    workflow_id,
                    "WorkflowStarted",
                    key="workflow:start",
                    payload={"workflow_name": workflow_name, "input": inputs},
                    idempotency_key="workflow:start",
                )
        return self._run_decider(workflow_id, workflow_fn)

    def run_until_idle(self, workflow_fn: Callable[..., Any], inputs: Any, *, workflow_id: str) -> RunResult:
        """Start a workflow and execute local run_step commands until blocked.

        This is the first practical test-drive runner: it proves real step bodies
        can run out-of-band while the workflow decider still exits cleanly at
        durable waits.
        """

        result = self.start(workflow_fn, inputs, workflow_id=workflow_id)
        return self.drain(workflow_id, initial=result)

    def drain(self, workflow_id: str, *, initial: Optional[RunResult] = None) -> RunResult:
        """Execute pending local run_step commands until no runnable command remains."""

        result = initial or self._result_from_instance(workflow_id)
        while True:
            command = self.claim_command(workflow_id, worker_id="local-drain", lease_seconds=30)
            if command is None:
                return self._result_from_instance(workflow_id) if result is None else self._result_from_instance(workflow_id)
            result = self._execute_run_step_command(workflow_id, command)
            if result.status in {"failed", "completed"}:
                # There might still be historical pending commands from a corrupt
                # test DB, but v0 stops on terminal status.
                return result

    def complete_step(self, workflow_id: str, step_key: str, output: Any) -> RunResult:
        instance = self._instance(workflow_id)
        if instance["status"] == "cancelled":
            return self._result_from_instance(workflow_id)

        workflow_fn = _WORKFLOW_REGISTRY[instance["workflow_name"]]
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if row["status"] == "cancelled":
                return self._result_from_row(row)

            self._append_event(
                con,
                workflow_id,
                "StepCompleted",
                key=step_key,
                payload={"output": output},
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
        instance = self._instance(workflow_id)
        if instance["status"] == "cancelled":
            return self._result_from_instance(workflow_id)

        workflow_fn = _WORKFLOW_REGISTRY[instance["workflow_name"]]
        dedupe = idempotency_key or f"signal:{signal_type}:{key}:{JsonCodec.dumps(payload)}"
        inserted = False
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            if row["status"] == "cancelled":
                return self._result_from_row(row)

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
        result = self._run_decider(workflow_id, workflow_fn) if inserted else self._result_from_instance(workflow_id)
        return self.drain(workflow_id, initial=result)

    def cancel_workflow(
        self,
        workflow_id: str,
        *,
        reason: str,
        source: Optional[Dict[str, Any]] = None,
        superseded_by: Optional[str] = None,
    ) -> RunResult:
        """Mark a workflow terminal-cancelled while preserving an audit event."""

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
        command_type: str = "run_step",
    ) -> Optional[Dict[str, Any]]:
        """Claim one pending or lease-expired command for a worker."""

        now = _now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """
                SELECT c.*
                FROM workflow_commands_outbox c
                JOIN workflow_instances wi ON wi.id = c.workflow_id
                WHERE c.workflow_id = ?
                  AND c.type = ?
                  AND wi.status NOT IN ('completed', 'failed', 'cancelled')
                  AND (
                    c.status = 'pending'
                    OR (c.status = 'running' AND COALESCE(c.lease_expires_at, 0) <= ?)
                  )
                ORDER BY c.id ASC LIMIT 1
                """,
                (workflow_id, command_type, now),
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
        command = self.claim_command(workflow_id, worker_id=worker_id, lease_seconds=lease_seconds)
        if command is None:
            return self._result_from_instance(workflow_id)
        return self._execute_run_step_command(workflow_id, command)

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
            command = self.claim_command(workflow_id, worker_id=worker_id, lease_seconds=lease_seconds)
            if command is None:
                return self._result_from_instance(workflow_id)
            result = self._execute_run_step_command(workflow_id, command)
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

    def list_workflows(self, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT id, workflow_name, status, waiting_on
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

    def workflow_status(self, workflow_id: str, *, recent_events: int = 20) -> Dict[str, Any]:
        row = self._instance(workflow_id)
        events = self.events(workflow_id)
        pending_commands = self._active_commands(workflow_id)
        return {
            "workflow_id": row["id"],
            "workflow_name": row["workflow_name"],
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
            "approvals": self._approval_summaries(events),
        }

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
            approvals.append(
                {
                    "key": key,
                    "status": (decision or {}).get("action", "waiting"),
                    "approver": payload.get("approver"),
                    "prompt": payload.get("prompt"),
                    "artifact": payload.get("artifact"),
                    "decision": decision,
                    "source": decision_event.get("source") if decision_event else None,
                }
            )
        return approvals

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
        if summary.get("status") == "waiting" and summary.get("waiting_on") == expected_wait:
            labels.append("active_wait")
        if not labels:
            labels.append("orphaned_or_inconsistent")
        return labels

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
        return self.complete_step(workflow_id, key, output)

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
            self._ensure_command_columns(con)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

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
        }


class WorkflowContext:
    def __init__(self, engine: WorkflowEngine, workflow_id: str):
        self.engine = engine
        self.workflow_id = workflow_id
        self._step_call_counts: Dict[str, int] = {}
        self._gather_call_count = 0
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

    async def request(
        self,
        prompt: str,
        *,
        key: str,
        artifact: Any = None,
        approver: str = "human",
        allowed: Optional[List[str]] = None,
        authority: Optional[List[str]] = None,
        timeout: Optional[str] = None,
    ) -> Any:
        self.ctx._raise_if_cancelled()
        event_key = f"approval:{key}"
        if self.ctx._last_event("ApprovalRequested", event_key) is None:
            payload = {
                "prompt": prompt,
                "key": key,
                "artifact": artifact,
                "approver": approver,
                "allowed": allowed or ["approve", "reject"],
                "authority": authority or [],
                "timeout": timeout,
            }
            with self.ctx.engine._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self.ctx._raise_if_cancelled_in_connection(con)
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

        decision_event = self.ctx._last_event("SignalReceived", f"signal:approval.decision:{key}")
        if decision_event is None:
            return await self.ctx.wait_for("approval.decision", key=key)

        decision = decision_event["payload"]
        if decision.get("action") not in (allowed or ["approve", "reject"]):
            raise ValueError(f"approval {key} action is not allowed: {decision.get('action')}")

        source = _validate_approval_source(key, approver, decision, decision_event.get("source"))
        return {**decision, "source": source}


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
    if expected_id and source.get("id") != expected_id:
        raise ValueError(f"approval {key} requires approval from {approver}")
    if expected_id and decision.get("by") != expected_id:
        raise ValueError(f"approval {key} decision.by must match {approver}")

    if not source.get("channel") or not any(source.get(field) for field in ("message_url", "message_id", "event_id")):
        raise ValueError(f"approval {key} requires external approval provenance")

    return source


@dataclass(frozen=True)
class StepExecutionContext:
    engine: WorkflowEngine
    workflow_id: str
    step_key: str


def _expected_wait_for_command(command: Dict[str, Any]) -> str:
    key = str(command.get("key") or "")
    if command.get("type") == "notify_approval" and key.startswith("approval:"):
        approval_key = key.split(":", 1)[1]
        return f"signal:approval.decision:{approval_key}"
    return key


def _diagnostic_message(label: str) -> str:
    messages = {
        "active_wait": "Workflow is actively waiting on this approval signal.",
        "matching_signal_exists": "A matching approval signal already exists; this notification is historical/stale.",
        "terminal_workflow_has_pending_command": "Workflow is terminal but this command is still pending or running.",
        "orphaned_or_inconsistent": "Command is pending or running but does not match the workflow's current wait state.",
    }
    return messages.get(label, "Command has an unknown diagnostic state.")


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

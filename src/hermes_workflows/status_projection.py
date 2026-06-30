from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from .types import to_json_value
from .workflow_values import Workflow


TERMINAL_WORKFLOW_STATUSES = {"completed", "failed", "cancelled"}


class JsonCodec:
    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(_to_jsonable(value), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def loads(value: Optional[str]) -> Any:
        if value is None or value == "":
            return None
        return _from_jsonable(json.loads(value))


class StatusProjection:
    """Read-side projection for workflow/operator/outbox status views.

    WorkflowEngine remains the public facade; this boundary owns the SQL reads
    and event-derived summaries used by CLI/dashboard/review surfaces.
    """

    def __init__(self, engine: Any):
        self._engine = engine

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

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
        steps = self._step_summaries(events)
        approvals = self._approval_summaries(events)
        human_inputs = self._operator_step_summaries(events, steps=steps)
        review_requests = self._review_request_summaries(human_inputs, approvals=approvals)
        diagnostics = self._command_diagnostics(pending_commands)
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
            "diagnostics": diagnostics,
            "runtime_state": self._runtime_state_projection(
                row,
                pending_commands=pending_commands,
                diagnostics=diagnostics,
                review_requests=review_requests,
            ),
            "child_workflows": child_workflows,
            "approvals": approvals,
            "operator_steps": human_inputs,
            "review_requests": review_requests,
            "steps": steps,
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

    def _runtime_state_projection(
        self,
        row: sqlite3.Row,
        *,
        pending_commands: List[Dict[str, Any]],
        diagnostics: List[Dict[str, Any]],
        review_requests: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        now = int(time.time())
        workflow_status = str(row["status"])
        waiting_on = row["waiting_on"]
        terminal = workflow_status in TERMINAL_WORKFLOW_STATUSES

        diagnostic_by_command: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for diagnostic in diagnostics:
            key = (str(diagnostic.get("command_type") or ""), str(diagnostic.get("command_key") or ""))
            diagnostic_by_command.setdefault(key, []).append(diagnostic)

        runnable_commands = [
            command
            for command in pending_commands
            if "runnable_work" in set(command.get("diagnostic_labels") or [])
        ]
        current_command = runnable_commands[0] if runnable_commands else (pending_commands[0] if pending_commands else None)

        if terminal:
            primary = workflow_status
            reason = f"workflow_{workflow_status}"
            label = workflow_status.capitalize()
            next_action = None
        elif runnable_commands:
            command = runnable_commands[0]
            lease_expires_at = command.get("lease_expires_at")
            if command.get("status") == "running" and isinstance(lease_expires_at, int) and lease_expires_at <= now:
                primary = "stuck"
                reason = "lease_expired"
                label = "Stuck"
                next_action = "Restart or repair the runner; this command lease has expired."
            elif command.get("status") == "running":
                primary = "running"
                reason = "runnable_command_claimed"
                label = "Running"
                next_action = "Wait for the runner heartbeat/lease to advance or expire."
            else:
                primary = "queued"
                reason = "runnable_command_unclaimed"
                label = "Queued"
                next_action = "Start or repair the runner for this workflow source."
        elif waiting_on:
            wait = _runtime_wait_summary(str(waiting_on))
            is_human_wait = wait.get("kind") in {"approval", "operator"} or _has_pending_review_requests(review_requests)
            primary = "waiting_on_human" if is_human_wait else "waiting"
            reason = str(wait.get("reason") or "waiting_on_external_input")
            label = "Waiting on Skylar" if primary == "waiting_on_human" else "Waiting"
            next_action = "Submit the requested human response." if primary == "waiting_on_human" else "Wait for the external dependency to resolve."
        else:
            primary = "running" if workflow_status == "running" else workflow_status
            reason = "workflow_non_terminal_no_active_command"
            label = "Running" if primary == "running" else str(primary).replace("_", " ").capitalize()
            next_action = "Inspect recent events; workflow is non-terminal with no active command or wait."

        command_payload = None
        if current_command is not None:
            command_payload = _runtime_command_summary(
                current_command,
                diagnostics=diagnostic_by_command.get(
                    (str(current_command.get("type") or ""), str(current_command.get("key") or "")),
                    [],
                ),
            )

        return {
            "schema_version": "runtime_state.v1",
            "primary": primary,
            "label": label,
            "reason": reason,
            "workflow_status": workflow_status,
            "waiting_on": waiting_on,
            "terminal": terminal,
            "current_wait": _runtime_wait_summary(str(waiting_on)) if waiting_on else None,
            "command": command_payload,
            "worker": None,
            "next_action": next_action,
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
            if payload.get("signal_type") not in {"approval.decision", "operator.response"}:
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
            kind = payload.get("kind")
            if kind in {"human_input.request.v1", "operator.request.v1"}:
                continue
            status = (decision or {}).get("action", "waiting")
            validation_error = None
            if decision_event is not None:
                try:
                    _validate_operator_source(str(key), decision or {}, source)
                except ValueError as exc:
                    status = "invalid_decision"
                    validation_error = str(exc)
            summary = {
                "key": key,
                "status": status,
                "prompt": payload.get("prompt"),
                "artifact": payload.get("artifact"),
                "schema": payload.get("schema"),
                "allowed": payload.get("allowed") or ["approve", "reject"],
                "timeout": payload.get("timeout"),
                "requested_seq": event.get("seq"),
                "decision": decision,
                "source": source,
            }
            if validation_error is not None:
                summary["validation_error"] = validation_error
            approvals.append(summary)
        return approvals

    def _review_request_summaries(
        self,
        human_inputs: list[dict[str, Any]],
        *,
        approvals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for item in approvals:
            request = dict(item)
            request["kind"] = "approval_policy"
            request["request_type"] = "approval_policy"
            request["input_surface"] = {
                "kind": "approval_decision",
                "actions": list(request.get("allowed") or ["approve", "reject"]),
                "feedback": {"kind": "text", "optional": True},
            }
            request["request_schema"] = {
                "id": "hermes_workflows.approvals:ApprovalDecision",
                "name": "ApprovalDecision",
                "kind": "approval_decision",
            }
            requests.append(request)
        for item in human_inputs:
            request = dict(item)
            schema_id = str(request.get("schema") or "json")
            request["kind"] = "human_input"
            request["request_type"] = "human_input"
            request.setdefault("source", None)
            raw_descriptor = request.get("schema_descriptor")
            descriptor = raw_descriptor if isinstance(raw_descriptor, dict) else _review_request_schema_descriptor(schema_id)
            request["request_schema"] = descriptor
            request["input_surface"] = _review_input_surface(descriptor)
            requests.append(request)
        return requests

    def _operator_step_summaries(self, events: List[Dict[str, Any]], *, steps: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        step_summaries = steps if steps is not None else self._step_summaries(events)
        operator_steps: list[dict[str, Any]] = []
        for step in step_summaries:
            step_type = step.get("step_type")
            completion_mode = step.get("completion_mode")
            if step_type != "operator" and completion_mode != "operator":
                continue
            item = dict(step)
            item["kind"] = "operator"
            item.setdefault("prompt", item.get("label"))
            if "request" in item:
                request = item.get("request") or {}
                if isinstance(request, dict):
                    item.setdefault("artifact", request.get("artifact"))
                    item.setdefault("schema", request.get("schema"))
                    item.setdefault("schema_descriptor", request.get("schema_descriptor"))
                    item.setdefault("timeout", request.get("timeout"))
            operator_steps.append(item)
        return operator_steps

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
                step["status"] = "waiting" if mode in {"approval", "operator", "worker", "agent"} else "requested"
                label = (
                    payload.get("public_label")
                    or payload.get("public_name")
                    or _agent_request_public_label(payload)
                    or payload.get("step_name")
                    or payload.get("label")
                    or step.get("label")
                    or step_id
                )
                step["label"] = label
                for field in ("public_name", "public_label", "name_source"):
                    value = payload.get(field) or _agent_request_public_field(payload, field)
                    if value is not None:
                        step[field] = value
                if mode:
                    step["completion_mode"] = mode
                if payload.get("step_type"):
                    step["step_type"] = payload.get("step_type")
                if payload.get("request") is not None and mode == "operator":
                    step["request"] = payload.get("request")
                step["last_seq"] = seq
                continue

            if event_type == "ApprovalRequested":
                step_id = strip_prefix(raw_key, "approval:")
                if not step_id:
                    continue
                step = ensure(step_id, first_seq=seq)
                is_human_input = payload.get("kind") in {"human_input.request.v1", "operator.request.v1"}
                step.update(
                    {
                        "status": "completed" if step.get("status") == "completed" else "waiting",
                        "label": payload.get("prompt") or step.get("label") or step_id,
                        "completion_mode": "operator" if is_human_input else "approval",
                        "step_type": "operator" if is_human_input else "approval",
                        "requested_seq": seq,
                    }
                )
                step["last_seq"] = seq
                continue

            if event_type == "AgentRequested":
                step_id = strip_prefix(raw_key, "agent:")
                if not step_id:
                    continue
                step = ensure(step_id, first_seq=seq)
                step.update(
                    {
                        "status": "completed" if step.get("status") == "completed" else "waiting",
                        "label": payload.get("public_label") or payload.get("public_name") or payload.get("key") or event.get("key") or step.get("label") or step_id,
                        "completion_mode": "agent",
                        "step_type": "agent",
                        "requested_seq": seq,
                    }
                )
                for field in ("public_name", "public_label", "name_source"):
                    if payload.get(field) is not None:
                        step[field] = payload.get(field)
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
                elif signal_type == "operator.response":
                    step_id = str(payload.get("key") or "")
                    mode = "operator"
                    step_type = "operator"
                elif signal_type == "agent.completed":
                    step_id = str(payload.get("key") or "")
                    mode = "agent"
                    step_type = "agent"
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
        if command.get("type") == "run_workflow" and summary.get("status") not in {"completed", "failed", "cancelled"}:
            labels.append("runnable_work")
        elif summary.get("status") == "waiting" and _command_matches_current_wait(command, str(summary.get("waiting_on") or ""), expected_wait):
            if command.get("type") == "notify_approval":
                labels.append("active_wait")
            else:
                labels.append("runnable_work")
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
                        "severity": "info" if label in {"active_wait", "runnable_work"} else "warning",
                    }
                )
        return diagnostics



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

def _command_matches_current_wait(command: Dict[str, Any], waiting_on: str, expected_wait: str) -> bool:
    if waiting_on == expected_wait:
        return True
    if command.get("type") == "run_step" and waiting_on.startswith("gather:"):
        return True
    if command.get("type") == "start_child_workflow" and waiting_on.startswith("child-gather:"):
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


def _runtime_command_summary(command: Dict[str, Any], *, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": command.get("id"),
        "type": command.get("type"),
        "key": command.get("key"),
        "status": command.get("status"),
        "created_at": command.get("created_at"),
        "updated_at": command.get("updated_at"),
        "attempts": command.get("attempts"),
        "claimed_by": command.get("claimed_by"),
        "lease_expires_at": command.get("lease_expires_at"),
        "last_error": command.get("last_error"),
        "diagnostic_labels": list(command.get("diagnostic_labels") or []),
        "diagnostics": diagnostics,
    }


def _has_pending_review_requests(review_requests: list[dict[str, Any]]) -> bool:
    return any(request.get("status") == "waiting" for request in review_requests)


def _runtime_wait_summary(waiting_on: str) -> dict[str, Any]:
    if waiting_on.startswith("signal:approval.decision:"):
        return {
            "kind": "approval",
            "key": waiting_on.removeprefix("signal:approval.decision:"),
            "reason": "waiting_on_approval_decision",
            "raw": waiting_on,
        }
    if waiting_on.startswith("signals:approval.decision:"):
        return {
            "kind": "approval",
            "keys": [part for part in waiting_on.removeprefix("signals:approval.decision:").split(",") if part],
            "reason": "waiting_on_approval_decision",
            "raw": waiting_on,
        }
    if waiting_on.startswith("signal:operator.response:"):
        return {
            "kind": "operator",
            "key": waiting_on.removeprefix("signal:operator.response:"),
            "reason": "waiting_on_operator_response",
            "raw": waiting_on,
        }
    if waiting_on.startswith("signal:agent.completed:"):
        return {
            "kind": "agent",
            "key": waiting_on.removeprefix("signal:agent.completed:"),
            "reason": "waiting_on_agent_completion",
            "raw": waiting_on,
        }
    if waiting_on.startswith("child:"):
        return {
            "kind": "child_workflow",
            "key": waiting_on.removeprefix("child:"),
            "reason": "waiting_on_child_workflow",
            "raw": waiting_on,
        }
    if waiting_on.startswith("child-gather:"):
        return {
            "kind": "child_workflow_gather",
            "count": waiting_on.removeprefix("child-gather:"),
            "reason": "waiting_on_child_workflow_gather",
            "raw": waiting_on,
        }
    if waiting_on.startswith("gather:") or waiting_on.startswith("parallel:") or waiting_on.startswith("group:"):
        kind, _, value = waiting_on.partition(":")
        return {
            "kind": kind,
            "key": value,
            "reason": f"waiting_on_{kind}",
            "raw": waiting_on,
        }
    return {"kind": "unknown", "reason": "waiting_on_external_input", "raw": waiting_on}


def _agent_request_public_field(payload: Dict[str, Any], field: str) -> Any:
    if payload.get(field) is not None:
        return payload.get(field)
    args = payload.get("args")
    if isinstance(args, list) and args and isinstance(args[0], dict):
        return args[0].get(field)
    request = payload.get("request")
    if isinstance(request, dict):
        return request.get(field)
    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        return artifact.get(field)
    return None

def _agent_request_public_label(payload: Dict[str, Any]) -> Any:
    return _agent_request_public_field(payload, "public_label") or _agent_request_public_field(payload, "public_name")

def _expected_wait_for_command(command: Dict[str, Any]) -> str:
    key = str(command.get("key") or "")
    if command.get("type") == "notify_approval" and key.startswith("approval:"):
        approval_key = key.split(":", 1)[1]
        payload = command.get("payload") or {}
        if isinstance(payload, dict) and payload.get("kind") in {"human_input.request.v1", "operator.request.v1"}:
            return f"signal:operator.response:{approval_key}"
        return f"signal:approval.decision:{approval_key}"
    return key

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
        "runnable_work": "Workflow has runnable work queued; a worker must claim this command for autonomous continuation.",
        "matching_signal_exists": "A matching approval signal already exists; this notification is historical/stale.",
        "terminal_workflow_has_pending_command": "Workflow is terminal but this command is still pending or running.",
        "orphaned_or_inconsistent": "Command is pending or running but does not match the workflow's current wait state.",
    }
    return messages.get(label, "Command has an unknown diagnostic state.")

def _review_request_schema_descriptor(schema_id: str) -> dict[str, Any]:
    normalized = schema_id or "json"
    if ":" in normalized:
        module, name = normalized.rsplit(":", 1)
    else:
        module, name = "", normalized
    if normalized in {"json", "dict", "builtins:dict"}:
        kind = "json_object"
    elif normalized in {"str", "builtins:str"}:
        kind = "text"
    else:
        kind = "structured_object"
    descriptor = {"id": normalized, "name": name or normalized, "kind": kind}
    if module:
        descriptor["module"] = module
    return descriptor

def _review_input_surface(schema: str | dict[str, Any]) -> dict[str, Any]:
    descriptor = schema if isinstance(schema, dict) else _review_request_schema_descriptor(schema)
    raw_fields = descriptor.get("fields")
    fields = raw_fields if isinstance(raw_fields, list) else []
    action_field = next(
        (
            field
            for field in fields
            if isinstance(field, dict) and field.get("name") in {"action", "decision"} and field.get("kind") == "choice"
        ),
        None,
    )
    feedback_field = next(
        (
            field
            for field in fields
            if isinstance(field, dict)
            and field.get("name") in {"feedback", "comment", "comments", "reason", "note", "notes"}
            and field.get("kind") in {"text", "object"}
        ),
        None,
    )
    action_options = action_field.get("options") or [] if isinstance(action_field, dict) else []
    if action_field:
        actions = [_review_action_descriptor(option, has_feedback=feedback_field is not None) for option in action_options]
        surface: dict[str, Any] = {"kind": "review_decision", "actions": actions}
        if action_field.get("name") != "action":
            surface["field"] = action_field.get("name")
        if feedback_field is not None:
            surface["feedback"] = {"kind": "text", "optional": True, "placeholder": "What should change?"}
        return surface
    if descriptor["kind"] == "text":
        return {"kind": "textarea", "placeholder": "Enter feedback"}
    if descriptor["kind"] == "structured_object":
        return {"kind": "structured_form", "schema": descriptor}
    return {"kind": "json_object", "schema": descriptor}

def _review_action_descriptor(action: Any, *, has_feedback: bool = False) -> dict[str, Any]:
    value = str(action)
    label = value.replace("_", " ").strip().capitalize() or value
    item: dict[str, Any] = {"value": value, "label": label}
    if has_feedback and value not in {"approve", "accept", "ship", "proceed", "continue", "yes"}:
        item["requires_feedback"] = True
    return item

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

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
            command = self._next_pending_command(workflow_id, command_type="run_step")
            if command is None:
                return self._result_from_instance(workflow_id) if result is None else self._result_from_instance(workflow_id)
            result = self._execute_run_step_command(workflow_id, command)
            if result.status in {"failed", "completed"}:
                # There might still be historical pending commands from a corrupt
                # test DB, but v0 stops on terminal status.
                return result

    def complete_step(self, workflow_id: str, step_key: str, output: Any) -> RunResult:
        workflow_fn = _WORKFLOW_REGISTRY[self._instance(workflow_id)["workflow_name"]]
        with self._connect() as con:
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
                "UPDATE workflow_commands_outbox SET status = 'completed' WHERE workflow_id = ? AND key = ? AND type = 'run_step'",
                (workflow_id, step_key),
            )
            con.execute(
                "UPDATE workflow_instances SET status = 'running', updated_at = ? WHERE id = ?",
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
        idempotency_key: Optional[str] = None,
    ) -> RunResult:
        workflow_fn = _WORKFLOW_REGISTRY[self._instance(workflow_id)["workflow_name"]]
        dedupe = idempotency_key or f"signal:{signal_type}:{key}:{JsonCodec.dumps(payload)}"
        inserted = False
        with self._connect() as con:
            inserted = self._append_event(
                con,
                workflow_id,
                "SignalReceived",
                key=f"signal:{signal_type}:{key}",
                payload={"signal_type": signal_type, "key": key, "payload": payload},
                idempotency_key=dedupe,
                ignore_duplicate=True,
            )
            if inserted:
                con.execute(
                    "UPDATE workflow_instances SET status = 'running', updated_at = ? WHERE id = ?",
                    (_now(), workflow_id),
                )
        result = self._run_decider(workflow_id, workflow_fn) if inserted else self._result_from_instance(workflow_id)
        return self.drain(workflow_id, initial=result)

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

    def events(self, workflow_id: str) -> List[Dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT seq, type, key, payload_json, idempotency_key, created_at
                FROM workflow_events
                WHERE workflow_id = ?
                ORDER BY seq ASC
                """,
                (workflow_id,),
            ).fetchall()
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

    def _run_decider(self, workflow_id: str, workflow_fn: Callable[..., Any]) -> RunResult:
        instance = self._instance(workflow_id)
        ctx = WorkflowContext(self, workflow_id)
        try:
            result = _run_maybe_async(workflow_fn(ctx, JsonCodec.loads(instance["input_json"])))
        except WorkflowWaiting as waiting:
            with self._connect() as con:
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'waiting', waiting_on = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (waiting.waiting_on, _now(), workflow_id),
                )
            return RunResult(workflow_id=workflow_id, status="waiting", waiting_on=waiting.waiting_on)
        except Exception as exc:  # v0/v1: fail closed and keep the error inspectable.
            with self._connect() as con:
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'failed', error_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (JsonCodec.dumps({"type": type(exc).__name__, "message": str(exc)}), _now(), workflow_id),
                )
            return RunResult(workflow_id=workflow_id, status="failed", error=f"{type(exc).__name__}: {exc}")

        with self._connect() as con:
            con.execute(
                """
                UPDATE workflow_instances
                SET status = 'completed', waiting_on = NULL, result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (JsonCodec.dumps(result), _now(), workflow_id),
            )
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
        return RunResult(
            workflow_id=workflow_id,
            status=row["status"],
            waiting_on=row["waiting_on"],
            result=JsonCodec.loads(row["result_json"]),
            error=JsonCodec.dumps(JsonCodec.loads(row["error_json"])) if row["error_json"] else None,
        )

    def _execute_run_step_command(self, workflow_id: str, command: sqlite3.Row) -> RunResult:
        from .decorators import get_step_body

        key = command["key"]
        payload = JsonCodec.loads(command["payload_json"])
        step_name = payload["step_name"]
        args = payload.get("args", [])
        kwargs = payload.get("kwargs", {})
        body = get_step_body(step_name)

        with self._connect() as con:
            con.execute(
                "UPDATE workflow_commands_outbox SET status = 'running' WHERE id = ? AND status = 'pending'",
                (command["id"],),
            )

        try:
            output = _run_maybe_async(body(StepExecutionContext(self, workflow_id, key), *args, **kwargs))
        except Exception as exc:
            with self._connect() as con:
                self._append_event(
                    con,
                    workflow_id,
                    "StepFailed",
                    key=key,
                    payload={"error": {"type": type(exc).__name__, "message": str(exc)}},
                    idempotency_key=f"failed:{key}:{command['id']}",
                    ignore_duplicate=True,
                )
                con.execute(
                    "UPDATE workflow_commands_outbox SET status = 'failed' WHERE id = ?",
                    (command["id"],),
                )
                con.execute(
                    """
                    UPDATE workflow_instances
                    SET status = 'failed', error_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (JsonCodec.dumps({"type": type(exc).__name__, "message": str(exc)}), _now(), workflow_id),
                )
            return RunResult(workflow_id=workflow_id, status="failed", error=f"{type(exc).__name__}: {exc}")

        with self._connect() as con:
            con.execute(
                "UPDATE workflow_commands_outbox SET status = 'completed' WHERE id = ?",
                (command["id"],),
            )
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
                  created_at INTEGER NOT NULL,
                  UNIQUE(workflow_id, key)
                );
                """
            )

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
                con.execute(
                    """
                    INSERT INTO workflow_commands_outbox(workflow_id, type, key, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (workflow_id, command_type, key, JsonCodec.dumps(payload), _now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False


class WorkflowContext:
    def __init__(self, engine: WorkflowEngine, workflow_id: str):
        self.engine = engine
        self.workflow_id = workflow_id
        self._step_call_counts: Dict[str, int] = {}
        self._gather_call_count = 0
        self.approval = ApprovalClient(self)

    async def run_step(self, step_name: str, args: Tuple[Any, ...], kwargs: Dict[str, Any], *, block: bool = True) -> Any:
        call_index = self._step_call_counts.get(step_name, 0)
        self._step_call_counts[step_name] = call_index + 1
        key = f"step:{step_name}:{call_index}"

        completed = self._last_event("StepCompleted", key)
        if completed is not None:
            return completed["output"]

        if self._last_event("StepRequested", key) is None:
            payload = {"step_name": step_name, "args": list(args), "kwargs": kwargs}
            with self.engine._connect() as con:
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
                self.engine._insert_command(self.workflow_id, "run_step", key, payload)

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
                raise TypeError("ctx.gather only supports @step calls in this spike")
            result = await self.run_step(call.step_name, call.args, call.kwargs, block=False)
            if isinstance(result, PendingStep):
                pending.append(result.key)
                results.append(None)
            else:
                results.append(result)

        if pending:
            with self.engine._connect() as con:
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
        wait_key = f"signal:{signal_type}:{key}"
        signal = self._last_event("SignalReceived", wait_key)
        if signal is not None:
            return signal["payload"]

        request_key = f"wait:{signal_type}:{key}"
        if self._last_event("WaitRequested", request_key) is None:
            with self.engine._connect() as con:
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
                self.ctx.engine._insert_command(self.ctx.workflow_id, "notify_approval", event_key, payload)
        return await self.ctx.wait_for("approval.decision", key=key)


@dataclass(frozen=True)
class StepExecutionContext:
    engine: WorkflowEngine
    workflow_id: str
    step_key: str


def _now() -> int:
    return int(time.time())


def _run_maybe_async(value: Any) -> Any:
    if inspect.isawaitable(value):
        try:
            return asyncio.run(value)
        except RuntimeError as exc:
            raise RuntimeError("WorkflowEngine v0/v1 must be called outside an active event loop") from exc
    return value


_WORKFLOW_REGISTRY: Dict[str, Callable[..., Any]] = {}


def register_workflow(fn: Callable[..., Any]) -> Callable[..., Any]:
    _WORKFLOW_REGISTRY[getattr(fn, "__workflow_name__", fn.__name__)] = fn
    return fn

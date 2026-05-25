from __future__ import annotations

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
        with self._connect() as con:
            self._append_event(
                con,
                workflow_id,
                "SignalReceived",
                key=f"signal:{signal_type}:{key}",
                payload={"signal_type": signal_type, "key": key, "payload": payload},
                idempotency_key=dedupe,
                ignore_duplicate=True,
            )
            con.execute(
                "UPDATE workflow_instances SET status = 'running', updated_at = ? WHERE id = ?",
                (_now(), workflow_id),
            )
        return self._run_decider(workflow_id, workflow_fn)

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
            result = _run_async(workflow_fn(ctx, JsonCodec.loads(instance["input_json"])))
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
        except Exception as exc:  # v0: fail closed and keep the error inspectable.
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

    async def run_step(self, step_name: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
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

        raise WorkflowWaiting(key)

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


def _now() -> int:
    return int(time.time())


def _run_async(awaitable: Any) -> Any:
    try:
        import asyncio

        return asyncio.run(awaitable)
    except RuntimeError as exc:
        # This tiny v0 is intentionally synchronous from the caller's point of
        # view. A future async engine can remove this guard.
        raise RuntimeError("WorkflowEngine v0 must be called outside an active event loop") from exc


_WORKFLOW_REGISTRY: Dict[str, Callable[..., Any]] = {}


def register_workflow(fn: Callable[..., Any]) -> Callable[..., Any]:
    _WORKFLOW_REGISTRY[getattr(fn, "__workflow_name__", fn.__name__)] = fn
    return fn

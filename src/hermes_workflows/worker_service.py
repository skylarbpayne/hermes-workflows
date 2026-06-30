from __future__ import annotations

import json
import os
import platform
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable, Iterable

from .engine import RunResult, WorkflowEngine, _WORKFLOW_REGISTRY
from .registry import WorkflowDbConfig, WorkflowRegistry
from .workflow_loading import load_workflow_ref


@dataclass(frozen=True)
class WorkerServiceSource:
    """One workflow command source drained by a resident worker process."""

    name: str
    path: str
    allowed_workflow_refs: frozenset[str]


@dataclass
class WorkerServiceExecution:
    db_name: str
    db_path: str
    workflow_id: str
    workflow_ref: str | None
    command_id: int | None = None
    heartbeat_status: str | None = None
    status: str | None = None
    waiting_on: str | None = None
    result: Any = None
    error: str | None = None


@dataclass
class WorkerServiceTick:
    worker_id: str
    worker_instance_id: str
    executed: int = 0
    idle: bool = True
    executions: list[WorkerServiceExecution] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "worker_instance_id": self.worker_instance_id,
            "executed": self.executed,
            "idle": self.idle,
            "executions": [execution.__dict__ for execution in self.executions],
            "errors": self.errors,
        }


class WorkflowWorkerService:
    """Lease runnable commands across configured workflow DBs.

    `hermes-workflows worker --db --id <workflow_ref>` remains a useful manual drain for
    one known workflow. This service is the resident/autoresume loop: it scans one or more
    configured workflow DB sources, loads each pending instance's stored workflow_ref so
    step bodies are registered, leases one command, executes it, and repeats until idle or
    its command budget is exhausted.
    """

    def __init__(
        self,
        sources: Iterable[WorkerServiceSource],
        *,
        worker_id: str = "workflow-worker",
        lease_seconds: int = 30,
        agent_runner: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.sources = list(sources)
        if not self.sources:
            raise ValueError("worker service needs at least one workflow DB source")
        self.worker_id = worker_id
        self.worker_instance_id = _new_worker_instance_id(worker_id)
        self.lease_seconds = lease_seconds
        self.heartbeat_ttl_seconds = max(lease_seconds * 3, 30)
        self.agent_runner = agent_runner
        self._identity = _worker_identity(agent_runner_enabled=agent_runner is not None)

    @classmethod
    def from_registry(
        cls,
        registry: WorkflowRegistry,
        *,
        db: str | None = None,
        worker_id: str = "workflow-worker",
        lease_seconds: int = 30,
        agent_runner: Callable[[dict[str, Any]], Any] | None = None,
    ) -> "WorkflowWorkerService":
        return cls(
            _sources_from_registry(registry, db=db),
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            agent_runner=agent_runner,
        )

    def tick(self, *, max_commands: int | None = None) -> WorkerServiceTick:
        """Drain available commands once across all configured sources.

        If `max_commands` is omitted, this tick drains until no source has runnable work.
        Use `max_commands=1` for a single global lease/execute operation.
        """

        summary = WorkerServiceTick(worker_id=self.worker_id, worker_instance_id=self.worker_instance_id)
        skipped_commands: set[tuple[str, int]] = set()
        while max_commands is None or summary.executed < max_commands:
            executed_one = False
            for source in self.sources:
                self._heartbeat_source(source)
                candidate = self._next_runnable(source, skipped_commands=skipped_commands)
                if candidate is None:
                    continue
                executed_one = True
                summary.idle = False
                execution = self._execute_candidate(source, candidate)
                summary.executions.append(execution)
                if execution.error is not None:
                    skipped_commands.add((source.path, int(candidate["command_id"])))
                    summary.errors.append(
                        {
                            "db_name": source.name,
                            "db_path": source.path,
                            "workflow_id": candidate.get("workflow_id"),
                            "workflow_ref": candidate.get("workflow_ref"),
                            "error": execution.error,
                        }
                    )
                else:
                    summary.executed += 1
                break
            if not executed_one:
                return summary
        summary.idle = False
        return summary

    def serve(
        self,
        *,
        poll_interval: float = 1.0,
        max_commands: int | None = None,
        idle_exit_after: float | None = None,
    ) -> WorkerServiceTick:
        """Run a resident worker loop until budget, idle timeout, or KeyboardInterrupt."""

        aggregate = WorkerServiceTick(worker_id=self.worker_id, worker_instance_id=self.worker_instance_id)
        idle_started_at: float | None = None
        try:
            while max_commands is None or aggregate.executed < max_commands:
                remaining = None if max_commands is None else max_commands - aggregate.executed
                tick = self.tick(max_commands=remaining)
                _log_tick(tick)
                aggregate.executed += tick.executed
                aggregate.executions.extend(tick.executions)
                aggregate.errors.extend(tick.errors)
                if tick.executed:
                    aggregate.idle = False
                    idle_started_at = None
                    if max_commands is not None and aggregate.executed >= max_commands:
                        break
                    continue
                now = time.monotonic()
                if idle_exit_after is not None:
                    if idle_started_at is None:
                        idle_started_at = now
                    if now - idle_started_at >= idle_exit_after:
                        break
                time.sleep(min(poll_interval, max(1.0, float(self.heartbeat_ttl_seconds) / 3.0)))
        finally:
            for source in self.sources:
                self._mark_source_stopped(source)
        return aggregate

    def _next_runnable(
        self,
        source: WorkerServiceSource,
        *,
        skipped_commands: set[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        engine = WorkflowEngine(Path(source.path), agent_runner=self.agent_runner)
        for row in engine.runnable_workflows(include_external_agent=self.agent_runner is not None):
            if skipped_commands and (source.path, int(row["command_id"])) in skipped_commands:
                continue
            return row
        return None

    def _execute_candidate(self, source: WorkerServiceSource, candidate: dict[str, Any]) -> WorkerServiceExecution:
        workflow_id = str(candidate["workflow_id"])
        workflow_ref = candidate.get("workflow_ref")
        execution = WorkerServiceExecution(
            db_name=source.name,
            db_path=source.path,
            workflow_id=workflow_id,
            workflow_ref=str(workflow_ref) if workflow_ref else None,
        )
        active_command = _active_command_metadata(candidate)
        active_heartbeat = self._heartbeat_source(source, active_command=active_command)
        execution.command_id = int(candidate["command_id"])
        execution.heartbeat_status = str(active_heartbeat.get("status") or "running")
        stop, heartbeat_thread = self._start_source_heartbeat(source, active_command=active_command)
        try:
            if not workflow_ref:
                raise ValueError("worker service requires stored workflow_ref on runnable workflow instances")
            workflow_ref = str(workflow_ref)
            if workflow_ref not in source.allowed_workflow_refs:
                raise ValueError(
                    f"workflow_ref {workflow_ref!r} is not allowlisted for Workflow Worker DB source {source.name!r}"
                )
            engine = WorkflowEngine(Path(source.path), agent_runner=self.agent_runner)
            instance = engine._instance(workflow_id)
            workflow_fn = load_workflow_ref(workflow_ref)
            registered_name = getattr(workflow_fn, "__workflow_name__", getattr(workflow_fn, "__name__", None))
            if registered_name != instance["workflow_name"]:
                raise ValueError(
                    f"stored workflow_name {instance['workflow_name']!r} does not match allowlisted workflow_ref {workflow_ref!r}"
                )
            _WORKFLOW_REGISTRY[str(instance["workflow_name"])] = workflow_fn
            result = engine.worker_once(
                workflow_id,
                worker_id=self.worker_id,
                worker_instance_id=self.worker_instance_id,
                lease_seconds=self.lease_seconds,
            )
        except Exception as exc:
            execution.error = f"{type(exc).__name__}: {exc}"
            try:
                WorkflowEngine(Path(source.path), agent_runner=self.agent_runner).record_command_error(
                    workflow_id,
                    int(candidate["command_id"]),
                    {"type": type(exc).__name__, "message": str(exc)},
                    requeue=True,
                )
            except Exception:
                pass
            return execution
        finally:
            stop.set()
            heartbeat_thread.join(timeout=1.0)
            try:
                self._heartbeat_source(source)
            except Exception:
                pass
        _copy_result(result, execution)
        return execution

    def _heartbeat_source(
        self,
        source: WorkerServiceSource,
        *,
        active_command: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        engine = WorkflowEngine(Path(source.path), agent_runner=self.agent_runner)
        identity = _identity_for_source(self._identity, source, active_command=active_command)
        return engine.record_worker_heartbeat(
            worker_id=self.worker_id,
            worker_instance_id=self.worker_instance_id,
            heartbeat_ttl_seconds=self.heartbeat_ttl_seconds,
            identity=identity,
        )

    def _mark_source_stopped(self, source: WorkerServiceSource) -> None:
        try:
            WorkflowEngine(Path(source.path), agent_runner=self.agent_runner).mark_worker_stopped(
                worker_id=self.worker_id,
                worker_instance_id=self.worker_instance_id,
            )
        except Exception:
            return

    def _start_source_heartbeat(
        self,
        source: WorkerServiceSource,
        *,
        active_command: dict[str, Any] | None = None,
    ) -> tuple[threading.Event, threading.Thread]:
        interval = max(1.0, min(float(self.heartbeat_ttl_seconds) / 3.0, 10.0))
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    self._heartbeat_source(source, active_command=active_command)
                except Exception:
                    continue

        thread = threading.Thread(
            target=heartbeat,
            name=f"workflow-worker-heartbeat:{self.worker_instance_id}:{source.name}",
            daemon=True,
        )
        thread.start()
        return stop, thread


def _copy_result(result: RunResult, execution: WorkerServiceExecution) -> None:
    execution.status = result.status
    execution.waiting_on = result.waiting_on
    execution.result = result.result
    execution.error = result.error


def _log_tick(tick: WorkerServiceTick) -> None:
    """Emit resident-worker activity as JSONL so daemon logs show live failures.

    The CLI still returns the full aggregate on stdout when the worker exits. Long-lived
    launchd/systemd-style workers may not exit for weeks, so claim/load/execute failures
    must be visible immediately in stderr instead of being trapped in memory.
    """

    if tick.idle and not tick.errors:
        return
    payload = {
        "event": "workflow_worker_tick",
        "worker_id": tick.worker_id,
        "worker_instance_id": tick.worker_instance_id,
        "executed": tick.executed,
        "idle": tick.idle,
        "executions": [
            {
                "db_name": execution.db_name,
                "db_path": execution.db_path,
                "workflow_id": execution.workflow_id,
                "workflow_ref": execution.workflow_ref,
                "command_id": execution.command_id,
                "heartbeat_status": execution.heartbeat_status,
                "status": execution.status,
                "waiting_on": execution.waiting_on,
                "error": execution.error,
            }
            for execution in tick.executions
        ],
        "errors": tick.errors,
    }
    print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)


def _new_worker_instance_id(worker_id: str) -> str:
    safe_worker_id = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in worker_id)
    return f"{safe_worker_id}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _worker_identity(*, agent_runner_enabled: bool) -> dict[str, Any]:
    try:
        hermes_version = importlib_metadata.version("hermes-workflows")
    except importlib_metadata.PackageNotFoundError:
        hermes_version = None
    return {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "hermes_version": hermes_version,
        "agent_runner_enabled": agent_runner_enabled,
        "metadata": {
            "package_fingerprint": {
                "hermes_workflows": hermes_version,
                "python": platform.python_version(),
                "executable": sys.executable,
            }
        },
    }


def _identity_for_source(
    identity: dict[str, Any],
    source: WorkerServiceSource,
    *,
    active_command: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scoped = dict(identity)
    metadata = dict(identity.get("metadata") or {})
    metadata.update(
        {
            "source_db_name": source.name,
            "source_db_path": source.path,
            "allowed_workflow_refs_count": len(source.allowed_workflow_refs),
        }
    )
    if active_command is not None:
        metadata["active_command"] = active_command
    scoped["metadata"] = metadata
    return scoped


def _active_command_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": int(candidate["command_id"]),
        "command_type": str(candidate["command_type"]),
        "command_key": str(candidate["command_key"]),
        "workflow_id": str(candidate["workflow_id"]),
    }


def _sources_from_registry(registry: WorkflowRegistry, *, db: str | None = None) -> list[WorkerServiceSource]:
    resolved: dict[str, WorkflowDbConfig] = {}
    allowed_refs_by_path: dict[str, set[str]] = {}

    for workflow_config in registry.workflows.values():
        db_config = registry.resolve_db(workflow_config.db) if workflow_config.db else registry.resolve_db(None)
        resolved[db_config.path] = db_config
        allowed_refs_by_path.setdefault(db_config.path, set()).add(workflow_config.workflow_ref)

    if db is not None:
        db_config = registry.resolve_db(db)
        allowed = allowed_refs_by_path.get(db_config.path, set())
        if not allowed:
            raise ValueError(
                f"worker service DB source {db_config.name!r} has no configured workflow refs; "
                "add workflow entries so resident workers do not execute DB-supplied refs blindly"
            )
        return [
            WorkerServiceSource(
                name=db_config.name,
                path=db_config.path,
                allowed_workflow_refs=frozenset(allowed),
            )
        ]

    if not resolved:
        raise ValueError("worker service requires workflow registry entries with allowlisted workflow refs")

    return [
        WorkerServiceSource(
            name=db_config.name,
            path=db_config.path,
            allowed_workflow_refs=frozenset(allowed_refs_by_path.get(db_config.path, set())),
        )
        for db_config in sorted(resolved.values(), key=lambda item: item.name)
    ]

from __future__ import annotations

import time
from dataclasses import dataclass, field
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
    status: str | None = None
    waiting_on: str | None = None
    result: Any = None
    error: str | None = None


@dataclass
class WorkerServiceTick:
    worker_id: str
    executed: int = 0
    idle: bool = True
    executions: list[WorkerServiceExecution] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
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
        worker_id: str = "workflow-worker-service",
        lease_seconds: int = 30,
        agent_runner: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.sources = list(sources)
        if not self.sources:
            raise ValueError("worker service needs at least one workflow DB source")
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.agent_runner = agent_runner

    @classmethod
    def from_registry(
        cls,
        registry: WorkflowRegistry,
        *,
        db: str | None = None,
        worker_id: str = "workflow-worker-service",
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

        summary = WorkerServiceTick(worker_id=self.worker_id)
        skipped_commands: set[tuple[str, int]] = set()
        while max_commands is None or summary.executed < max_commands:
            executed_one = False
            for source in self.sources:
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

        aggregate = WorkerServiceTick(worker_id=self.worker_id)
        idle_started_at: float | None = None
        while max_commands is None or aggregate.executed < max_commands:
            remaining = None if max_commands is None else max_commands - aggregate.executed
            tick = self.tick(max_commands=remaining)
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
            time.sleep(poll_interval)
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
        try:
            if not workflow_ref:
                raise ValueError("worker service requires stored workflow_ref on runnable workflow instances")
            workflow_ref = str(workflow_ref)
            if workflow_ref not in source.allowed_workflow_refs:
                raise ValueError(
                    f"workflow_ref {workflow_ref!r} is not allowlisted for worker-service DB source {source.name!r}"
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
                lease_seconds=self.lease_seconds,
            )
        except Exception as exc:
            execution.error = f"{type(exc).__name__}: {exc}"
            return execution
        _copy_result(result, execution)
        return execution


def _copy_result(result: RunResult, execution: WorkerServiceExecution) -> None:
    execution.status = result.status
    execution.waiting_on = result.waiting_on
    execution.result = result.result
    execution.error = result.error


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

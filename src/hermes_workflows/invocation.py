from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable

from .dashboard import render_dashboard
from .engine import TERMINAL_WORKFLOW_STATUSES, WorkflowEngine
from .receipts import build_workflow_receipt, write_receipt
from .registry import WorkflowDbConfig, WorkflowRefConfig, WorkflowRegistry


def load_workflow_ref(ref: str) -> Callable[..., Any]:
    if ":" not in ref:
        raise ValueError("workflow ref must look like module:function")
    module_name, attr = ref.split(":", 1)
    module = importlib.import_module(module_name)
    workflow = getattr(module, attr)
    return workflow


class InvocationService:
    def __init__(self, registry: WorkflowRegistry | None = None) -> None:
        self.registry = registry or WorkflowRegistry.from_sources()

    def invoke(
        self,
        workflow_name_or_ref: str,
        *,
        workflow_id: str,
        input_payload: dict[str, Any] | None = None,
        db: str | None = None,
        source: dict[str, Any] | None = None,
        receipt_path: str | Path | None = None,
        dashboard_out: str | Path | None = None,
    ) -> dict[str, Any]:
        workflow_config = self.registry.resolve_workflow(workflow_name_or_ref)
        db_config = self._db_for_workflow(workflow_config, db)
        workflow = load_workflow_ref(workflow_config.workflow_ref)  # import before DB init: fail closed on bad refs.
        merged_input = dict(workflow_config.default_input)
        merged_input.update(input_payload or {})
        merged_input["_registry_name"] = workflow_config.name
        if source:
            merged_input.setdefault("_source", source)
        engine = WorkflowEngine(db_config.path)
        result = engine.run_until_idle(
            workflow,
            merged_input,
            workflow_id=workflow_id,
            workflow_ref=workflow_config.workflow_ref,
        )
        dashboard_path = self._render_dashboard(engine, dashboard_out)
        receipt = build_workflow_receipt(
            engine=engine,
            result=result,
            workflow_config=workflow_config,
            db_config=db_config,
            input_payload=merged_input,
            source=source,
            dashboard_path=dashboard_path,
        )
        if receipt_path is not None:
            write_receipt(receipt_path, receipt)
        return receipt

    def _db_for_workflow(self, workflow_config: WorkflowRefConfig, db: str | None = None) -> WorkflowDbConfig:
        db_ref = db if db is not None else workflow_config.db
        return self.registry.resolve_db(db_ref)

    @staticmethod
    def _render_dashboard(engine: WorkflowEngine, dashboard_out: str | Path | None) -> str | None:
        if dashboard_out is None:
            return None
        return str(render_dashboard(engine, Path(dashboard_out)))


class TrustedResumer:
    def __init__(self, registry: WorkflowRegistry | None = None) -> None:
        self.registry = registry or WorkflowRegistry.from_sources()

    def resume_trusted(
        self,
        workflow_name_or_ref: str,
        *,
        workflow_id: str,
        db: str | None = None,
        worker_id: str = "trusted-local-resumer",
        receipt_path: str | Path | None = None,
        dashboard_out: str | Path | None = None,
        require_trusted: bool = True,
    ) -> dict[str, Any]:
        workflow_config = self.registry.resolve_workflow(workflow_name_or_ref)
        if require_trusted and not workflow_config.trusted_resume:
            raise ValueError(f"trusted resume requires registry entry {workflow_config.name!r} to set trusted_resume=true")
        if require_trusted:
            self._assert_trusted_db_binding(workflow_config, db)
        db_config = self._db_for_workflow(workflow_config, db)
        read_engine = WorkflowEngine(db_config.path, read_only=True)
        status = read_engine.workflow_status(workflow_id)
        if status["status"] in TERMINAL_WORKFLOW_STATUSES:
            raise ValueError(f"workflow {workflow_id} is already terminal: {status['status']}")
        workflow_ref = status.get("workflow_ref")
        if not workflow_ref:
            raise ValueError(f"workflow {workflow_id} has no stored workflow_ref; trusted resume requires provenance")
        if workflow_ref != workflow_config.workflow_ref:
            raise ValueError(
                f"workflow {workflow_id} ref {workflow_ref!r} does not match trusted registry ref {workflow_config.workflow_ref!r}"
            )
        if require_trusted and self._registry_name_for_instance(read_engine, workflow_id) != workflow_config.name:
            raise ValueError(
                f"workflow {workflow_id} registry provenance does not match trusted registry entry {workflow_config.name!r}"
            )
        if not _has_recorded_approval_decision_for_current_wait(status):
            raise ValueError(f"workflow {workflow_id} has no recorded approval decision for its current wait")
        workflow = load_workflow_ref(str(workflow_ref))  # import after read-only status; no DB mutation on import failure.
        effective_config = WorkflowRefConfig(
            name=workflow_config.name,
            workflow_ref=str(workflow_ref),
            db=workflow_config.db,
            title=workflow_config.title,
            description=workflow_config.description,
            tags=workflow_config.tags,
            default_input=dict(workflow_config.default_input),
            trusted_resume=workflow_config.trusted_resume,
            kanban_policy=workflow_config.kanban_policy,
            dashboard_policy=workflow_config.dashboard_policy,
        )
        engine = WorkflowEngine(db_config.path)
        result = engine.resume(workflow, workflow_id)
        dashboard_path = InvocationService._render_dashboard(engine, dashboard_out)
        receipt = build_workflow_receipt(
            engine=engine,
            result=result,
            workflow_config=effective_config,
            db_config=db_config,
            dashboard_path=dashboard_path,
            worker_id=worker_id,
        )
        if receipt_path is not None:
            write_receipt(receipt_path, receipt)
        return receipt

    def resume_pending(
        self,
        registry_name: str,
        *,
        db: str | None = None,
        limit: int = 10,
        worker_id: str = "trusted-local-resumer",
        receipt_dir: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        workflow_config = self.registry.resolve_workflow(registry_name)
        if not workflow_config.trusted_resume:
            raise ValueError(f"bulk resume requires registry entry {workflow_config.name!r} to set trusted_resume=true")
        self._assert_trusted_db_binding(workflow_config, db)
        db_config = self._db_for_workflow(workflow_config, db)
        read_engine = WorkflowEngine(db_config.path, read_only=True)
        workflows = read_engine.list_workflows(status="waiting")
        receipts: list[dict[str, Any]] = []
        for item in workflows:
            if len(receipts) >= limit:
                break
            workflow_id = item["workflow_id"]
            status = read_engine.workflow_status(workflow_id, recent_events=5)
            if status.get("workflow_ref") != workflow_config.workflow_ref:
                continue
            if self._registry_name_for_instance(read_engine, workflow_id) != workflow_config.name:
                continue
            if not _has_recorded_approval_decision_for_current_wait(status):
                continue
            receipt_path = Path(receipt_dir) / f"{workflow_id}.json" if receipt_dir is not None else None
            receipts.append(
                self.resume_trusted(
                    workflow_config.name,
                    workflow_id=workflow_id,
                    db=db,
                    worker_id=worker_id,
                    receipt_path=receipt_path,
                    require_trusted=True,
                )
            )
        return receipts

    def _db_for_workflow(self, workflow_config: WorkflowRefConfig, db: str | None = None) -> WorkflowDbConfig:
        db_ref = db if db is not None else workflow_config.db
        return self.registry.resolve_db(db_ref)

    def _assert_trusted_db_binding(self, workflow_config: WorkflowRefConfig, db: str | None) -> None:
        if db is None:
            return
        if workflow_config.db is None or db != workflow_config.db:
            raise ValueError(
                f"trusted resume DB override {db!r} does not match trusted registry DB alias {workflow_config.db!r}"
            )

    @staticmethod
    def _registry_name_for_instance(engine: WorkflowEngine, workflow_id: str) -> str | None:
        for event in engine.events(workflow_id):
            if event.get("type") != "WorkflowStarted":
                continue
            payload = event.get("payload") or {}
            inputs = payload.get("input") if isinstance(payload, dict) else None
            if isinstance(inputs, dict):
                registry_name = inputs.get("_registry_name")
                return str(registry_name) if registry_name else None
        return None


def _current_approval_wait_key(status: dict[str, Any]) -> str | None:
    waiting_on = status.get("waiting_on")
    prefix = "signal:approval.decision:"
    if not isinstance(waiting_on, str) or not waiting_on.startswith(prefix):
        return None
    key = waiting_on[len(prefix) :]
    return key or None


def _has_recorded_approval_decision_for_current_wait(status: dict[str, Any]) -> bool:
    approval_key = _current_approval_wait_key(status)
    if approval_key is None:
        return False
    for approval in status.get("approvals") or []:
        if approval.get("key") != approval_key:
            continue
        if approval.get("decision") and approval.get("status") not in {"waiting", "invalid_decision"}:
            return True
    return False

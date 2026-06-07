from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class WorkflowDbConfig:
    name: str
    path: str


@dataclass(frozen=True)
class WorkflowRefConfig:
    name: str
    workflow_ref: str
    db: str | None = None
    title: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    default_input: dict[str, Any] = field(default_factory=dict)
    trusted_resume: bool = False
    kanban_policy: str = "comment"
    dashboard_policy: str = "receipt"


class WorkflowRegistry:
    """Resolve workflow aliases and DB aliases for operator-safe invocations.

    The registry is deliberately boring: adapters can use aliases, local operators can
    still pass explicit paths, and gateway-token contexts can fail closed on raw paths.
    """

    def __init__(
        self,
        *,
        dbs: dict[str, WorkflowDbConfig] | None = None,
        workflows: dict[str, WorkflowRefConfig] | None = None,
    ) -> None:
        self.dbs = dbs or {}
        self.workflows = workflows or {}

    @classmethod
    def from_sources(
        cls,
        *,
        config_path: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "WorkflowRegistry":
        dbs: dict[str, WorkflowDbConfig] = {}
        workflows: dict[str, WorkflowRefConfig] = {}

        for name, path in _env_dbs(env if env is not None else os.environ).items():
            dbs[name] = WorkflowDbConfig(name=name, path=path)

        loaded: Mapping[str, Any] | None = None
        base_dir: Path | None = None
        if config is not None:
            loaded = config
            base_dir = Path.cwd()
        elif config_path is not None:
            config_file = Path(config_path).expanduser()
            loaded = _load_json(config_file)
            base_dir = config_file.parent
        else:
            default = Path(".hermes/workflows.registry.json")
            if default.exists():
                loaded = _load_json(default)
                base_dir = default.parent

        if loaded is not None:
            dbs.update(_parse_dbs(loaded.get("dbs"), base_dir=base_dir))
            workflows.update(_parse_workflows(loaded.get("workflows")))

        return cls(dbs=dbs, workflows=workflows)

    def resolve_db(
        self,
        value: str | None,
        *,
        allow_path: bool = True,
        gateway_token_context: bool = False,
    ) -> WorkflowDbConfig:
        raw = str(value or "").strip()
        allow_path = allow_path and not gateway_token_context
        if not raw:
            if len(self.dbs) == 1:
                return next(iter(self.dbs.values()))
            if "default" in self.dbs:
                return self.dbs["default"]
            raise ValueError("No workflow DB provided and no single/default configured DB was found.")
        if raw in self.dbs:
            return self.dbs[raw]
        if looks_like_path(raw):
            if not allow_path:
                raise ValueError("explicit DB paths are not accepted in this context; use a configured DB alias")
            return WorkflowDbConfig(name="path", path=str(Path(raw).expanduser()))
        raise ValueError(f"Unknown workflow DB alias {raw!r}.")

    def resolve_gateway_db(self, value: str | None) -> WorkflowDbConfig:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("gateway DB alias is required")
        if looks_like_path(raw):
            raise ValueError("explicit DB paths are not accepted in this context; use a configured DB alias")
        if raw not in self.dbs:
            raise ValueError(f"Unknown workflow DB alias {raw!r}.")
        return self.dbs[raw]

    def resolve_workflow(self, value: str, *, db: str | None = None) -> WorkflowRefConfig:
        raw = str(value or "").strip()
        if raw in self.workflows:
            workflow = self.workflows[raw]
            if db is None:
                return workflow
            return WorkflowRefConfig(
                name=workflow.name,
                workflow_ref=workflow.workflow_ref,
                db=db,
                title=workflow.title,
                description=workflow.description,
                tags=workflow.tags,
                default_input=dict(workflow.default_input),
                trusted_resume=workflow.trusted_resume,
                kanban_policy=workflow.kanban_policy,
                dashboard_policy=workflow.dashboard_policy,
            )
        if ":" in raw:
            return WorkflowRefConfig(name=raw, workflow_ref=raw, db=db)
        if db is not None:
            raise ValueError("workflow ref must look like module:function")
        raise ValueError(f"Unknown workflow alias {raw!r}.")

    def to_payload(self) -> dict[str, Any]:
        return {
            "dbs": [{"name": db.name, "path": db.path} for db in sorted(self.dbs.values(), key=lambda item: item.name)],
            "workflows": [
                {
                    "name": wf.name,
                    "workflow_ref": wf.workflow_ref,
                    "db": wf.db,
                    "title": wf.title,
                    "description": wf.description,
                    "tags": list(wf.tags),
                    "default_input": wf.default_input,
                    "trusted_resume": wf.trusted_resume,
                    "kanban_policy": wf.kanban_policy,
                    "dashboard_policy": wf.dashboard_policy,
                }
                for wf in sorted(self.workflows.values(), key=lambda item: item.name)
            ],
        }


# Public helper used by tests and adapter code that needs the same path heuristic.
def looks_like_path(value: str) -> bool:
    return value.startswith(("/", "./", "../", "~")) or os.sep in value or value.endswith((".db", ".sqlite", ".sqlite3"))


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"registry config must be a JSON object: {path}")
    return data


def _env_dbs(env: Mapping[str, str]) -> dict[str, str]:
    configured: dict[str, str] = {}
    if env.get("HERMES_WORKFLOWS_DB"):
        configured["default"] = _normalize_db_path(str(env["HERMES_WORKFLOWS_DB"]), base_dir=None)
    raw = env.get("HERMES_WORKFLOWS_DBS")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("HERMES_WORKFLOWS_DBS must be JSON") from exc
        if isinstance(parsed, dict):
            configured.update({str(k): _normalize_db_path(str(v), base_dir=None) for k, v in parsed.items()})
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get("name") and item.get("path"):
                    configured[str(item["name"])] = _normalize_db_path(str(item["path"]), base_dir=None)
        else:
            raise ValueError("HERMES_WORKFLOWS_DBS must be a JSON object or list")
    return configured


def _normalize_db_path(value: str, *, base_dir: Path | None) -> str:
    path = Path(value).expanduser()
    if base_dir is not None and not path.is_absolute():
        path = base_dir / path
    return str(path)


def _parse_dbs(value: Any, *, base_dir: Path | None = None) -> dict[str, WorkflowDbConfig]:
    dbs: dict[str, WorkflowDbConfig] = {}
    if value is None:
        return dbs
    if isinstance(value, dict):
        for name, payload in value.items():
            if isinstance(payload, dict):
                path = payload.get("path")
            else:
                path = payload
            if not path:
                raise ValueError("registry db entries must include path")
            dbs[str(name)] = WorkflowDbConfig(name=str(name), path=_normalize_db_path(str(path), base_dir=base_dir))
        return dbs
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict) or not item.get("name") or not item.get("path"):
                raise ValueError("registry db entries must include name and path")
            dbs[str(item["name"])] = WorkflowDbConfig(
                name=str(item["name"]), path=_normalize_db_path(str(item["path"]), base_dir=base_dir)
            )
        return dbs
    raise ValueError("registry dbs must be an object or list")


def _parse_workflows(value: Any) -> dict[str, WorkflowRefConfig]:
    workflows: dict[str, WorkflowRefConfig] = {}
    if value is None:
        return workflows
    if isinstance(value, dict):
        iterable = []
        for name, payload in value.items():
            if not isinstance(payload, dict):
                raise ValueError("registry workflow entries must be objects")
            item = dict(payload)
            item["name"] = name
            iterable.append(item)
    elif isinstance(value, list):
        iterable = value
    else:
        raise ValueError("registry workflows must be an object or list")
    for item in iterable:
        if not isinstance(item, dict) or not item.get("name") or not item.get("workflow_ref"):
            raise ValueError("registry workflow entries must include name and workflow_ref")
        name = str(item["name"])
        default_input = item.get("default_input") or {}
        if not isinstance(default_input, dict):
            raise ValueError("registry workflow default_input must be an object")
        trusted_resume = item.get("trusted_resume", False)
        if not isinstance(trusted_resume, bool):
            raise ValueError("registry workflow trusted_resume must be a boolean")
        workflows[name] = WorkflowRefConfig(
            name=name,
            workflow_ref=str(item["workflow_ref"]),
            db=str(item["db"]) if item.get("db") is not None else None,
            title=str(item["title"]) if item.get("title") is not None else None,
            description=str(item["description"]) if item.get("description") is not None else None,
            tags=tuple(str(tag) for tag in item.get("tags", []) or []),
            default_input=dict(default_input),
            trusted_resume=trusted_resume,
            kanban_policy=str(item.get("kanban_policy") or "comment"),
            dashboard_policy=str(item.get("dashboard_policy") or "receipt"),
        )
    return workflows

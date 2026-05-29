from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Workflow:
    """Durable reference to generated Python workflow code.

    `AgentStep(..., returns=Workflow)` returns this normal value. Calling it from
    a workflow starts a durable child workflow instance.
    """

    source: str
    symbol: str
    source_sha256: str
    path: str
    module_name: str
    provenance: dict[str, Any] | None = None
    approval_required: bool = False
    approval_key: str | None = None

    def __call__(self, ctx: Any, inputs: Any, *, key: str | None = None) -> Any:
        return ctx.start_child(self, inputs, key=key)

    def to_json(self) -> dict[str, Any]:
        return {
            "__hermes_type__": "Workflow",
            "source": self.source,
            "symbol": self.symbol,
            "source_sha256": self.source_sha256,
            "path": self.path,
            "module_name": self.module_name,
            "provenance": self.provenance,
            "approval_required": self.approval_required,
            "approval_key": self.approval_key,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Workflow":
        source = payload["source"]
        source_sha256 = payload["source_sha256"]
        actual_sha256 = sha256_text(source)
        if actual_sha256 != source_sha256:
            raise ValueError("serialized Workflow source_sha256 does not match source")
        return cls(
            source=source,
            symbol=payload["symbol"],
            source_sha256=source_sha256,
            path="",
            module_name=f"hermes_generated_workflows.{source_sha256}",
            provenance=payload.get("provenance"),
            approval_required=bool(payload.get("approval_required", False)),
            approval_key=payload.get("approval_key"),
        )

    def with_base_dir(self, base_dir: Path) -> "Workflow":
        source_sha256 = sha256_text(self.source)
        if source_sha256 != self.source_sha256:
            raise ValueError("Workflow source_sha256 does not match source")
        path = base_dir / "generated_workflows" / f"{source_sha256}.py"
        return Workflow(
            source=self.source,
            symbol=self.symbol,
            source_sha256=source_sha256,
            path=str(path),
            module_name=f"hermes_generated_workflows.{source_sha256}",
            provenance=self.provenance,
            approval_required=self.approval_required,
            approval_key=self.approval_key,
        )

    @classmethod
    def from_source(
        cls,
        source: str,
        *,
        symbol: str,
        base_dir: Path,
        provenance: dict[str, Any] | None = None,
        approval_required: bool = False,
        approval_key: str | None = None,
        load: bool = True,
    ) -> "Workflow":
        validate_generated_workflow_source(source)
        if symbol not in _workflow_symbol_names(source):
            raise ValueError(f"generated Workflow symbol is not a @workflow function: {symbol}")
        source_sha256 = sha256_text(source)
        generated_dir = base_dir / "generated_workflows"
        generated_dir.mkdir(parents=True, exist_ok=True)
        path = generated_dir / f"{source_sha256}.py"
        if not path.exists() or path.read_text(encoding="utf-8") != source:
            path.write_text(source, encoding="utf-8")
        module_name = f"hermes_generated_workflows.{source_sha256}"
        workflow = cls(
            source=source,
            symbol=symbol,
            source_sha256=source_sha256,
            path=str(path),
            module_name=module_name,
            provenance=provenance,
            approval_required=approval_required,
            approval_key=approval_key or (f"generated-workflow:{source_sha256}" if approval_required else None),
        )
        if load:
            workflow.load()
        return workflow

    def load(self, *, approved: bool = False) -> Callable[..., Any]:
        if self.approval_required and not approved:
            raise ValueError("generated Workflow requires human approval before import/execution")
        if sha256_text(self.source) != self.source_sha256:
            raise ValueError("Workflow source_sha256 does not match source")
        validate_generated_workflow_source(self.source)
        if not self.path:
            raise ValueError("Workflow value must be bound to a generated-workflows directory before loading")
        path = Path(self.path)
        expected_name = f"{self.source_sha256}.py"
        if path.name != expected_name or path.parent.name != "generated_workflows":
            raise ValueError("generated workflow path must live under a generated_workflows directory")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.source, encoding="utf-8")
        elif sha256_text(path.read_text(encoding="utf-8")) != self.source_sha256:
            raise ValueError(f"generated workflow source hash mismatch: {path}")

        module = sys.modules.get(self.module_name)
        if module is None:
            spec = importlib.util.spec_from_file_location(self.module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot import generated workflow module: {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[self.module_name] = module
            from .decorators import registration_namespace

            with registration_namespace(f"generated:{self.source_sha256}"):
                spec.loader.exec_module(module)

        fn = getattr(module, self.symbol)
        unique_name = self.workflow_name
        if getattr(fn, "__workflow_name__", None) != unique_name:
            raise ValueError(f"generated Workflow symbol is not registered as {unique_name}: {self.symbol}")
        return fn

    @property
    def workflow_name(self) -> str:
        return f"generated:{self.source_sha256}:{self.symbol}"


def workflow_from_agent_output(
    output: Any,
    *,
    base_dir: Path,
    provenance: dict[str, Any] | None = None,
    approval_required: bool = False,
) -> Workflow:
    if isinstance(output, Workflow):
        workflow = replace(
            output,
            provenance=provenance or output.provenance,
            approval_required=approval_required or output.approval_required,
            approval_key=output.approval_key or None,
        ).with_base_dir(base_dir)
        if workflow.approval_required and workflow.approval_key is None:
            workflow = replace(workflow, approval_key=f"generated-workflow:{workflow.source_sha256}")
        if not workflow.approval_required:
            workflow.load()
        return workflow
    if isinstance(output, str):
        source = output
        symbol = _first_workflow_symbol(source)
        if symbol is None:
            raise ValueError("generated workflow source must define at least one @workflow function")
    elif isinstance(output, dict):
        source = output.get("source") or output.get("python_source") or output.get("code")
        symbol = output.get("symbol") or output.get("workflow") or output.get("workflow_symbol")
        if not isinstance(source, str):
            raise ValueError("Workflow AgentStep output must include Python source")
        if symbol is None:
            symbol = _first_workflow_symbol(source)
        if not isinstance(symbol, str):
            raise ValueError("Workflow AgentStep output symbol must be a string")
    else:
        raise TypeError("Workflow AgentStep output must be Python source or a {source, symbol} dict")
    return Workflow.from_source(
        source,
        symbol=symbol,
        base_dir=base_dir,
        provenance=provenance,
        approval_required=approval_required,
        load=not approval_required,
    )


def validate_generated_workflow_source(source: str) -> None:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            _validate_generated_import(node)
            continue
        if isinstance(node, ast.Import):
            raise ValueError("generated workflow modules may only import workflow/step from hermes_workflows")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _validate_generated_function_shape(node)
            continue
        if isinstance(node, ast.Assign) and all(isinstance(target, ast.Name) for target in node.targets):
            if any(target.id in {"workflow", "step"} for target in node.targets if isinstance(target, ast.Name)):
                raise ValueError("generated workflow modules may not rebind workflow or step")
            if _is_literal_or_empty_collection(node.value):
                continue
        raise ValueError(f"generated workflow modules may not execute top-level {type(node).__name__}")

    if _first_workflow_symbol(source) is None:
        raise ValueError("generated workflow source must define at least one @workflow function")


def _validate_generated_function_shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    if node.name in {"workflow", "step"}:
        raise ValueError("generated workflow functions may not shadow workflow or step")
    for decorator in node.decorator_list:
        if not _is_allowed_decorator(decorator):
            raise ValueError("generated workflow functions may only use @workflow or @step decorators")
    args = node.args
    if args.defaults or any(default is not None for default in args.kw_defaults):
        raise ValueError("generated workflow functions may not use default argument expressions")
    annotated_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        annotated_args.append(args.vararg)
    if args.kwarg is not None:
        annotated_args.append(args.kwarg)
    if any(arg.annotation is not None for arg in annotated_args) or node.returns is not None:
        raise ValueError("generated workflow functions may not use annotations")


def _is_allowed_decorator(decorator: ast.AST) -> bool:
    if isinstance(decorator, ast.Name):
        return decorator.id in {"workflow", "step"}
    return False


def _validate_generated_import(node: ast.ImportFrom) -> None:
    if node.module != "hermes_workflows" or node.level != 0:
        raise ValueError("generated workflow modules may only import workflow/step from hermes_workflows")
    for alias in node.names:
        if alias.name not in {"workflow", "step"} or alias.asname is not None:
            raise ValueError("generated workflow modules may only import workflow and step without aliases")


def _first_workflow_symbol(source: str) -> str | None:
    symbols = _workflow_symbol_names(source)
    return symbols[0] if symbols else None


def _workflow_symbol_names(source: str) -> list[str]:
    tree = ast.parse(source)
    symbols: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(isinstance(decorator, ast.Name) and decorator.id == "workflow" for decorator in node.decorator_list):
            symbols.append(node.name)
    return symbols


def _is_literal_or_empty_collection(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal_or_empty_collection(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (key is None or _is_literal_or_empty_collection(key)) and _is_literal_or_empty_collection(value)
            for key, value in zip(node.keys, node.values)
        )
    try:
        json.dumps(ast.literal_eval(node))
        return True
    except Exception:
        return False

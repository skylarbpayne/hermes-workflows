from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


def load_workflow_ref(ref: str) -> Callable[..., Any]:
    """Load a workflow from module:function, path.py, or path.py:function."""

    path_part, symbol = _split_path_ref(ref)
    if path_part is not None:
        module = _load_module_from_path(path_part)
        return _workflow_from_module(module, symbol=symbol, ref=ref)

    if ":" not in ref:
        raise ValueError("workflow ref must be a module:function, workflow.py, or workflow.py:function")
    module_name, attr = ref.split(":", 1)
    module = importlib.import_module(module_name)
    workflow = getattr(module, attr)
    return workflow


def canonical_workflow_ref(ref: str, workflow_fn: Callable[..., Any] | None = None) -> str:
    path_part, symbol = _split_path_ref(ref)
    if path_part is not None:
        selected = symbol or getattr(workflow_fn, "__name__", None) or getattr(workflow_fn, "__workflow_name__", None)
        if selected:
            return f"{path_part.expanduser().resolve()}:{selected}"
        return str(path_part.expanduser().resolve())
    return ref


def discover_workflow_refs(project_root: str | Path) -> list[dict[str, Any]]:
    root = Path(project_root).expanduser().resolve()
    refs: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.py")):
        if _skip_discovery_path(root, path):
            continue
        try:
            workflows = _workflow_symbols_from_source(path)
        except (OSError, SyntaxError):
            continue
        for symbol, workflow_name in workflows:
            refs.append(
                {
                    "name": path.stem if len(workflows) == 1 else f"{path.stem}:{symbol}",
                    "workflow_ref": f"{path}:{symbol}",
                    "path": str(path),
                    "symbol": symbol,
                    "workflow_name": workflow_name,
                }
            )
    return refs


def resolve_discovered_workflow(value: str, *, project_root: str | Path) -> str | None:
    matches = [item for item in discover_workflow_refs(project_root) if item["name"] == value or item["symbol"] == value]
    if len(matches) == 1:
        return str(matches[0]["workflow_ref"])
    return None


def _split_path_ref(ref: str) -> tuple[Path, str | None] | tuple[None, None]:
    raw = str(ref)
    candidate = raw
    symbol = None
    if ":" in raw:
        before, after = raw.rsplit(":", 1)
        if before.endswith(".py") or Path(before).suffix == ".py":
            candidate = before
            symbol = after
    path = Path(candidate).expanduser()
    if candidate.endswith(".py") or path.suffix == ".py" or path.exists():
        if path.suffix == ".py" or path.exists():
            return path, symbol
    return None, None


def _load_module_from_path(path: Path) -> ModuleType:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"workflow file does not exist: {resolved}")
    module_name = f"_hermes_workflow_{hashlib.sha256(str(resolved).encode('utf-8')).hexdigest()[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import workflow file: {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    path_entry = str(resolved.parent)
    added_path = path_entry not in sys.path
    if added_path:
        sys.path.insert(0, path_entry)
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if added_path:
            try:
                sys.path.remove(path_entry)
            except ValueError:  # pragma: no cover - defensive only.
                pass
    return module


def _workflow_from_module(module: ModuleType, *, symbol: str | None, ref: str) -> Callable[..., Any]:
    if symbol:
        workflow = getattr(module, symbol)
        if not callable(workflow):
            raise TypeError(f"workflow symbol is not callable: {ref}")
        return workflow
    workflows = _workflow_symbols(module)
    if len(workflows) == 1:
        return workflows[0][1]
    if not workflows:
        raise ValueError(f"no @workflow functions found in {ref}")
    names = ", ".join(name for name, _ in workflows)
    raise ValueError(f"multiple @workflow functions found in {ref}; choose one with path.py:function ({names})")


def _workflow_symbols(module: ModuleType) -> list[tuple[str, Callable[..., Any]]]:
    workflows: list[tuple[str, Callable[..., Any]]] = []
    for name, value in vars(module).items():
        if callable(value) and getattr(value, "__workflow_name__", None):
            workflows.append((name, value))
    return workflows


def _workflow_symbols_from_source(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    workflows: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_workflow_decorator(decorator) for decorator in node.decorator_list):
            workflows.append((node.name, node.name))
    return workflows


def _is_workflow_decorator(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "workflow"
    if isinstance(node, ast.Attribute):
        return node.attr == "workflow"
    if isinstance(node, ast.Call):
        return _is_workflow_decorator(node.func)
    return False


def _skip_discovery_path(root: Path, path: Path) -> bool:
    parts = set(path.relative_to(root).parts)
    return bool(parts & {".git", ".hermes", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist"})

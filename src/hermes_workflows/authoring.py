from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass, fields, is_dataclass
import ast
from typing import Annotated, Any, Callable, Generic, Literal, TypeVar, get_args, get_origin, get_type_hints

from .approvals import ApprovalDecision
from .engine import PendingStep
from .types import to_json_value

T = TypeVar("T")
_MISSING = object()
_CURRENT_CONTEXT: contextvars.ContextVar[Any] = contextvars.ContextVar("hermes_workflow_context")
_NAME_HINT_STACK: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "hermes_workflow_public_name_hints",
    default=(),
)


@dataclass(frozen=True)
class AgentCall(Generic[T]):
    name: str
    prompt: str
    input: Any = None
    returns: Any = dict
    key_by: Any = None
    key: str | None = None
    tools: Sequence[str] | None = None
    skills: Sequence[str] | None = None
    files: Sequence[str] | None = None
    model: str | None = None
    variant: str | None = None
    isolation: str = "workspace"
    timeout: int | None = None
    budget: float | None = None
    mock_output: Any = None
    public_name: str | None = None
    public_label: str | None = None
    name_source: str = "explicit"

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise TypeError("agent(...) requires a non-empty prompt")
        if not isinstance(self.name, str) or not self.name.strip():
            raise TypeError("agent(...) could not infer a non-empty public name; pass agent('name', prompt=...)")

    def __await__(self):
        return self._run(block=True).__await__()

    async def _run(self, *, block: bool) -> Any:
        ctx = current_context()
        return await self._run_with_context(ctx, block=block)

    async def _run_with_context(self, ctx: Any, *, block: bool) -> Any:
        key = self.step_key(ctx)
        payload = self._payload(key)
        request = payload["args"][0]
        if getattr(ctx.engine, "agent_runner", None) is None and self.mock_output is None:
            result = await ctx._request_agent_work(
                self.prompt,
                key=key,
                artifact=request,
                assignee=self.name,
                instructions="Complete this agent(...) request, then signal agent.completed with the JSON output payload.",
                block=block,
                public_name=self.effective_public_name,
                public_label=self.effective_public_label,
                name_source=self.name_source,
            )
        else:
            result = await ctx.run_step(
                "agent",
                tuple(payload["args"]),
                dict(payload["kwargs"]),
                block=block,
                key=key,
                payload_builder=lambda: payload,
            )
        if isinstance(result, PendingStep):
            return result
        return _coerce_return(result, self.returns)

    def step_key(self, ctx: Any) -> str:
        safe_name = _safe_key(self.name)
        if self.key is not None:
            return _safe_key(self.key)
        if self.key_by is not None:
            return f"agent:{safe_name}:{_safe_key(self.key_by)}"
        counts = getattr(ctx, "_authoring_agent_call_counts", None)
        if counts is None:
            counts = {}
            setattr(ctx, "_authoring_agent_call_counts", counts)
        index = counts.get(safe_name, 0)
        counts[safe_name] = index + 1
        return f"agent:{safe_name}:{index}"

    def with_input(self, input_value: Any, *, key_by: Any = _MISSING) -> "AgentCall[Any]":
        return AgentCall(
            self.name,
            prompt=self.prompt,
            input=input_value,
            returns=self.returns,
            key_by=(key_by if key_by is not _MISSING else _default_item_key(input_value)),
            key=self.key,
            tools=self.tools,
            skills=self.skills,
            files=self.files,
            model=self.model,
            variant=self.variant,
            isolation=self.isolation,
            timeout=self.timeout,
            budget=self.budget,
            mock_output=self.mock_output,
            public_name=self.public_name,
            public_label=self.public_label,
            name_source=self.name_source,
        )

    @property
    def effective_public_name(self) -> str:
        return self.public_name or self.name

    @property
    def effective_public_label(self) -> str:
        return self.public_label or _public_label(self.effective_public_name)

    def _payload(self, key: str) -> dict[str, Any]:
        safe_input = _jsonable(self.input)
        rendered_prompt = self.prompt
        request = {
            "kind": "agent.request.v1",
            "name": self.name,
            "public_name": self.effective_public_name,
            "public_label": self.effective_public_label,
            "name_source": self.name_source,
            "prompt": self.prompt,
            "prompt_sha256": _sha256_text(self.prompt),
            "rendered_prompt": rendered_prompt,
            "rendered_prompt_sha256": _sha256_text(rendered_prompt),
            "input": safe_input,
            "input_sha256": _sha256_json(safe_input),
            "returns": _return_schema_id(self.returns),
            "tools": list(self.tools or []),
            "skills": list(self.skills or []),
            "files": list(self.files or []),
            "model": self.model,
            "variant": self.variant,
            "isolation": self.isolation,
            "timeout": self.timeout,
            "budget": self.budget,
            "mock_output": self.mock_output,
            "step_key": key,
        }
        request["fingerprint"] = _sha256_json(
            {
                "prompt": request["prompt"],
                "input": request["input"],
                "returns": request["returns"],
                "tools": request["tools"],
                "skills": request["skills"],
                "files": request["files"],
                "model": request["model"],
                "variant": request["variant"],
                "isolation": request["isolation"],
            }
        )
        return {
            "step_name": "agent",
            "args": [request],
            "kwargs": {},
            "public_name": self.effective_public_name,
            "public_label": self.effective_public_label,
            "name_source": self.name_source,
        }


@dataclass(frozen=True)
class AskCall(Generic[T]):
    prompt: str
    key: str | None = None
    input: Any = None
    returns: Any = dict
    timeout: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise TypeError("ask(...) requires a non-empty prompt")

    def __await__(self):
        return self._run(block=True).__await__()

    async def _run(self, *, block: bool) -> Any:
        ctx = current_context()
        return await self._run_with_context(ctx, block=block)

    async def _run_with_context(self, ctx: Any, *, block: bool) -> Any:
        key = self.step_key(ctx)
        result = await ctx._request_human_input(
            self.prompt,
            key=key,
            artifact=self.input,
            schema=_return_schema_id(self.returns),
            schema_descriptor=_return_schema_descriptor(self.returns),
            timeout=self.timeout,
            block=block,
        )
        if isinstance(result, PendingStep):
            return result
        return _coerce_return(result, self.returns)

    def step_key(self, ctx: Any) -> str:
        if self.key is not None:
            return _safe_key(self.key)
        base = _safe_key(self.prompt)
        counts = getattr(ctx, "_authoring_ask_call_counts", None)
        if counts is None:
            counts = {}
            setattr(ctx, "_authoring_ask_call_counts", counts)
        index = counts.get(base, 0)
        counts[base] = index + 1
        return base if index == 0 else f"{base}_{index}"


def bind_workflow_context(ctx: Any):
    return _CURRENT_CONTEXT.set(ctx)


def reset_workflow_context(token: contextvars.Token[Any]) -> None:
    _CURRENT_CONTEXT.reset(token)


def current_context() -> Any:
    try:
        return _CURRENT_CONTEXT.get()
    except LookupError as exc:
        raise RuntimeError("workflow authoring primitive used outside a running Hermes workflow") from exc


def agent(
    name: str | None = None,
    *,
    prompt: str,
    input: Any = None,
    returns: Any = dict,
    key_by: Any = None,
    key: str | None = None,
    tools: Sequence[str] | None = None,
    skills: Sequence[str] | None = None,
    files: Sequence[str] | None = None,
    model: str | None = None,
    variant: str | None = None,
    isolation: str = "workspace",
    timeout: int | None = None,
    budget: float | None = None,
    mock_output: Any = None,
) -> AgentCall[Any]:
    if name is None:
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        public_name, name_source = _infer_public_name(caller)
    else:
        public_name = str(name).strip()
        name_source = "explicit"
    return AgentCall(
        public_name,
        prompt=prompt,
        input=input,
        returns=returns,
        key_by=key_by,
        key=key,
        tools=tools,
        skills=skills,
        files=files,
        model=model,
        variant=variant,
        isolation=isolation,
        timeout=timeout,
        budget=budget,
        mock_output=mock_output,
        public_name=public_name,
        public_label=_public_label(public_name),
        name_source=name_source,
    )


def ask(
    prompt: str,
    *,
    key: str | None = None,
    input: Any = None,
    returns: Any = dict,
    timeout: str | None = None,
) -> AskCall[Any]:
    """Request typed input from a Review Queue surface.

    `ask(...)` mirrors `agent(...)`: `input=` is the value/artifact to review,
    and `returns=` is the typed response contract.
    """

    return AskCall(prompt, key=key, input=input, returns=returns, timeout=timeout)


async def gather(*calls: Any) -> list[Any]:
    return await current_context().gather(*calls)


async def parallel(calls: Iterable[Any], *, limit: int | None = None) -> list[Any]:
    ctx = current_context()
    parallel_index = getattr(ctx, "_authoring_parallel_call_count", 0)
    setattr(ctx, "_authoring_parallel_call_count", parallel_index + 1)
    wait_key = f"parallel:{parallel_index}"
    results: list[Any] = []
    pending: list[str] = []
    for call in calls:
        result = await _start_call(ctx, call, block=False)
        if isinstance(result, PendingStep):
            pending.append(result.key)
            results.append(None)
        else:
            results.append(result)
    if pending:
        await ctx.wait_for_pending_group(wait_key, pending, kind="parallel", limit=limit)
    return results


async def pipeline(items: Iterable[Any], *stages: Any, limit: int | None = None) -> list[Any]:
    current = list(items)
    for stage_index, stage in enumerate(stages):
        calls = []
        for index, item in enumerate(current):
            call = _stage_call(stage, item, stage_index=stage_index, item_index=index)
            calls.append(call)
        current = await parallel(calls, limit=limit)
    return current


async def goal(
    do_fn: Callable[..., Any],
    check_fn: Callable[..., Any],
    *,
    max_iters: int = 20,
    initial: Any = _MISSING,
) -> Any:
    """Run ``do_fn`` until ``check_fn`` accepts the result.

    Both callables may return plain values, awaitables, ``agent(...)``/``ask(...)``
    call objects, or durable step calls. Public step names inferred inside those
    callables prefer the callable names so authors can write inference-first
    loops without exposing runtime context plumbing.
    """

    if max_iters < 1:
        raise ValueError("goal(..., max_iters=...) must be at least 1")
    value = None if initial is _MISSING else initial
    have_value = initial is not _MISSING
    for _index in range(max_iters):
        candidate = _call_with_optional_value(do_fn, value, have_value=have_value)
        value = await _resolve_goal_value(candidate, hint=_callable_public_name(do_fn, fallback="do"))
        have_value = True
        verdict = _call_with_optional_value(check_fn, value, have_value=True)
        accepted = await _resolve_goal_value(verdict, hint=_callable_public_name(check_fn, fallback="check"))
        if bool(accepted):
            return value
    return value

    return list(value)


async def approve(
    prompt: str,
    *,
    key: str | None = None,
    artifact: Any = None,
    allowed: Sequence[str] | None = None,
    timeout: str | None = None,
    feedback_loop: bool = False,
) -> ApprovalDecision:
    runtime_context = current_context()
    return await runtime_context.approve(
        prompt,
        key=key,
        artifact=artifact,
        allowed=list(allowed) if allowed is not None else None,
        timeout=timeout,
        feedback_loop=feedback_loop,
    )


async def approve_many(
    requests: Sequence[Mapping[str, Any]],
    *,
    allowed: Sequence[str] | None = None,
    timeout: str | None = None,
    feedback_loop: bool = False,
) -> list[dict[str, Any]]:
    runtime_context = current_context()
    return await runtime_context.approval.request_many(
        list(requests),
        allowed=list(allowed) if allowed is not None else None,
        timeout=timeout,
        feedback_loop=feedback_loop,
    )


async def wait_for(signal_type: str, *, key: str) -> Any:
    return await current_context().wait_for(signal_type, key=key)


async def start_child(workflow_ref: Any, inputs: Any, *, key: str | None = None, group: str | None = None, block: bool = True) -> Any:
    return await current_context().start_child(workflow_ref, inputs, key=key, group=group, block=block)


async def map_workflow(
    workflow_ref: Any,
    items: Sequence[Any],
    *,
    key_fn: Callable[[Any], str],
    concurrency: int | None = None,
) -> list[Any]:
    return await current_context().map_workflow(workflow_ref, list(items), key_fn=key_fn, concurrency=concurrency)


def workflow_id() -> str:
    return str(current_context().workflow_id)


def workflow_status(workflow_id: str, *, recent_events: int = 20) -> dict[str, Any]:
    return current_context().engine.workflow_status(workflow_id, recent_events=recent_events)


def cancel_workflow(workflow_id: str | None = None, *, reason: str | None = None) -> None:
    runtime_context = current_context()
    runtime_context.engine.cancel_workflow(workflow_id or runtime_context.workflow_id, reason=reason)


async def _start_call(ctx: Any, call: Any, *, block: bool) -> Any:
    from .bash import BashCall

    if isinstance(call, AgentCall):
        return await call._run_with_context(ctx, block=block)
    if isinstance(call, AskCall):
        return await call._run_with_context(ctx, block=block)
    if isinstance(call, BashCall):
        return await call._run_with_context(ctx, block=block)
    if getattr(call, "__durable_step_call__", False):
        return await ctx.run_step(
            call.step_name,
            call.args,
            call.kwargs,
            block=block,
            payload_builder=getattr(call, "payload_builder", None),
        )
    if inspect.isawaitable(call):
        if not block:
            raise TypeError("parallel(...) only supports agent(...), bash(...), and @step calls for non-blocking fan-out")
        return await call
    return call


def _stage_call(stage: Any, item: Any, *, stage_index: int, item_index: int) -> Any:
    if isinstance(stage, AgentCall):
        return stage.with_input(item, key_by=_default_item_key(item) or f"{stage_index}-{item_index}")
    if callable(stage):
        with _public_name_hint(_callable_public_name(stage, fallback=f"stage_{stage_index}")):
            return stage(item)
    raise TypeError(f"unsupported pipeline stage: {type(stage).__name__}")


async def _resolve_goal_value(value: Any, *, hint: str) -> Any:
    with _public_name_hint(hint):
        if isinstance(value, (AgentCall, AskCall)):
            return await value
        if getattr(value, "__durable_step_call__", False):
            return await value
        if inspect.isawaitable(value):
            return await value
        return value


def _call_with_optional_value(fn: Callable[..., Any], value: Any, *, have_value: bool) -> Any:
    with _public_name_hint(_callable_public_name(fn, fallback="step")):
        if not have_value:
            return fn()
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(value)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepts_varargs = any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        if accepts_varargs or positional:
            return fn(value)
        return fn()


class _public_name_hint:
    def __init__(self, hint: str | None):
        self.hint = hint
        self.token: contextvars.Token[tuple[str, ...]] | None = None

    def __enter__(self) -> None:
        if not self.hint:
            return
        stack = _NAME_HINT_STACK.get()
        self.token = _NAME_HINT_STACK.set((*stack, self.hint))

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.token is not None:
            _NAME_HINT_STACK.reset(self.token)


def _infer_public_name(frame: Any) -> tuple[str, str]:
    assignment_name = _infer_assignment_name(frame)
    if assignment_name:
        return assignment_name, "assignment"
    hints = _NAME_HINT_STACK.get()
    if hints:
        return hints[-1], "callable"
    callable_name = _frame_callable_name(frame)
    if callable_name:
        return callable_name, "callable"
    return "agent", "fallback"


def _infer_assignment_name(frame: Any) -> str | None:
    if frame is None:
        return None
    try:
        info = inspect.getframeinfo(frame, context=0)
        lines, start_line = inspect.getsourcelines(frame.f_code)
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse("".join(lines))
    except SyntaxError:
        return None
    relative_line = info.lineno - start_line + 1
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    candidates: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node, "lineno", relative_line) > relative_line:
            continue
        end_lineno = getattr(node, "end_lineno", getattr(node, "lineno", relative_line))
        if end_lineno < relative_line:
            continue
        func = node.func
        is_agent = isinstance(func, ast.Name) and func.id == "agent"
        is_attr_agent = isinstance(func, ast.Attribute) and func.attr == "agent"
        if is_agent or is_attr_agent:
            candidates.append(node)
    if not candidates:
        return None
    call = max(candidates, key=lambda item: (getattr(item, "lineno", 0), getattr(item, "col_offset", 0)))
    node: ast.AST = call
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Assign):
            return _target_public_name(node.targets[0])
        if isinstance(node, ast.AnnAssign):
            return _target_public_name(node.target)
        if isinstance(node, ast.NamedExpr):
            return _target_public_name(node.target)
        if isinstance(node, ast.Return):
            break
    return None


def _target_public_name(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, (ast.Tuple, ast.List)) and target.elts:
        return _target_public_name(target.elts[0])
    return None


def _frame_callable_name(frame: Any) -> str | None:
    if frame is None:
        return None
    name = frame.f_code.co_name
    if name and not name.startswith("<"):
        return name
    return None


def _callable_public_name(fn: Callable[..., Any], *, fallback: str) -> str:
    name = getattr(fn, "__name__", "") or ""
    if name and not name.startswith("<"):
        return name
    qualname = getattr(fn, "__qualname__", "") or ""
    parts = [part for part in qualname.split(".") if part and not part.startswith("<")]
    if parts:
        return parts[-1]
    return fallback


def _public_label(name: str) -> str:
    text = str(name).strip().replace("_", " ").replace("-", " ")
    return " ".join(part for part in text.split()) or str(name)


def _coerce_return(value: Any, returns: Any) -> Any:
    if returns in (None, Any, dict):
        return value
    if returns is str:
        return value if isinstance(value, str) else str(value)
    if returns in (int, float, bool):
        return returns(value)
    if is_dataclass(returns) and isinstance(returns, type):
        if isinstance(value, returns):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"cannot coerce {type(value).__name__} to {returns.__name__}")
        kwargs = {field.name: value[field.name] for field in fields(returns) if field.name in value}
        return returns(**kwargs)
    return value


def _return_schema_descriptor(returns: Any) -> dict[str, Any]:
    schema_id = _return_schema_id(returns)
    if returns is None or returns in (Any, dict):
        return {"id": schema_id, "name": "json", "kind": "json_object"}
    if returns is str:
        return {"id": schema_id, "name": "str", "kind": "text"}
    if returns in (int, float, bool):
        return {"id": schema_id, "name": returns.__name__, "kind": "scalar", "type": returns.__name__}
    if is_dataclass(returns) and isinstance(returns, type):
        type_hints = _safe_dataclass_type_hints(returns)
        field_descriptors = [_field_schema_descriptor(field, annotation=type_hints.get(field.name, field.type)) for field in fields(returns)]
        return {
            "id": schema_id,
            "name": returns.__qualname__,
            "kind": "structured_object",
            "module": returns.__module__,
            "fields": field_descriptors,
        }
    return {"id": schema_id, "name": str(returns), "kind": "structured_object"}


def _safe_dataclass_type_hints(dataclass_type: type[Any]) -> dict[str, Any]:
    """Return best-effort field annotations without making Python 3.9 choke.

    Test-local dataclasses often use postponed annotations. On Python 3.9,
    `str | None` cannot be evaluated by `get_type_hints`, but the Review Queue
    still needs enough schema information to render `Literal[...]` actions.
    """

    try:
        return get_type_hints(dataclass_type, include_extras=True)
    except TypeError:
        try:
            return get_type_hints(dataclass_type)
        except Exception:
            return dict(getattr(dataclass_type, "__annotations__", {}))
    except Exception:
        return dict(getattr(dataclass_type, "__annotations__", {}))


def _field_schema_descriptor(field: Any, *, annotation: Any) -> dict[str, Any]:
    description = _field_description(field, annotation=annotation)
    annotation = _strip_annotated(annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    required = field.default is MISSING and field.default_factory is MISSING
    descriptor: dict[str, Any] = {"name": field.name, "kind": "scalar", "required": required}
    if description:
        descriptor["description"] = description
    literal_options = _literal_options(annotation)
    if origin is Literal or literal_options:
        descriptor["kind"] = "choice"
        descriptor["options"] = list(args) if args else literal_options
        return descriptor
    if _is_text_annotation(annotation):
        descriptor["kind"] = "text"
    elif annotation is bool or annotation == "bool":
        descriptor["kind"] = "boolean"
    elif annotation in (int, float) or annotation in ("int", "float"):
        descriptor["kind"] = "number"
    else:
        descriptor["kind"] = "object"
    return descriptor


def _strip_annotated(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        return args[0] if args else Any
    if isinstance(annotation, str):
        parts = _annotated_string_args(annotation)
        if parts:
            return parts[0]
    return annotation


def _field_description(field: Any, *, annotation: Any) -> str | None:
    metadata_description = field.metadata.get("description") if getattr(field, "metadata", None) else None
    if isinstance(metadata_description, str) and metadata_description.strip():
        return metadata_description.strip()
    if get_origin(annotation) is Annotated:
        for item in get_args(annotation)[1:]:
            if isinstance(item, str) and item.strip():
                return item.strip()
            description = getattr(item, "description", None)
            if isinstance(description, str) and description.strip():
                return description.strip()
    if isinstance(annotation, str):
        for item in _annotated_string_args(annotation)[1:]:
            try:
                value = ast.literal_eval(item)
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _annotated_string_args(annotation: str) -> list[str]:
    text = annotation.strip()
    if not (text.startswith("Annotated[") or text.startswith("typing.Annotated[")) or not text.endswith("]"):
        return []
    inner = text[text.index("[") + 1 : -1]
    return _split_top_level_args(inner)


def _split_top_level_args(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = None
    escape = False
    for index, char in enumerate(value):
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "[({":
            depth += 1
        elif char in "])}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _literal_options(annotation: Any) -> list[Any]:
    if not isinstance(annotation, str):
        return []
    text = annotation.strip()
    if not (text.startswith("Literal[") or text.startswith("typing.Literal[")):
        return []
    inner = text[text.index("[") + 1 : -1]
    try:
        parsed = ast.literal_eval(f"({inner},)")
    except Exception:
        return []
    return list(parsed)


def _is_text_annotation(annotation: Any) -> bool:
    if annotation is str:
        return True
    args = get_args(annotation)
    if args and str in args and type(None) in args:
        return True
    if not isinstance(annotation, str):
        return False
    normalized = annotation.replace("typing.", "").replace(" ", "")
    return normalized in {"str", "str|None", "Optional[str]", "Union[str,None]", "Union[None,str]"}


def _return_schema_id(returns: Any) -> str:
    if returns is None:
        return "json"
    if returns is dict:
        return "json"
    if isinstance(returns, type):
        return f"{returns.__module__}:{returns.__qualname__}"
    return str(returns)


def _default_item_key(value: Any) -> Any:
    if hasattr(value, "slug"):
        return getattr(value, "slug")
    if hasattr(value, "id"):
        return getattr(value, "id")
    if isinstance(value, Mapping):
        return value.get("slug") or value.get("id") or value.get("key") or value.get("text")
    if isinstance(value, (str, int, float, bool)):
        return value
    return None


def _jsonable(value: Any) -> Any:
    return to_json_value(value)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")))


def _safe_key(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("step key source must be non-empty")
    safe = "".join(char if char.isalnum() or char in "._-:" else "_" for char in text)
    safe = "_".join(part for part in safe.split("_") if part)
    if safe == text and len(safe) <= 80:
        return safe
    digest = _sha256_text(text)[:10]
    return f"{safe[:64].strip('._-:') or 'key'}-{digest}"

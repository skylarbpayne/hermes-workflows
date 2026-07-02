from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass, fields, is_dataclass
import ast
from pathlib import Path
from typing import Annotated, Any, Callable, Generic, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

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


class GoalExhaustedError(RuntimeError):
    """Raised when goal(...) reaches max_iters without accepted criteria."""

    def __init__(self, *, max_iters: int, last_result: Any, last_check: Any) -> None:
        self.max_iters = max_iters
        self.last_result = last_result
        self.last_check = last_check
        super().__init__(f"goal(...) exhausted after {max_iters} iteration(s)")


@dataclass(frozen=True)
class AgentCall(Generic[T]):
    name: str
    prompt: Any
    input: Any = None
    returns: Any = dict
    key_by: Any = None
    key: str | None = None
    tools: Sequence[str] | None = None
    skills: Sequence[str] | None = None
    files: Sequence[str] | None = None
    workspace_dir: str | Path | None = None
    model: str | None = None
    variant: str | None = None
    isolation: str = "workspace"
    timeout: int | None = None
    budget: float | None = None
    max_attempts: int = 2
    mock_output: Any = None
    public_name: str | None = None
    public_label: str | None = None
    name_source: str = "explicit"

    def __post_init__(self) -> None:
        prompt_fields = _agent_prompt_fields(self.prompt)
        if not isinstance(prompt_fields["rendered_prompt"], str) or not prompt_fields["rendered_prompt"].strip():
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
                request["rendered_prompt"],
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
            workspace_dir=self.workspace_dir,
            model=self.model,
            variant=self.variant,
            isolation=self.isolation,
            timeout=self.timeout,
            budget=self.budget,
            max_attempts=self.max_attempts,
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
        prompt_fields = _agent_prompt_fields(self.prompt)
        workspace_dir = _workspace_dir_value(self.workspace_dir)
        request = {
            "kind": "agent.request.v1",
            "name": self.name,
            "public_name": self.effective_public_name,
            "public_label": self.effective_public_label,
            "name_source": self.name_source,
            **prompt_fields,
            "input": safe_input,
            "input_sha256": _sha256_json(safe_input),
            "returns": _return_schema_id(self.returns),
            "tools": list(self.tools or []),
            "skills": list(self.skills or []),
            "files": list(self.files or []),
            "workspace_dir": workspace_dir,
            "model": self.model,
            "variant": self.variant,
            "isolation": self.isolation,
            "timeout": self.timeout,
            "budget": self.budget,
            "max_attempts": self.max_attempts,
            "mock_output": self.mock_output,
            "step_key": key,
        }
        request["returns_schema"] = _return_schema_descriptor(self.returns)
        fingerprint_parts = {
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
        if workspace_dir is not None:
            fingerprint_parts["workspace_dir"] = workspace_dir
        request["fingerprint"] = _sha256_json(fingerprint_parts)
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
    choices: Sequence[str] | None = None

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
            schema_descriptor=_with_choices(_return_schema_descriptor(self.returns), self.choices),
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


@dataclass(frozen=True)
class SelectCall(Generic[T]):
    key: str
    options: Sequence[Any]
    prompt: str | None = None
    returns: Any = _MISSING
    timeout: str | None = None

    def __await__(self):
        return self._run(block=True).__await__()

    async def _run(self, *, block: bool) -> Any:
        ctx = current_context()
        return await self._run_with_context(ctx, block=block)

    async def _run_with_context(self, ctx: Any, *, block: bool) -> Any:
        normalized_options = [_selection_option(option, index=index) for index, option in enumerate(self.options)]
        result = await ctx._request_human_input(
            self.prompt or "Select one option.",
            key=_safe_key(self.key),
            artifact={"options": normalized_options},
            schema=_return_schema_id(dict if self.returns is _MISSING else self.returns),
            schema_descriptor=_selection_schema_descriptor(normalized_options, self.returns),
            timeout=self.timeout,
            block=block,
        )
        if isinstance(result, PendingStep):
            return result
        if self.returns is not _MISSING:
            return _coerce_return(result, self.returns)
        return _selected_option_value(result, self.options, normalized_options)


def bind_workflow_context(ctx: Any):
    return _CURRENT_CONTEXT.set(ctx)


def reset_workflow_context(token: contextvars.Token[Any]) -> None:
    _CURRENT_CONTEXT.reset(token)


def current_context() -> Any:
    try:
        return _CURRENT_CONTEXT.get()
    except LookupError as exc:
        raise RuntimeError("workflow authoring primitive used outside a running Hermes workflow") from exc


def current_step_context() -> Any:
    """Return the currently executing workflow or step context.

    This is an advanced escape hatch for code that needs direct runtime access;
    normal workflow and step bodies should prefer authoring primitives instead.
    """
    return current_context()


def agent(
    name: str | None = None,
    prompt: Any = None,
    *,
    input: Any = None,
    returns: Any = dict,
    key_by: Any = None,
    key: str | None = None,
    tools: Sequence[str] | None = None,
    skills: Sequence[str] | None = None,
    files: Sequence[str] | None = None,
    workspace_dir: str | Path | None = None,
    model: str | None = None,
    variant: str | None = None,
    isolation: str = "workspace",
    timeout: int | None = None,
    budget: float | None = None,
    max_attempts: int = 2,
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
        workspace_dir=workspace_dir,
        model=model,
        variant=variant,
        isolation=isolation,
        timeout=timeout,
        budget=budget,
        max_attempts=max_attempts,
        mock_output=mock_output,
        public_name=public_name,
        public_label=_public_label(public_name),
        name_source=name_source,
    )


def ask(
    prompt_or_key: str | None = None,
    prompt: str | None = None,
    *,
    key: str | None = None,
    input: Any = None,
    returns: Any = dict,
    timeout: str | None = None,
    choices: Sequence[str] | None = None,
    choice: Sequence[str] | None = None,
) -> AskCall[Any]:
    """Request typed input from a Review Queue surface.

    Supports both ``ask("Approve?", key="outline_0", ...)`` and
    ``ask("outline_0", "Approve?", ...)`` so authored workflow structure can
    stay close to the sketch without requiring sync runtime magic.
    """

    if choices is not None and choice is not None:
        raise TypeError("ask(...) accepts either choices= or choice=, not both")
    if prompt is None:
        if prompt_or_key is None:
            raise TypeError("ask(...) requires a prompt")
        prompt_text = prompt_or_key
        request_key = key
    else:
        if prompt_or_key is None:
            prompt_text = prompt
            request_key = key
        else:
            if key is not None:
                raise TypeError("ask(...) got both positional key and key=")
            request_key = prompt_or_key
            prompt_text = prompt
    return AskCall(prompt_text, key=request_key, input=input, returns=returns, timeout=timeout, choices=choices if choices is not None else choice)


def select(
    key: str,
    options: Sequence[Any],
    *,
    prompt: str | None = None,
    returns: Any = _MISSING,
    timeout: str | None = None,
) -> SelectCall[Any]:
    """Request a human/operator selection from stable options.

    If ``returns=`` is omitted, awaiting select(...) returns the selected option
    value from the original typed option list.
    """

    return SelectCall(key=key, options=options, prompt=prompt, returns=returns, timeout=timeout)


async def gather(*calls: Any) -> list[Any]:
    return await current_context().gather(*calls)


async def parallel(calls: Iterable[Any] | Any, *more_calls: Any, limit: int | None = None) -> list[Any]:
    if more_calls:
        calls = (calls, *more_calls)
    elif isinstance(calls, (AgentCall, AskCall, SelectCall)) or getattr(calls, "__durable_step_call__", False) or inspect.isawaitable(calls):
        calls = (calls,)
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


def pipeline(items_or_stage: Any, *stages: Any, limit: int | None = None) -> Any:
    if _looks_like_stage(items_or_stage):
        return _SingleValuePipeline((items_or_stage, *stages))
    return _pipeline_items(items_or_stage, *stages, limit=limit)


async def _pipeline_items(items: Iterable[Any], *stages: Any, limit: int | None = None) -> list[Any]:
    current = list(items)
    for stage_index, stage in enumerate(stages):
        calls = []
        for index, item in enumerate(current):
            call = _stage_call(stage, item, stage_index=stage_index, item_index=index)
            calls.append(call)
        current = await parallel(calls, limit=limit)
    return current


@dataclass(frozen=True)
class _SingleValuePipeline:
    stages: tuple[Any, ...]

    def __call__(self, value: Any) -> Any:
        return self._run(value)

    async def _run(self, value: Any) -> Any:
        current = value
        for stage_index, stage in enumerate(self.stages):
            call = _stage_call(stage, current, stage_index=stage_index, item_index=0)
            current = await _resolve_goal_value(call, hint=_callable_public_name(stage, fallback=f"stage_{stage_index}"))
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
    feedback: Any = None
    for _index in range(max_iters):
        candidate = _call_goal_do(do_fn, value, have_value=have_value, feedback=feedback)
        value = await _resolve_goal_value(candidate, hint=_callable_public_name(do_fn, fallback="do"))
        have_value = True
        verdict = _call_goal_check(check_fn, _index, value)
        raw_criteria = await _resolve_goal_value(verdict, hint=_callable_public_name(check_fn, fallback="check"))
        criteria = _goal_criteria(raw_criteria)
        if criteria.accepted:
            return value
        feedback = criteria.feedback
    raise GoalExhaustedError(max_iters=max_iters, last_result=value, last_check=raw_criteria)


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
    if isinstance(call, SelectCall):
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
        if isinstance(value, (AgentCall, AskCall, SelectCall)):
            return await value
        if getattr(value, "__durable_step_call__", False):
            return await value
        if inspect.isawaitable(value):
            return await value
        return value


@dataclass(frozen=True)
class _GoalCriteria:
    accepted: bool
    feedback: Any = None


def _goal_criteria(value: Any) -> _GoalCriteria:
    if isinstance(value, bool):
        return _GoalCriteria(value)
    if isinstance(value, ApprovalDecision):
        return _GoalCriteria(value.approved, value.feedback)
    if is_dataclass(value) and not isinstance(value, type):
        return _goal_criteria({field.name: getattr(value, field.name) for field in fields(value)})
    if isinstance(value, Mapping):
        feedback = _first_present(value, ("feedback", "reason", "note", "comment", "message", "direct_feedback"))
        for key in ("accepted", "approved", "ok", "passed", "pass"):
            if key in value:
                return _GoalCriteria(bool(value[key]), feedback)
        action = value.get("action") or value.get("decision")
        if isinstance(action, str):
            normalized = action.strip().lower().replace("-", "_")
            if normalized in {"approve", "approved", "accept", "accepted", "yes", "ship", "pass", "passed"}:
                return _GoalCriteria(True, feedback)
            if normalized in {"reject", "rejected", "request_changes", "revise", "rerun", "edit", "no", "fail", "failed"}:
                return _GoalCriteria(False, feedback)
        return _GoalCriteria(bool(value), feedback)
    feedback = _first_attr(value, ("feedback", "reason", "note", "comment", "message", "direct_feedback"))
    for attr in ("accepted", "approved", "ok", "passed"):
        if hasattr(value, attr):
            return _GoalCriteria(bool(getattr(value, attr)), feedback)
    if hasattr(value, "action"):
        return _goal_criteria({"action": getattr(value, "action"), "feedback": feedback})
    return _GoalCriteria(bool(value), feedback)


def _first_present(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _first_attr(value: Any, attrs: Sequence[str]) -> Any:
    for attr in attrs:
        attr_value = getattr(value, attr, None)
        if attr_value not in (None, ""):
            return attr_value
    return None


def _selection_option(option: Any, *, index: int) -> dict[str, Any]:
    raw = to_json_value(option)
    if isinstance(raw, Mapping):
        option_id = raw.get("id") or raw.get("key") or raw.get("value") or str(index)
        label = raw.get("label") or raw.get("title") or raw.get("name") or str(option_id)
        return {"id": str(option_id), "label": str(label), "value": dict(raw)}
    return {"id": str(index), "label": str(raw), "value": raw}


def _selection_schema_descriptor(options: Sequence[Mapping[str, Any]], returns: Any) -> Mapping[str, Any]:
    if returns is not _MISSING:
        return _return_schema_descriptor(returns)
    return {
        "id": "selection",
        "name": "selection",
        "kind": "structured_object",
        "fields": [
            {
                "name": "id",
                "kind": "choice",
                "required": True,
                "options": [str(option["id"]) for option in options],
                "description": "Selected option id",
            }
        ],
    }


def _selected_option_value(result: Any, raw_options: Sequence[Any], normalized_options: Sequence[Mapping[str, Any]]) -> Any:
    selected = result
    if isinstance(result, Mapping):
        for key in ("id", "option_id", "selected", "selection", "choice", "angle_id", "value"):
            if key in result:
                selected = result[key]
                break
    for raw, normalized in zip(raw_options, normalized_options):
        if str(normalized["id"]) == str(selected):
            return raw
    for raw, normalized in zip(raw_options, normalized_options):
        if to_json_value(raw) == to_json_value(result) or normalized.get("value") == to_json_value(result):
            return raw
    raise ValueError(f"select(...) response did not match an option id: {selected!r}")


def _with_choices(descriptor: dict[str, Any], choices: Sequence[str] | None) -> dict[str, Any]:
    if choices is None:
        return descriptor
    enriched = dict(descriptor)
    enriched["choices"] = [str(choice) for choice in choices]
    return enriched


def _looks_like_stage(value: Any) -> bool:
    return isinstance(value, (AgentCall, AskCall, SelectCall)) or callable(value) or getattr(value, "__durable_step_call__", False)


def _call_goal_do(fn: Callable[..., Any], value: Any, *, have_value: bool, feedback: Any) -> Any:
    with _public_name_hint(_callable_public_name(fn, fallback="do")):
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(value, feedback) if have_value and feedback is not None else _call_with_optional_value(fn, value, have_value=have_value)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepts_varargs = any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        if have_value and feedback is not None and (accepts_varargs or len(positional) >= 2):
            return fn(value, feedback)
        return _call_with_optional_value(fn, value, have_value=have_value)


def _call_goal_check(fn: Callable[..., Any], index: int, value: Any) -> Any:
    with _public_name_hint(_callable_public_name(fn, fallback="check")):
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(index, value)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepts_varargs = any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        if accepts_varargs or len(positional) >= 2:
            return fn(index, value)
        if positional:
            return fn(value)
        return fn()


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
    origin = get_origin(returns)
    args = get_args(returns)
    if origin is list:
        item_type = args[0] if args else Any
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError(f"cannot coerce {type(value).__name__} to list")
        return [_coerce_return(item, item_type) for item in value]
    if returns is str:
        return value if isinstance(value, str) else str(value)
    if returns is bool:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "y", "1", "approve", "approved", "ship", "pass"}:
                return True
            if normalized in {"false", "no", "n", "0", "reject", "rejected", "revise", "request_changes", "fail"}:
                return False
        return bool(value)
    if returns in (int, float):
        return returns(value)
    if is_dataclass(returns) and isinstance(returns, type):
        if isinstance(value, returns):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"cannot coerce {type(value).__name__} to {returns.__name__}")
        type_hints = _safe_dataclass_type_hints(returns)
        kwargs = {
            field.name: _coerce_return(value[field.name], type_hints.get(field.name, field.type))
            for field in fields(returns)
            if field.name in value
        }
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
    origin = get_origin(returns)
    args = get_args(returns)
    if origin is list:
        item_type = args[0] if args else Any
        return {
            "id": schema_id,
            "name": "list",
            "kind": "list",
            "items": _return_schema_descriptor(item_type),
        }
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
    raw_annotation = annotation
    annotation = _strip_annotated(annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    required = field.default is MISSING and field.default_factory is MISSING
    descriptor: dict[str, Any] = {
        "name": field.name,
        "kind": "scalar",
        "required": required,
        "type": _annotation_type_label(annotation),
    }
    if description:
        descriptor["description"] = description
        descriptor["help"] = description
    if field.default is not MISSING:
        descriptor["default"] = _jsonable(field.default)
    elif field.default_factory is not MISSING:  # type: ignore[attr-defined]
        descriptor["has_default"] = True
    literal_options = _literal_options(annotation)
    if origin is Literal or literal_options:
        descriptor["kind"] = "choice"
        descriptor["options"] = list(args) if args else literal_options
        descriptor["type"] = _annotation_type_label(raw_annotation)
        return descriptor
    list_item = _list_item_annotation(annotation, origin=origin, args=args)
    if list_item is not None:
        descriptor["kind"] = "list"
        descriptor["items"] = _simple_schema_descriptor(list_item)
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


def _annotation_type_label(annotation: Any) -> str:
    annotation = _strip_annotated(annotation)
    literal_options = _literal_options(annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal or literal_options:
        options = list(args) if args else literal_options
        return "Literal[" + ", ".join(repr(option) for option in options) + "]"
    if isinstance(annotation, str):
        return annotation.replace("typing.", "")
    if annotation is Any:
        return "Any"
    if annotation is type(None):
        return "None"
    if origin is not None:
        name = getattr(origin, "__name__", str(origin).replace("typing.", ""))
        if origin is Union or name in {"Union", "UnionType"}:
            return " | ".join(_annotation_type_label(arg) for arg in args)
        if args:
            return f"{name}[{', '.join(_annotation_type_label(arg) for arg in args)}]"
        return name
    return getattr(annotation, "__name__", str(annotation).replace("typing.", ""))


def _list_item_annotation(annotation: Any, *, origin: Any, args: tuple[Any, ...]) -> Any | None:
    if origin is list:
        return args[0] if args else Any
    if not isinstance(annotation, str):
        return None
    normalized = annotation.replace("typing.", "").replace(" ", "")
    if normalized.startswith("list[") and normalized.endswith("]"):
        return normalized[5:-1] or Any
    if normalized.startswith("List[") and normalized.endswith("]"):
        return normalized[5:-1] or Any
    return None


def _simple_schema_descriptor(annotation: Any) -> dict[str, Any]:
    annotation = _strip_annotated(annotation)
    literal_options = _literal_options(annotation)
    if get_origin(annotation) is Literal or literal_options:
        options = list(get_args(annotation)) if get_args(annotation) else literal_options
        return {"kind": "choice", "options": options}
    if _is_text_annotation(annotation):
        return {"kind": "text"}
    if annotation is bool or annotation == "bool":
        return {"kind": "boolean"}
    if annotation in (int, float) or annotation in ("int", "float"):
        return {"kind": "number"}
    if annotation is Any or annotation == "Any" or annotation == "typing.Any":
        return {"kind": "object"}
    return {"kind": "object"}


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
    field_metadata = getattr(field, "metadata", None)
    metadata_description = None
    if field_metadata:
        metadata_description = field_metadata.get("help") or field_metadata.get("description")
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
    origin = get_origin(returns)
    args = get_args(returns)
    if origin is list:
        item_id = _return_schema_id(args[0]) if args else "json"
        return f"list[{item_id}]"
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


def _agent_prompt_fields(prompt: Any) -> dict[str, Any]:
    to_agent_request_fields = getattr(prompt, "to_agent_request_fields", None)
    if callable(to_agent_request_fields):
        raw_fields = to_agent_request_fields()
        if not isinstance(raw_fields, Mapping):
            raise TypeError("agent(...) prompt object must return a mapping of request fields")
        fields = dict(raw_fields)
        required = ("prompt", "prompt_sha256", "rendered_prompt", "rendered_prompt_sha256")
        missing = [key for key in required if key not in fields]
        if missing:
            raise TypeError("agent(...) prompt object missing fields: " + ", ".join(missing))
        return fields
    if not isinstance(prompt, str):
        raise TypeError("agent(...) requires a non-empty prompt")
    return {
        "prompt": prompt,
        "prompt_sha256": _sha256_text(prompt),
        "rendered_prompt": prompt,
        "rendered_prompt_sha256": _sha256_text(prompt),
    }


def _workspace_dir_value(workspace_dir: str | Path | None) -> str | None:
    if workspace_dir is None:
        return None
    if isinstance(workspace_dir, Path):
        path = workspace_dir.expanduser()
    elif isinstance(workspace_dir, str) and workspace_dir.strip():
        path = Path(workspace_dir).expanduser()
    else:
        raise TypeError("agent(...) workspace_dir must be a non-empty path string or Path")
    return str(path.resolve())


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

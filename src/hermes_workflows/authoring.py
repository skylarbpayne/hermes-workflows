from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Callable, Generic, TypeVar

from .approvals import ApprovalDecision
from .engine import PendingStep
from .prompts import render_prompt

T = TypeVar("T")
_MISSING = object()
_CURRENT_CONTEXT: contextvars.ContextVar[Any] = contextvars.ContextVar("hermes_workflow_context")


@dataclass(frozen=True)
class ContextBundle:
    label: str
    content: Any
    source: str | None = None
    sha256: str | None = None

    def to_manifest(self) -> dict[str, Any]:
        jsonable = _jsonable(self.content)
        return {
            "label": self.label,
            "source": self.source,
            "content": jsonable,
            "sha256": self.sha256 or _sha256_json(jsonable),
        }


@dataclass(frozen=True)
class AgentCall(Generic[T]):
    name: str
    prompt: str
    input: Any = None
    context: Any = None
    returns: Any = dict
    key_by: Any = None
    variables: dict[str, Any] | None = None
    tools: Sequence[str] | None = None
    skills: Sequence[str] | None = None
    files: Sequence[str] | None = None
    model: str | None = None
    variant: str | None = None
    isolation: str = "workspace"
    timeout: int | None = None
    budget: float | None = None
    mock_output: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise TypeError("agent(...) requires a non-empty prompt")

    def __await__(self):
        return self._run(block=True).__await__()

    async def _run(self, *, block: bool) -> Any:
        ctx = current_context()
        return await self._run_with_context(ctx, block=block)

    async def _run_with_context(self, ctx: Any, *, block: bool) -> Any:
        key = self.step_key(ctx)
        payload = self._payload(key)
        result = await ctx.run_step(
            "agent_step",
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
            context=self.context,
            returns=self.returns,
            key_by=(key_by if key_by is not _MISSING else _default_item_key(input_value)),
            variables=self.variables,
            tools=self.tools,
            skills=self.skills,
            files=self.files,
            model=self.model,
            variant=self.variant,
            isolation=self.isolation,
            timeout=self.timeout,
            budget=self.budget,
            mock_output=self.mock_output,
        )

    def _payload(self, key: str) -> dict[str, Any]:
        variables = dict(self.variables or {})
        safe_input = _jsonable(self.input)
        context_manifest = _context_manifest(self.context)
        rendered_prompt = render_prompt(self.prompt, variables) if variables else self.prompt
        request = {
            "kind": "agent_step.request.v1",
            "name": self.name,
            "prompt": self.prompt,
            "prompt_sha256": _sha256_text(self.prompt),
            "rendered_prompt": rendered_prompt,
            "rendered_prompt_sha256": _sha256_text(rendered_prompt),
            "variables": _jsonable(variables),
            "variables_sha256": _sha256_json(variables),
            "input": safe_input,
            "input_sha256": _sha256_json(safe_input),
            "context": context_manifest,
            "context_sha256": _sha256_json(context_manifest),
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
                "rendered_prompt": request["rendered_prompt"],
                "input": request["input"],
                "context_sha256": request["context_sha256"],
                "returns": request["returns"],
                "tools": request["tools"],
                "skills": request["skills"],
                "files": request["files"],
                "model": request["model"],
                "variant": request["variant"],
                "isolation": request["isolation"],
            }
        )
        return {"step_name": "agent_step", "args": [request], "kwargs": {}}


@dataclass(frozen=True)
class ApprovalValueCall(Generic[T]):
    key: str
    value: T
    prompt: str | None = None
    approver: str = "human"
    allowed: Sequence[str] | None = None
    authority: Sequence[str] | None = None
    timeout: str | None = None

    def __await__(self):
        return self._run().__await__()

    async def _run(self) -> T:
        ctx = current_context()
        decision = await ctx.approve(
            self.prompt or self.key,
            key=self.key,
            artifact=self.value,
            approver=self.approver,
            allowed=list(self.allowed or ["approve", "reject", "edit", "revise", "rerun"]),
            authority=list(self.authority or []),
            timeout=self.timeout,
            feedback_loop=True,
        )
        if isinstance(decision, ApprovalDecision) and decision.approved:
            return self.value
        if isinstance(decision, ApprovalDecision) and decision.needs_revision:
            raise ValueError(f"approval {self.key} needs revision: {decision.feedback or decision.action}")
        return self.value


@dataclass(frozen=True)
class ApprovalStage:
    key: str
    prompt: str | None = None
    approver: str = "human"
    allowed: Sequence[str] | None = None
    authority: Sequence[str] | None = None
    timeout: str | None = None

    def __call__(self, value: T) -> ApprovalValueCall[T]:
        item_key = _default_item_key(value)
        key = f"{self.key}:{_safe_key(item_key)}" if item_key is not None else self.key
        return ApprovalValueCall(
            key,
            value,
            prompt=self.prompt or self.key,
            approver=self.approver,
            allowed=self.allowed,
            authority=self.authority,
            timeout=self.timeout,
        )


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
    name: str,
    *,
    prompt: str,
    input: Any = None,
    context: Any = None,
    returns: Any = dict,
    key_by: Any = None,
    variables: dict[str, Any] | None = None,
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
    return AgentCall(
        name,
        prompt=prompt,
        input=input,
        context=context,
        returns=returns,
        key_by=key_by,
        variables=variables,
        tools=tools,
        skills=skills,
        files=files,
        model=model,
        variant=variant,
        isolation=isolation,
        timeout=timeout,
        budget=budget,
        mock_output=mock_output,
    )


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


async def approve(
    prompt: str,
    *,
    key: str | None = None,
    artifact: Any = None,
    approver: str = "human",
    allowed: Sequence[str] | None = None,
    authority: Sequence[str] | None = None,
    timeout: str | None = None,
) -> ApprovalDecision:
    ctx = current_context()
    return await ctx.approve(
        prompt,
        key=key,
        artifact=artifact,
        approver=approver,
        allowed=list(allowed) if allowed is not None else None,
        authority=list(authority) if authority is not None else None,
        timeout=timeout,
    )


def approve_until(
    key: str,
    value: Any = _MISSING,
    *,
    prompt: str | None = None,
    approver: str = "human",
    allowed: Sequence[str] | None = None,
    authority: Sequence[str] | None = None,
    timeout: str | None = None,
) -> ApprovalStage | ApprovalValueCall[Any]:
    if value is _MISSING:
        return ApprovalStage(key, prompt=prompt, approver=approver, allowed=allowed, authority=authority, timeout=timeout)
    return ApprovalValueCall(
        key,
        value,
        prompt=prompt,
        approver=approver,
        allowed=allowed,
        authority=authority,
        timeout=timeout,
    )


async def _start_call(ctx: Any, call: Any, *, block: bool) -> Any:
    if isinstance(call, AgentCall):
        return await call._run_with_context(ctx, block=block)
    if isinstance(call, ApprovalValueCall):
        if block:
            return await call
        raise TypeError("approval calls cannot be fanned out as non-blocking worker steps")
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
            raise TypeError("parallel(...) only supports agent(...) and @step calls for non-blocking fan-out")
        return await call
    return call


def _stage_call(stage: Any, item: Any, *, stage_index: int, item_index: int) -> Any:
    if isinstance(stage, AgentCall):
        return stage.with_input(item, key_by=_default_item_key(item) or f"{stage_index}-{item_index}")
    if isinstance(stage, ApprovalStage):
        return stage(item)
    if callable(stage):
        return stage(item)
    raise TypeError(f"unsupported pipeline stage: {type(stage).__name__}")


def _context_manifest(context: Any) -> list[dict[str, Any]]:
    if context is None:
        return []
    if isinstance(context, ContextBundle):
        return [context.to_manifest()]
    if isinstance(context, Mapping):
        if "label" in context and "content" in context:
            bundle = ContextBundle(
                label=str(context["label"]),
                content=context.get("content"),
                source=str(context["source"]) if context.get("source") is not None else None,
                sha256=str(context["sha256"]) if context.get("sha256") is not None else None,
            )
            return [bundle.to_manifest()]
        return [{"label": "context", "content": _jsonable(context), "sha256": _sha256_json(context)}]
    if isinstance(context, Sequence) and not isinstance(context, (str, bytes, bytearray)):
        bundles: list[dict[str, Any]] = []
        for index, item in enumerate(context):
            if isinstance(item, ContextBundle):
                bundles.append(item.to_manifest())
            elif isinstance(item, Mapping) and "label" in item and "content" in item:
                bundles.extend(_context_manifest(item))
            else:
                jsonable = _jsonable(item)
                bundles.append({"label": f"context:{index}", "content": jsonable, "sha256": _sha256_json(jsonable)})
        return bundles
    jsonable = _jsonable(context)
    return [{"label": "context", "content": jsonable, "sha256": _sha256_json(jsonable)}]


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
    if isinstance(value, ApprovalDecision):
        return _jsonable(value.to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


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

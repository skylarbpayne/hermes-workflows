from __future__ import annotations

from functools import wraps
from typing import Any, Awaitable, Callable, Dict

_STEP_REGISTRY: Dict[str, Callable[..., Any]] = {}


def workflow(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Mark a plain async function as a durable workflow decider."""

    setattr(fn, "__workflow_name__", fn.__name__)
    from .engine import register_workflow

    return register_workflow(fn)


def step(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap an async function so workflow calls become durable step awaits.

    The decider never runs the step body inline. It records a StepRequested
    event/outbox command and exits. A worker later executes the registered body
    and reports StepCompleted.
    """

    _STEP_REGISTRY[fn.__name__] = fn

    @wraps(fn)
    async def wrapper(ctx: Any, *args: Any, **kwargs: Any) -> Any:
        return await ctx.run_step(fn.__name__, args, kwargs)

    setattr(wrapper, "__step_name__", fn.__name__)
    setattr(wrapper, "__step_body__", fn)
    return wrapper


def get_step_body(step_name: str) -> Callable[..., Any]:
    try:
        return _STEP_REGISTRY[step_name]
    except KeyError as exc:
        raise KeyError(f"unknown step body: {step_name}") from exc

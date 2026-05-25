from __future__ import annotations

from functools import wraps
from typing import Any, Awaitable, Callable


def workflow(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Mark a plain async function as a durable workflow decider."""

    setattr(fn, "__workflow_name__", fn.__name__)
    from .engine import register_workflow

    return register_workflow(fn)


def step(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap an async function so workflow calls become durable step awaits.

    In v0 the step body is not executed by the decider. A worker/adapter is
    expected to perform the command and report completion separately.
    """

    @wraps(fn)
    async def wrapper(ctx: Any, *args: Any, **kwargs: Any) -> Any:
        return await ctx.run_step(fn.__name__, args, kwargs)

    setattr(wrapper, "__step_name__", fn.__name__)
    setattr(wrapper, "__step_body__", fn)
    return wrapper

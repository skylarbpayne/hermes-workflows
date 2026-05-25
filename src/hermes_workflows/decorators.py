from __future__ import annotations

from functools import wraps
from typing import Any, Awaitable, Callable, Dict, Generator

_STEP_REGISTRY: Dict[str, Callable[..., Any]] = {}


class DurableStepCall:
    """Awaitable placeholder for a durable step invocation.

    Direct `await step(ctx, ...)` still works, while `ctx.gather(...)` can inspect
    multiple calls and enqueue all missing steps before the workflow exits.
    """

    __durable_step_call__ = True

    def __init__(
        self,
        ctx: Any,
        step_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        payload_builder: Callable[[], dict[str, Any]] | None = None,
    ):
        self.ctx = ctx
        self.step_name = step_name
        self.args = args
        self.kwargs = kwargs
        self.payload_builder = payload_builder

    def __await__(self) -> Generator[Any, None, Any]:
        return self.ctx.run_step(
            self.step_name,
            self.args,
            self.kwargs,
            payload_builder=self.payload_builder,
        ).__await__()


def workflow(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Mark a plain async function as a durable workflow decider."""

    setattr(fn, "__workflow_name__", fn.__name__)
    from .engine import register_workflow

    return register_workflow(fn)


def step(fn: Callable[..., Awaitable[Any]]) -> Callable[..., DurableStepCall]:
    """Wrap an async function so workflow calls become durable step awaits.

    The decider never runs the step body inline. It records a StepRequested
    event/outbox command and exits. A worker later executes the registered body
    and reports StepCompleted.
    """

    _STEP_REGISTRY[fn.__name__] = fn

    @wraps(fn)
    def wrapper(ctx: Any, *args: Any, **kwargs: Any) -> DurableStepCall:
        return DurableStepCall(ctx, fn.__name__, args, kwargs)

    setattr(wrapper, "__step_name__", fn.__name__)
    setattr(wrapper, "__step_body__", fn)
    return wrapper


def get_step_body(step_name: str) -> Callable[..., Any]:
    try:
        return _STEP_REGISTRY[step_name]
    except KeyError as exc:
        raise KeyError(f"unknown step body: {step_name}") from exc

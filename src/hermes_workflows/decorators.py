from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from typing import Any, Awaitable, Callable, Dict, Generator

_STEP_REGISTRY: Dict[str, Callable[..., Any]] = {}
_REGISTRATION_NAMESPACE: str | None = None


@contextmanager
def registration_namespace(namespace: str):
    """Temporarily namespace workflow/step registration names.

    Generated Python modules still use the normal public decorators, but their
    durable names must not collide with static workflow/step names. Importing a
    generated module runs decorators at import time, so the namespace has to be
    active while `exec_module(...)` runs.
    """

    global _REGISTRATION_NAMESPACE
    previous = _REGISTRATION_NAMESPACE
    _REGISTRATION_NAMESPACE = namespace
    try:
        yield
    finally:
        _REGISTRATION_NAMESPACE = previous


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

    workflow_name = f"{_REGISTRATION_NAMESPACE}:{fn.__name__}" if _REGISTRATION_NAMESPACE else fn.__name__
    setattr(fn, "__workflow_name__", workflow_name)
    from .engine import register_workflow

    return register_workflow(fn)


def step(fn: Callable[..., Awaitable[Any]]) -> Callable[..., DurableStepCall]:
    """Wrap an async function so workflow calls become durable step awaits.

    The decider never runs the step body inline. It records a StepRequested
    event/outbox command and exits. A worker later executes the registered body
    and reports StepCompleted.
    """

    step_name = f"{_REGISTRATION_NAMESPACE}:{fn.__name__}" if _REGISTRATION_NAMESPACE else fn.__name__
    _STEP_REGISTRY[step_name] = fn

    @wraps(fn)
    def wrapper(ctx: Any, *args: Any, **kwargs: Any) -> DurableStepCall:
        return DurableStepCall(ctx, step_name, args, kwargs)

    setattr(wrapper, "__step_name__", step_name)
    setattr(wrapper, "__step_body__", fn)
    return wrapper


def get_step_body(step_name: str) -> Callable[..., Any]:
    try:
        return _STEP_REGISTRY[step_name]
    except KeyError as exc:
        raise KeyError(f"unknown step body: {step_name}") from exc

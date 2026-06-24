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
    """Awaitable placeholder for a durable step invocation."""

    __durable_step_call__ = True

    def __init__(
        self,
        runtime_context: Any | None,
        step_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        payload_builder: Callable[[], dict[str, Any]] | None = None,
    ):
        self.runtime_context = runtime_context
        self.step_name = step_name
        self.args = args
        self.kwargs = kwargs
        self.payload_builder = payload_builder

    def __await__(self) -> Generator[Any, None, Any]:
        runtime_context = self.runtime_context
        if runtime_context is None:
            from .authoring import current_context

            runtime_context = current_context()
        return runtime_context.run_step(
            self.step_name,
            self.args,
            self.kwargs,
            payload_builder=self.payload_builder,
        ).__await__()


def workflow(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Mark a plain async function as a durable workflow decider.

    The returned function is still just the user's async decider, with a small
    direct-run helper attached so a workflow file can end with:

        if __name__ == "__main__":
            my_workflow.run()

    Running that file with ``uv run workflow.py`` uses the same durable runtime
    and default DB as the ``hermes-workflows run`` CLI.
    """

    workflow_name = f"{_REGISTRATION_NAMESPACE}:{fn.__name__}" if _REGISTRATION_NAMESPACE else fn.__name__
    setattr(fn, "__workflow_name__", workflow_name)
    from .input_parsing import workflow_input_type

    setattr(fn, "__workflow_input_type__", workflow_input_type(fn))

    def run(argv: list[str] | None = None) -> int:
        from .runner_api import workflow_run_cli

        return workflow_run_cli(fn, argv, workflow_ref=workflow_name)

    setattr(fn, "run", run)
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
    def wrapper(*args: Any, **kwargs: Any) -> DurableStepCall:
        runtime_context = None
        call_args = args
        if args and _looks_like_workflow_context(args[0]):
            runtime_context = args[0]
            call_args = args[1:]
        return DurableStepCall(runtime_context, step_name, call_args, kwargs)

    setattr(wrapper, "__step_name__", step_name)
    setattr(wrapper, "__step_body__", fn)
    return wrapper


def _looks_like_workflow_context(value: Any) -> bool:
    return hasattr(value, "run_step") and hasattr(value, "workflow_id")


def get_step_body(step_name: str) -> Callable[..., Any]:
    try:
        return _STEP_REGISTRY[step_name]
    except KeyError as exc:
        raise KeyError(f"unknown step body: {step_name}") from exc

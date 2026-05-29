from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, Union

from .decorators import DurableStepCall, step
from .workflow_values import Workflow, workflow_from_agent_output

_PLACEHOLDER = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
PromptPath = Union[str, PathLike[str]]


@dataclass(frozen=True, init=False)
class AgentPrompt:
    """A durable render-only prompt-file step.

    The prompt file is read only when the step is first requested. The rendered
    prompt packet is then stored in normal workflow history like any other step
    result, so replay does not depend on the file still existing.
    """

    path: PromptPath
    variables: dict[str, Any]

    def __init__(self, path: PromptPath, **variables: Any):
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "variables", dict(variables))

    def __call__(self, ctx: Any) -> DurableStepCall:
        return DurableStepCall(
            ctx,
            "agent_prompt",
            (),
            {},
            payload_builder=lambda: build_agent_prompt_payload(self.path, self.variables),
        )


@dataclass(frozen=True, init=False)
class AgentStep:
    """A durable agent step with typed return coercion.

    The runner is injectable/testable. `mock_output` remains available for
    examples and deterministic tests, while a configured engine `agent_runner`
    receives a snapshotted request packet and returns a durable output.
    """

    name: str
    prompt: str
    returns: Any
    variables: dict[str, Any]
    mock_output: Any

    def __init__(
        self,
        name: str,
        *,
        prompt: str,
        returns: Any = dict,
        variables: dict[str, Any] | None = None,
        mock_output: Any = None,
    ):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "returns", returns)
        object.__setattr__(self, "variables", dict(variables or {}))
        object.__setattr__(self, "mock_output", mock_output)

    def __call__(self, ctx: Any) -> DurableStepCall:
        return DurableStepCall(
            ctx,
            "agent_step",
            (),
            {},
            payload_builder=lambda: build_agent_step_payload(
                self.name,
                self.prompt,
                self.returns,
                self.variables,
                self.mock_output,
            ),
        )


def build_agent_prompt_payload(path: PromptPath, variables: dict[str, Any]) -> dict[str, Any]:
    prompt_path = Path(path)
    prompt_text = prompt_path.read_text(encoding="utf-8")
    safe_variables = _json_roundtrip(variables)
    rendered_prompt = render_prompt(prompt_text, safe_variables)
    request = {
        "kind": "agent_prompt.request.v1",
        "prompt_path": str(prompt_path),
        "prompt_text": prompt_text,
        "prompt_sha256": _sha256_text(prompt_text),
        "variables": safe_variables,
        "variables_sha256": _sha256_json(safe_variables),
        "rendered_prompt": rendered_prompt,
        "rendered_prompt_sha256": _sha256_text(rendered_prompt),
    }
    return {"step_name": "agent_prompt", "args": [request], "kwargs": {}}


def build_agent_step_payload(
    name: str,
    prompt: str,
    returns: Any,
    variables: dict[str, Any],
    mock_output: Any,
) -> dict[str, Any]:
    safe_variables = _json_roundtrip(variables)
    return_kind = "workflow" if returns is Workflow else "json"
    request = {
        "kind": "agent_step.request.v1",
        "name": name,
        "prompt": prompt,
        "prompt_sha256": _sha256_text(prompt),
        "variables": safe_variables,
        "variables_sha256": _sha256_json(safe_variables),
        "returns": return_kind,
        "mock_output": mock_output,
    }
    return {"step_name": "agent_step", "args": [request], "kwargs": {}}


@step
async def agent_prompt(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Return the request-time rendered prompt packet.

    V0 intentionally does not call an LLM or external agent. Later live runner
    support can build on this request packet without weakening durability.
    """

    return {
        "kind": "agent_prompt.rendered.v1",
        "prompt_path": request["prompt_path"],
        "prompt_sha256": request["prompt_sha256"],
        "variables_sha256": request["variables_sha256"],
        "rendered_prompt_sha256": request["rendered_prompt_sha256"],
        "variables": request["variables"],
        "rendered_prompt": request["rendered_prompt"],
    }


@step
async def agent_step(ctx: Any, request: dict[str, Any]) -> Any:
    """Execute an agent request and coerce its typed return value.

    Live runs persist the exact request/response/provenance as StepCompleted
    metadata. Generated Workflow values are snapshotted and validated here, but
    are marked approval-required so import/execution fails closed until a human
    approval decision wakes the parent workflow.
    """

    mock_output = request.get("mock_output")
    live = mock_output is None and ctx.engine.agent_runner is not None
    metadata = None
    provenance = None
    if live:
        from .engine import StepOutput

        agent_runner = ctx.engine.agent_runner
        if agent_runner is None:
            raise RuntimeError("agent_step live runner requested but engine.agent_runner is not configured")
        runner_request = _build_runner_request(ctx, request)
        runner_response = agent_runner(runner_request)
        if inspect.isawaitable(runner_response):
            runner_response = await runner_response
        if isinstance(runner_response, dict) and "output" in runner_response:
            output = runner_response["output"]
            provenance = runner_response.get("provenance")
        else:
            output = runner_response
        metadata = {
            "kind": "agent_step.live_result.v1",
            "request": runner_request,
            "response": runner_response,
            "provenance": provenance,
        }
    else:
        output = mock_output

    if output is None:
        output = {
            "kind": "agent_step.rendered.v1",
            "name": request["name"],
            "prompt": request["prompt"],
            "variables": request["variables"],
        }
    if request.get("returns") == "workflow":
        workflow = workflow_from_agent_output(
            output,
            base_dir=ctx.engine.db_path.parent,
            provenance=(
                {
                    "runner_provenance": provenance,
                    "request": metadata["request"],
                    "response": metadata["response"],
                }
                if live and metadata is not None
                else None
            ),
            approval_required=live,
        )
        return StepOutput(workflow, metadata) if live else workflow
    return StepOutput(output, metadata) if live else output


def _build_runner_request(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    rendered_prompt = render_prompt(request["prompt"], request["variables"])
    return {
        "kind": "agent_step.runner_request.v1",
        "name": request["name"],
        "prompt": request["prompt"],
        "prompt_sha256": request["prompt_sha256"],
        "rendered_prompt": rendered_prompt,
        "rendered_prompt_sha256": _sha256_text(rendered_prompt),
        "variables": request["variables"],
        "variables_sha256": request["variables_sha256"],
        "returns": request["returns"],
        "workflow_id": ctx.workflow_id,
        "step_key": ctx.step_key,
    }


def render_prompt(template: str, variables: dict[str, Any]) -> str:
    missing = sorted({name for name in _PLACEHOLDER.findall(template) if name not in variables})
    if missing:
        raise KeyError("missing prompt variables: " + ", ".join(missing))

    def replace(match: re.Match[str]) -> str:
        return _render_value(variables[match.group(1)])

    return _PLACEHOLDER.sub(replace, template)


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, indent=2, sort_keys=True)
    return str(value)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(json.dumps(_json_roundtrip(value), sort_keys=True, separators=(",", ":")))


def _json_roundtrip(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True))

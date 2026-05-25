from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any

from .decorators import DurableStepCall, step

_PLACEHOLDER = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
PromptPath = str | PathLike[str]


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

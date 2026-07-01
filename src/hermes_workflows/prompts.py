from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .decorators import step
from .types import to_json_value
from .workflow_values import workflow_from_agent_output

_PLACEHOLDER = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


@dataclass(frozen=True)
class RenderedPrompt:
    template_path: str
    template_text: str
    template_sha256: str
    variables_sha256: str
    rendered_prompt: str
    rendered_prompt_sha256: str
    include_rendered_text: bool = True

    def __str__(self) -> str:
        return self.rendered_prompt

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "prompt.rendered.v1",
            "template_path": self.template_path,
            "prompt_path": self.template_path,
            "template_sha256": self.template_sha256,
            "prompt_sha256": self.template_sha256,
            "variables_sha256": self.variables_sha256,
            "rendered_prompt_sha256": self.rendered_prompt_sha256,
        }
        if self.include_rendered_text:
            payload["template_text"] = self.template_text
            payload["prompt_text"] = self.template_text
            payload["rendered_prompt"] = self.rendered_prompt
        return payload

    def to_agent_request_fields(self) -> dict[str, Any]:
        return {
            "prompt": self.template_text,
            "prompt_sha256": self.template_sha256,
            "rendered_prompt": self.rendered_prompt,
            "rendered_prompt_sha256": self.rendered_prompt_sha256,
            "prompt_path": self.template_path,
            "template_path": self.template_path,
            "template_sha256": self.template_sha256,
            "variables_sha256": self.variables_sha256,
        }


@dataclass(frozen=True)
class PromptFile:
    path: Path

    def render(self, *, include_rendered_text: bool = True, **variables: Any) -> RenderedPrompt:
        template_text = self.path.read_text()
        rendered = render_prompt(template_text, variables)
        return RenderedPrompt(
            template_path=str(self.path),
            template_text=template_text,
            template_sha256=_sha256_text(template_text),
            variables_sha256=_sha256_json(variables),
            rendered_prompt=rendered,
            rendered_prompt_sha256=_sha256_text(rendered),
            include_rendered_text=include_rendered_text,
        )


def prompt_file(path: str | Path, *, base_dir: str | Path | None = None) -> PromptFile:
    template_path = Path(path).expanduser()
    if not template_path.is_absolute():
        if base_dir is None:
            caller = inspect.currentframe().f_back  # type: ignore[union-attr]
            base_dir = Path(caller.f_code.co_filename).parent if caller is not None else Path.cwd()
        template_path = Path(base_dir).expanduser() / template_path
    return PromptFile(template_path.resolve())


@step
async def agent(ctx: Any, request: dict[str, Any]) -> Any:
    """Execute a durable agent request and coerce its typed return value."""

    mock_output = request.get("mock_output")
    live = mock_output is None and ctx.engine.agent_runner is not None
    metadata = None
    provenance = None
    if live:
        from .engine import StepOutput

        agent_runner = ctx.engine.agent_runner
        if agent_runner is None:
            raise RuntimeError("agent live runner requested but engine.agent_runner is not configured")
        max_attempts = _max_attempts(request)
        retry_history: list[dict[str, Any]] = []
        last_error: AgentOutputSchemaError | None = None
        last_output: Any = None
        for attempt in range(1, max_attempts + 1):
            runner_request = _build_runner_request(ctx, request)
            if last_error is not None:
                runner_request = _with_retry_feedback(
                    runner_request,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=last_error,
                    previous_output=last_output,
                )
            runner_response = agent_runner(runner_request)
            if inspect.isawaitable(runner_response):
                runner_response = await runner_response
            if isinstance(runner_response, dict) and "output" in runner_response:
                output = runner_response["output"]
                provenance = runner_response.get("provenance")
            else:
                output = runner_response
            try:
                _validate_output_schema(output, request.get("returns_schema"))
            except AgentOutputSchemaError as exc:
                last_error = exc
                last_output = output
                retry_history.append(
                    {
                        "attempt": attempt,
                        "error": exc.to_json(),
                        "output": _brief_json(output),
                    }
                )
                if attempt < max_attempts:
                    continue
                raise
            metadata = {
                "kind": "agent.live_result.v1",
                "request": runner_request,
                "response": runner_response,
                "provenance": provenance,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "retry_history": retry_history,
            }
            break
        else:  # pragma: no cover - range is non-empty after _max_attempts validation.
            raise RuntimeError("agent runner retry loop exhausted without output")
    else:
        output = mock_output

    if output is None:
        output = {
            "kind": "agent.rendered.v1",
            "name": request["name"],
            "prompt": request["prompt"],
            "input": request.get("input"),
        }
    if request.get("returns") in {"workflow", "hermes_workflows.workflow_values:Workflow"}:
        workflow = workflow_from_agent_output(
            output,
            base_dir=ctx.engine.db_path.parent,
            provenance=(
                {
                    "runner_provenance": provenance,
                    "request": metadata["request"],
                    "response": metadata["response"],
                    "retry_history": metadata.get("retry_history", []),
                }
                if live and metadata is not None
                else None
            ),
            approval_required=live,
        )
        return StepOutput(workflow, metadata) if live else workflow
    return StepOutput(output, metadata) if live else output


def _build_runner_request(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    rendered_prompt = request.get("rendered_prompt") or request["prompt"]
    runner_request = {
        "kind": "agent.runner_request.v1",
        "name": request["name"],
        "prompt": request["prompt"],
        "prompt_sha256": request["prompt_sha256"],
        "rendered_prompt": rendered_prompt,
        "rendered_prompt_sha256": request.get("rendered_prompt_sha256") or _sha256_text(rendered_prompt),
        "returns": request["returns"],
        "returns_schema": request.get("returns_schema"),
        "workflow_id": ctx.workflow_id,
        "step_key": ctx.step_key,
    }
    for key in (
        "input",
        "input_sha256",
        "fingerprint",
        "prompt_path",
        "template_path",
        "template_sha256",
        "variables_sha256",
        "tools",
        "skills",
        "files",
        "workspace_dir",
        "model",
        "variant",
        "isolation",
        "timeout",
        "budget",
        "max_attempts",
        "public_name",
        "public_label",
        "name_source",
    ):
        if key in request:
            runner_request[key] = request[key]
    return runner_request


class AgentOutputSchemaError(TypeError):
    def __init__(self, message: str, *, path: str = "output") -> None:
        self.path = path
        self.message = message
        super().__init__(f"agent output schema validation failed at {path}: {message}")

    def to_json(self) -> dict[str, str]:
        return {"type": type(self).__name__, "path": self.path, "message": self.message}


def _max_attempts(request: Mapping[str, Any]) -> int:
    raw = request.get("max_attempts", 2)
    try:
        attempts = int(raw)
    except (TypeError, ValueError) as exc:
        raise TypeError("agent max_attempts must be an integer") from exc
    if attempts < 1:
        raise TypeError("agent max_attempts must be at least 1")
    return min(attempts, 5)


def _with_retry_feedback(
    runner_request: dict[str, Any],
    *,
    attempt: int,
    max_attempts: int,
    error: AgentOutputSchemaError,
    previous_output: Any,
) -> dict[str, Any]:
    retry = {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "reason": "previous_output_failed_schema_validation",
        "error": error.to_json(),
        "previous_output": _brief_json(previous_output),
    }
    instruction = (
        f"\n\n---\nRETRY ATTEMPT {attempt} OF {max_attempts}\n"
        "The previous agent output failed the workflow's typed return schema.\n"
        f"Validation error: {error}\n"
        f"Previous output: {_brief_json(previous_output)}\n"
        "Return a corrected JSON-compatible output matching returns_schema. Do not explain.\n"
    )
    updated = dict(runner_request)
    updated["retry"] = retry
    updated["rendered_prompt"] = f"{runner_request.get('rendered_prompt') or runner_request.get('prompt') or ''}{instruction}"
    updated["rendered_prompt_sha256"] = _sha256_text(updated["rendered_prompt"])
    return updated


def _validate_output_schema(output: Any, schema: Any, *, path: str = "output") -> None:
    if not isinstance(schema, Mapping):
        return
    kind = schema.get("kind")
    if kind == "json_object" or schema.get("name") == "json":
        return
    if kind == "list":
        if not isinstance(output, list):
            raise AgentOutputSchemaError(f"expected list, got {type(output).__name__}", path=path)
        item_schema = schema.get("items")
        for index, item in enumerate(output):
            _validate_output_schema(item, item_schema, path=f"{path}[{index}]")
        return
    if kind == "structured_object":
        if not isinstance(output, Mapping):
            raise AgentOutputSchemaError(f"expected object, got {type(output).__name__}", path=path)
        if schema.get("name") == "Workflow" and schema.get("module") == "hermes_workflows.workflow_values":
            missing = [field for field in ("source", "symbol") if field not in output]
            if missing:
                raise AgentOutputSchemaError(f"missing required field(s): {', '.join(missing)}; expected fields: source, symbol", path=path)
            return
        fields = schema.get("fields") or []
        if not isinstance(fields, list):
            return
        missing = [str(field.get("name")) for field in fields if isinstance(field, Mapping) and field.get("required") and field.get("name") not in output]
        if missing:
            expected = [str(field.get("name")) for field in fields if isinstance(field, Mapping) and field.get("name")]
            raise AgentOutputSchemaError(
                f"missing required field(s): {', '.join(missing)}; expected fields: {', '.join(expected)}",
                path=path,
            )
        for field in fields:
            if not isinstance(field, Mapping):
                continue
            name = field.get("name")
            if not isinstance(name, str) or name not in output:
                continue
            _validate_field_value(output[name], field, path=f"{path}.{name}")


def _validate_field_value(value: Any, field: Mapping[str, Any], *, path: str) -> None:
    kind = field.get("kind")
    if kind == "text" and not isinstance(value, str):
        raise AgentOutputSchemaError(f"expected text, got {type(value).__name__}", path=path)
    if kind == "boolean" and not isinstance(value, bool):
        raise AgentOutputSchemaError(f"expected boolean, got {type(value).__name__}", path=path)
    if kind == "number" and not isinstance(value, (int, float)):
        raise AgentOutputSchemaError(f"expected number, got {type(value).__name__}", path=path)
    if kind == "choice" and "options" in field and value not in set(field.get("options") or []):
        raise AgentOutputSchemaError(f"expected one of {list(field.get('options') or [])!r}, got {value!r}", path=path)


def _brief_json(value: Any, *, max_chars: int = 2000) -> str:
    try:
        text = json.dumps(to_json_value(value), ensure_ascii=False, sort_keys=True)
    except Exception:
        text = repr(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 15] + "...[truncated]"


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
    return json.loads(json.dumps(to_json_value(value), sort_keys=True))


def _jsonable(value: Any) -> Any:
    return to_json_value(value)

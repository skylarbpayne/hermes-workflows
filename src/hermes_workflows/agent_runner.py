from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agent_cli_adapter import (
    AdapterError,
    build_provider_prompt,
    expand_model_arg_templates,
    parse_provider_response,
    redacted_error,
    run_agent_command,
)


class AgentRunnerError(RuntimeError):
    """Raised when an external agent runner fails closed.

    ``details`` contains a redacted diagnostic payload suitable for receipts and
    test assertions. It must not include raw provider environment values.
    """

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class SubprocessAgentRunner:
    """Canonical subprocess-backed runner for ``agent(...)`` requests.

    The runner accepts durable ``agent.runner_request.v1`` dictionaries and
    invokes a trusted provider command. It is the implementation used by the
    Workflow Worker path and by advanced public imports. Use
    ``request_stdin_mode="json"`` for provider commands that consume the raw
    runner request; use ``request_stdin_mode="prompt"`` for provider commands
    that should receive a rendered instruction prompt.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        model_arg_templates: Sequence[str] = (),
        request_stdin_mode: str = "json",
        timeout_seconds: float = 300.0,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        max_stdout_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
        max_agent_stdout_bytes: int | None = None,
        max_agent_stderr_bytes: int | None = None,
        provenance_runner_name: str = "hermes_workflows.subprocess_agent_runner",
        response_mode: str = "passthrough",
    ) -> None:
        if isinstance(argv, (str, bytes)):
            raise TypeError("SubprocessAgentRunner command must be an argv sequence, not a shell string")
        self.argv = [str(part) for part in argv]
        self.command = self.argv  # Backward-compatible public attribute.
        self.model_arg_templates = [str(part) for part in model_arg_templates]
        self.request_stdin_mode = request_stdin_mode
        self.timeout_seconds = float(timeout_seconds)
        self.cwd = Path(cwd).expanduser() if cwd is not None else None
        self.env = {str(key): str(value) for key, value in (env or {}).items()}
        stdout_limit = max_agent_stdout_bytes if max_agent_stdout_bytes is not None else max_stdout_bytes
        stderr_limit = max_agent_stderr_bytes if max_agent_stderr_bytes is not None else max_stderr_bytes
        self.max_agent_stdout_bytes = int(stdout_limit if stdout_limit is not None else 1_000_000)
        self.max_agent_stderr_bytes = int(stderr_limit if stderr_limit is not None else 4096)
        self.max_stdout_bytes = self.max_agent_stdout_bytes  # Backward-compatible public attribute.
        self.provenance_runner_name = provenance_runner_name
        self.response_mode = response_mode
        if not self.argv:
            raise ValueError("agent runner command must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("agent runner timeout must be positive")
        if self.max_agent_stdout_bytes <= 0:
            raise ValueError("max_stdout_bytes must be positive")
        if self.max_agent_stderr_bytes <= 0:
            raise ValueError("max_stderr_bytes must be positive")
        if self.request_stdin_mode not in {"prompt", "json"}:
            raise ValueError("agent runner request_stdin_mode must be 'prompt' or 'json'")
        if self.response_mode not in {"passthrough", "runner"}:
            raise ValueError("agent runner response_mode must be 'passthrough' or 'runner'")

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        argv = [*self.argv]
        provider_result = None
        try:
            argv = [*argv, *expand_model_arg_templates(self.model_arg_templates, request)]
            stdin_payload = self._stdin_payload(request)
            provider_result = run_agent_command(
                argv,
                stdin_payload,
                self.timeout_seconds,
                self.max_agent_stdout_bytes,
                self.max_agent_stderr_bytes,
                cwd=self._request_cwd(request),
                env=self.env or None,
            )
            if provider_result.timed_out:
                raise AdapterError(
                    "provider_timeout",
                    f"agent runner timed out after {self.timeout_seconds:g}s",
                    provider_result=provider_result,
                )
            if provider_result.stdout_exceeded:
                raise AdapterError(
                    "provider_stdout_exceeded",
                    f"agent runner stdout exceeded {self.max_agent_stdout_bytes} bytes",
                    provider_result=provider_result,
                )
            if provider_result.stderr_exceeded:
                raise AdapterError(
                    "provider_stderr_exceeded",
                    f"agent runner stderr exceeded {self.max_agent_stderr_bytes} bytes",
                    provider_result=provider_result,
                )
            if provider_result.exit_code is None:
                raise AdapterError("provider_start_failed", "agent runner could not start", provider_result=provider_result)
            if provider_result.exit_code != 0:
                raise AdapterError(
                    "provider_nonzero_exit",
                    f"agent runner exited with code {provider_result.exit_code}",
                    provider_result=provider_result,
                )
            try:
                stdout_text = provider_result.stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AdapterError(
                    "provider_invalid_utf8",
                    "agent runner returned invalid UTF-8 on stdout",
                    provider_result=provider_result,
                ) from exc
            if self.response_mode == "passthrough":
                return _parse_passthrough_response(stdout_text, provider_result, self.argv)
            args = argparse.Namespace(provenance_runner_name=self.provenance_runner_name)
            return parse_provider_response(stdout_text, request, provider_result, args)
        except AdapterError as exc:
            message = _public_error_message(exc)
            diagnostic = redacted_error(
                exc.code,
                message,
                argv=argv,
                duration_ms=int((time.monotonic() - started) * 1000),
                request=request,
                provider_result=exc.provider_result or provider_result,
            )
            if exc.code == "provider_timeout":
                diagnostic["timeout_seconds"] = self.timeout_seconds
            raise AgentRunnerError(diagnostic["message"], details=diagnostic) from exc

    def _stdin_payload(self, request: dict[str, Any]) -> str:
        if self.request_stdin_mode == "json":
            try:
                return json.dumps(request, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise AdapterError("invalid_runner_request", "agent runner request is not JSON serializable") from exc
        return build_provider_prompt(request)

    def _request_cwd(self, request: dict[str, Any]) -> Path | None:
        workspace_dir = request.get("workspace_dir")
        if workspace_dir in (None, ""):
            return self.cwd
        if not isinstance(workspace_dir, str):
            raise AdapterError("invalid_runner_request", "agent runner request workspace_dir must be a string")
        path = Path(workspace_dir).expanduser()
        if not path.is_absolute():
            raise AdapterError("invalid_runner_request", "agent runner request workspace_dir must be an absolute path")
        if not path.exists() or not path.is_dir():
            raise AdapterError("invalid_runner_request", "agent runner request workspace_dir must exist and be a directory")
        return path


def _parse_passthrough_response(stdout_text: str, provider_result: Any, argv: Sequence[str]) -> dict[str, Any]:
    try:
        response = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise AdapterError("provider_invalid_json", "agent runner returned invalid JSON on stdout", provider_result=provider_result) from exc
    if not isinstance(response, dict):
        raise AdapterError("provider_invalid_response", "agent runner response must be a JSON object", provider_result=provider_result)
    if "output" not in response:
        raise AdapterError("provider_invalid_response", "agent runner response must include an 'output' field", provider_result=provider_result)
    if response.get("provenance") is not None and not isinstance(response.get("provenance"), dict):
        raise AdapterError("provider_invalid_response", "agent runner provenance must be a JSON object when provided", provider_result=provider_result)
    if response.get("provenance") is None:
        response["provenance"] = {
            "runner": "subprocess",
            "command": Path(argv[0]).name if argv else "",
            "duration_ms": provider_result.duration_ms,
        }
    return response


def _public_error_message(exc: AdapterError) -> str:
    if exc.code == "provider_invalid_json":
        return "agent runner returned invalid JSON on stdout"
    if exc.code == "provider_invalid_response" and "include output" in exc.message:
        return "agent runner response must include an 'output' field"
    if exc.code == "provider_invalid_response" and "JSON object" in exc.message:
        return "agent runner response must be a JSON object"
    if exc.code == "provider_invalid_response" and "provenance" in exc.message:
        return "agent runner provenance must be a JSON object when provided"
    return exc.message


def build_agent_runner(
    *,
    agent_command: str | None,
    agent_args: Sequence[str] | None = None,
    agent_model_args: Sequence[str] | None = None,
    agent_request_stdin: str = "prompt",
    timeout_seconds: float = 120.0,
    max_stdout_bytes: int = 1_000_000,
    max_stderr_bytes: int = 4096,
    provenance_runner_name: str = "hermes_workflows.worker_agent_runner",
) -> SubprocessAgentRunner | None:
    if not agent_command:
        return None
    return SubprocessAgentRunner(
        [agent_command, *list(agent_args or [])],
        model_arg_templates=list(agent_model_args or []),
        request_stdin_mode=agent_request_stdin,
        timeout_seconds=timeout_seconds,
        max_agent_stdout_bytes=max_stdout_bytes,
        max_agent_stderr_bytes=max_stderr_bytes,
        provenance_runner_name=provenance_runner_name,
        response_mode="runner",
    )

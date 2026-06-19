from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .agent_cli_adapter import (
    AdapterError,
    build_provider_prompt,
    expand_model_arg_templates,
    parse_provider_response,
    redacted_error,
    run_agent_command,
)


@dataclass(frozen=True)
class SubprocessAgentRunner:
    """Run agent(...) requests through a strict JSON provider command.

    The provider command receives a prompt on stdin and must print one JSON object:
    {"output": <json>, "provenance": {...}}. The returned object is shaped like
    the in-process agent runner contract consumed by prompts.agent.
    """

    argv: Sequence[str]
    model_arg_templates: Sequence[str] = ()
    request_stdin_mode: str = "prompt"
    timeout_seconds: float = 120.0
    max_agent_stdout_bytes: int = 1_000_000
    max_agent_stderr_bytes: int = 4096
    provenance_runner_name: str = "hermes_workflows.worker_agent_runner"

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("agent runner command must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("agent runner timeout must be positive")
        if self.request_stdin_mode not in {"prompt", "json"}:
            raise ValueError("agent runner request_stdin_mode must be 'prompt' or 'json'")

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        argv = [str(part) for part in self.argv]
        provider_result = None
        try:
            argv = [*argv, *expand_model_arg_templates(self.model_arg_templates, request)]
            stdin_payload = (
                json.dumps(request, sort_keys=True, separators=(",", ":"))
                if self.request_stdin_mode == "json"
                else build_provider_prompt(request)
            )
            provider_result = run_agent_command(
                argv,
                stdin_payload,
                self.timeout_seconds,
                self.max_agent_stdout_bytes,
                self.max_agent_stderr_bytes,
            )
            if provider_result.timed_out:
                raise AdapterError(
                    "provider_timeout",
                    f"provider timed out after {self.timeout_seconds:g}s",
                    provider_result=provider_result,
                )
            if provider_result.stdout_exceeded:
                raise AdapterError(
                    "provider_stdout_exceeded",
                    f"provider stdout exceeded {self.max_agent_stdout_bytes} bytes",
                    provider_result=provider_result,
                )
            if provider_result.stderr_exceeded:
                raise AdapterError(
                    "provider_stderr_exceeded",
                    f"provider stderr exceeded {self.max_agent_stderr_bytes} bytes",
                    provider_result=provider_result,
                )
            if provider_result.exit_code is None:
                raise AdapterError("provider_start_failed", "provider command could not start", provider_result=provider_result)
            if provider_result.exit_code != 0:
                raise AdapterError(
                    "provider_nonzero_exit",
                    f"provider exited with code {provider_result.exit_code}",
                    provider_result=provider_result,
                )
            try:
                stdout_text = provider_result.stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AdapterError(
                    "provider_invalid_utf8",
                    "provider stdout was not valid UTF-8",
                    provider_result=provider_result,
                ) from exc
            args = argparse.Namespace(provenance_runner_name=self.provenance_runner_name)
            return parse_provider_response(stdout_text, request, provider_result, args)
        except AdapterError as exc:
            diagnostic = redacted_error(
                exc.code,
                exc.message,
                argv=argv,
                duration_ms=int((time.monotonic() - started) * 1000),
                request=request,
                provider_result=exc.provider_result or provider_result,
            )
            raise RuntimeError(json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))) from exc


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
    )

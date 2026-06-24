from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


ADAPTER_VERSION = 1
DEFAULT_RUNNER_NAME = "hermes_workflows.agent_cli_adapter"
SECRET_FLAGS = {"--api-key", "--token", "--password", "--secret", "--auth", "--cookie", "-k"}
SECRET_KEY_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|AUTH|COOKIE)", re.IGNORECASE)
STRONG_SECRET_ENV_KEY_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|COOKIE)", re.IGNORECASE)
NON_SECRET_ENV_KEY_SUFFIXES = ("USERNAME", "USER")


@dataclass(frozen=True)
class ProviderResult:
    argv: list[str]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_ms: int
    timed_out: bool = False
    stdout_exceeded: bool = False
    stderr_exceeded: bool = False


class AdapterError(Exception):
    def __init__(self, code: str, message: str, *, provider_result: ProviderResult | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider_result = provider_result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Strict JSON CLI adapter for hermes-workflows agent(...) runners.")
    parser.add_argument("--agent-command", required=True, help="Provider CLI executable/argv0.")
    parser.add_argument("--agent-arg", action="append", default=[], help="Provider CLI argv entry appended after --agent-command.")
    parser.add_argument(
        "--agent-model-arg",
        action="append",
        default=[],
        help="Provider CLI argv template appended only when request.model is set; repeat for multiple args. Use {model} as the model placeholder.",
    )
    parser.add_argument(
        "--agent-prompt-arg",
        help=(
            "Provider CLI flag that should receive the rendered agent prompt as the following argv value "
            "instead of stdin, e.g. --agent-prompt-arg --oneshot for Hermes."
        ),
    )
    parser.add_argument("--response-mode", choices=["json-object"], default="json-object")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-agent-stdout-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-agent-stderr-bytes", type=int, default=4096)
    parser.add_argument("--provenance-runner-name", default=DEFAULT_RUNNER_NAME)
    args = parser.parse_args(_normalize_agent_arg_options(argv))
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.max_agent_stdout_bytes <= 0:
        parser.error("--max-agent-stdout-bytes must be positive")
    if args.max_agent_stderr_bytes <= 0:
        parser.error("--max-agent-stderr-bytes must be positive")
    return args


def _normalize_agent_arg_options(argv: Sequence[str] | None) -> Sequence[str] | None:
    """Let `--agent-arg --provider-flag` work with argparse.

    argparse treats option-looking values after `--agent-arg` as new adapter
    options. The public contract intentionally uses repeated `--agent-arg`
    entries for provider argv, including provider flags like `--json`, so we
    rewrite only that spelling to the unambiguous `--agent-arg=<value>` form.
    `--agent-model-arg` has the same shape because model-specific provider
    flags commonly look like `--model`.
    """

    if argv is None:
        argv = sys.argv[1:]
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        item = str(argv[index])
        if item in {"--agent-arg", "--agent-model-arg", "--agent-prompt-arg"} and index + 1 < len(argv):
            normalized.append(f"{item}={argv[index + 1]}")
            index += 2
            continue
        normalized.append(item)
        index += 1
    return normalized


def expand_model_arg_templates(model_arg_templates: Sequence[str], request: dict[str, Any]) -> list[str]:
    """Expand opt-in provider argv templates for request.model.

    Providers do not agree on a standard model flag, so hermes-workflows only
    appends model argv when the operator configures one or more templates. Each
    configured argv entry is appended when `request["model"]` is a non-empty
    string, with literal `{model}` occurrences replaced by that model.
    Templates without `{model}` are allowed for providers that use a flag/value
    pair such as `--agent-model-arg --model --agent-model-arg {model}`.
    """

    model = request.get("model")
    if model is None or model == "" or not model_arg_templates:
        return []
    if not isinstance(model, str):
        raise AdapterError("invalid_runner_request", "request.model must be a string when present")
    return [str(template).replace("{model}", model) for template in model_arg_templates]


def load_runner_request(stdin_text: str) -> dict[str, Any]:
    try:
        request = json.loads(stdin_text)
    except json.JSONDecodeError as exc:
        raise AdapterError("invalid_runner_request_json", f"runner request stdin was not valid JSON: {exc.msg}") from exc
    if not isinstance(request, dict):
        raise AdapterError("invalid_runner_request", "runner request must be a JSON object")
    if request.get("kind") != "agent.runner_request.v1":
        raise AdapterError("invalid_runner_request", "runner request kind must be agent.runner_request.v1")
    return request


def _request_workspace_dir(request: dict[str, Any]) -> Path | None:
    workspace_dir = request.get("workspace_dir")
    if workspace_dir in (None, ""):
        return None
    if not isinstance(workspace_dir, str):
        raise AdapterError("invalid_runner_request", "request.workspace_dir must be a string when present")
    path = Path(workspace_dir).expanduser()
    if not path.is_absolute():
        raise AdapterError("invalid_runner_request", "request.workspace_dir must be an absolute path")
    if not path.exists() or not path.is_dir():
        raise AdapterError("invalid_runner_request", "request.workspace_dir must exist and be a directory")
    return path


def build_provider_prompt(request: dict[str, Any]) -> str:
    return (
        "You are being called by hermes-workflows agent(...).\n"
        "Return exactly one JSON object and no surrounding prose.\n\n"
        "Required response schema:\n"
        "{\n"
        '  "output": <JSON-compatible value>,\n'
        '  "provenance": {"model": "optional", "request_id": "optional", "notes": "optional non-secret text"}\n'
        "}\n\n"
        'If the requested return type is "workflow", output must be:\n'
        "{\n"
        '  "source": "Python source defining one @workflow",\n'
        '  "symbol": "workflow_function_name"\n'
        "}\n\n"
        "agent(...) request:\n"
        f"{json.dumps(request, indent=2, sort_keys=True)}\n"
    )


def run_agent_command(
    argv: list[str],
    prompt: str,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    cwd: str | Path | None = None,
) -> ProviderResult:
    started = time.monotonic()
    stdout = bytearray()
    stderr = bytearray()
    process: subprocess.Popen[bytes] | None = None

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(Path(cwd).expanduser()) if cwd is not None else None,
        )
    except OSError as exc:
        duration_ms = _duration_ms(started)
        return ProviderResult(argv=argv, exit_code=None, stdout=b"", stderr=str(exc).encode("utf-8", errors="replace"), duration_ms=duration_ms)

    try:
        prompt_bytes = prompt.encode("utf-8")
        prompt_offset = 0
        deadline = started + timeout_seconds
        selector = selectors.DefaultSelector()
        try:
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            if process.stdout is not None:
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            if process.stderr is not None:
                os.set_blocking(process.stderr.fileno(), False)
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")

            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    return ProviderResult(
                        argv=argv,
                        exit_code=process.returncode,
                        stdout=bytes(stdout),
                        stderr=bytes(stderr),
                        duration_ms=_duration_ms(started),
                        timed_out=True,
                    )
                events = selector.select(timeout=min(0.1, remaining))
                if not events:
                    continue
                for key, _ in events:
                    if key.data == "stdin":
                        try:
                            if prompt_offset < len(prompt_bytes):
                                prompt_offset += os.write(key.fd, prompt_bytes[prompt_offset : prompt_offset + 65536])
                            if prompt_offset >= len(prompt_bytes):
                                selector.unregister(key.fileobj)
                                if process.stdin is not None:
                                    process.stdin.close()
                        except (BrokenPipeError, OSError):
                            selector.unregister(key.fileobj)
                            try:
                                if process.stdin is not None:
                                    process.stdin.close()
                            except OSError:
                                pass
                        continue

                    if key.data == "stdout":
                        read_size = min(65536, max_stdout_bytes + 1 - len(stdout))
                    else:
                        read_size = min(65536, max_stderr_bytes + 1 - len(stderr))
                    if read_size <= 0:
                        process.kill()
                        process.wait()
                        return ProviderResult(
                            argv=argv,
                            exit_code=process.returncode,
                            stdout=bytes(stdout),
                            stderr=bytes(stderr),
                            duration_ms=_duration_ms(started),
                            stdout_exceeded=key.data == "stdout",
                            stderr_exceeded=key.data == "stderr",
                        )
                    chunk = os.read(key.fd, read_size)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        stdout.extend(chunk)
                        if len(stdout) > max_stdout_bytes:
                            process.kill()
                            process.wait()
                            return ProviderResult(
                                argv=argv,
                                exit_code=process.returncode,
                                stdout=bytes(stdout),
                                stderr=bytes(stderr),
                                duration_ms=_duration_ms(started),
                                stdout_exceeded=True,
                            )
                    else:
                        stderr.extend(chunk)
                        if len(stderr) > max_stderr_bytes:
                            process.kill()
                            process.wait()
                            return ProviderResult(
                                argv=argv,
                                exit_code=process.returncode,
                                stdout=bytes(stdout),
                                stderr=bytes(stderr),
                                duration_ms=_duration_ms(started),
                                stderr_exceeded=True,
                            )
        finally:
            selector.close()

        remaining = max(0.0, deadline - time.monotonic())
        try:
            exit_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return ProviderResult(
                argv=argv,
                exit_code=process.returncode,
                stdout=bytes(stdout),
                stderr=bytes(stderr),
                duration_ms=_duration_ms(started),
                timed_out=True,
            )
        return ProviderResult(argv=argv, exit_code=exit_code, stdout=bytes(stdout), stderr=bytes(stderr), duration_ms=_duration_ms(started))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def parse_provider_response(stdout: str, request: dict[str, Any], provider_result: ProviderResult, args: argparse.Namespace) -> dict[str, Any]:
    try:
        provider_response = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError("provider_invalid_json", f"provider stdout was not strict JSON: {exc.msg}", provider_result=provider_result) from exc
    if not isinstance(provider_response, dict):
        raise AdapterError("provider_invalid_response", "provider response must be a JSON object", provider_result=provider_result)
    if "output" not in provider_response:
        raise AdapterError("provider_invalid_response", "provider response must include output", provider_result=provider_result)
    provider_provenance = provider_response.get("provenance")
    if provider_provenance is not None and not isinstance(provider_provenance, dict):
        raise AdapterError("provider_invalid_response", "provider provenance must be an object when present", provider_result=provider_result)

    secrets = collect_redaction_values(provider_result.argv, request)
    provenance = {
        "runner": args.provenance_runner_name,
        "adapter_version": ADAPTER_VERSION,
        "agent_command": sanitized_command(provider_result.argv, secrets),
        "request_kind": request.get("kind"),
        "request_name": request.get("name"),
        "request_model": request.get("model"),
        "request_sha256": sha256_json(request),
        "rendered_prompt_sha256": request.get("rendered_prompt_sha256"),
        "provider_provenance": sanitize_provider_provenance(provider_provenance or {}, secrets),
        "duration_ms": provider_result.duration_ms,
        "exit_code": provider_result.exit_code,
    }
    return {"output": provider_response["output"], "provenance": provenance}


def redacted_error(
    code: str,
    message: str,
    *,
    argv: list[str],
    duration_ms: int,
    request: dict[str, Any] | None = None,
    provider_result: ProviderResult | None = None,
) -> dict[str, Any]:
    secrets = collect_redaction_values(argv, request)
    result = provider_result
    stdout = result.stdout if result is not None else b""
    stderr = result.stderr if result is not None else b""
    error: dict[str, Any] = {
        "kind": "agent_cli_adapter.error.v1",
        "error": code,
        "message": redact_text(message, secrets),
        "agent_command": sanitized_command(argv, secrets),
        "duration_ms": duration_ms if result is None else result.duration_ms,
    }
    if result is not None:
        error["exit_code"] = result.exit_code
        error["stdout_bytes"] = len(stdout)
        error["stderr_bytes"] = len(stderr)
    if stdout:
        error["stdout_tail"] = redact_text(stdout[-4096:].decode("utf-8", errors="replace"), secrets)
    if stderr:
        error["stderr_tail"] = redact_text(stderr[-4096:].decode("utf-8", errors="replace"), secrets)
    return error


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def collect_secret_values(argv: Sequence[str]) -> set[str]:
    secrets: set[str] = set()
    redact_next = False
    for raw in argv:
        arg = str(raw)
        if redact_next:
            if len(arg) >= 3:
                secrets.add(arg)
            redact_next = False
            continue
        if arg in SECRET_FLAGS:
            redact_next = True
            continue
        if "=" in arg:
            key, value = arg.split("=", 1)
            flag_key = key.split("/", 1)[-1]
            if key in SECRET_FLAGS or SECRET_KEY_RE.search(flag_key):
                if len(value) >= 3:
                    secrets.add(value)
    for key, value in os.environ.items():
        normalized_key = key.upper()
        username_only_key = any(normalized_key.endswith(suffix) for suffix in NON_SECRET_ENV_KEY_SUFFIXES)
        if username_only_key and not STRONG_SECRET_ENV_KEY_RE.search(key):
            continue
        if SECRET_KEY_RE.search(key) and len(value) >= 3:
            secrets.add(value)
    return secrets


def collect_redaction_values(argv: Sequence[str], request: dict[str, Any] | None = None) -> set[str]:
    values = collect_secret_values(argv)
    if request is not None:
        for key in ("prompt", "rendered_prompt"):
            value = request.get(key)
            if isinstance(value, str) and len(value) >= 8:
                values.add(value)
    return values


def sanitized_command(argv: Sequence[str], secrets: set[str] | None = None) -> dict[str, Any]:
    secrets = set(secrets or collect_secret_values(argv))
    redacted: list[str] = []
    redact_next = False
    for index, raw in enumerate(argv):
        arg = str(raw)
        if index == 0:
            redacted.append(Path(arg).name)
            continue
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if arg in SECRET_FLAGS:
            redacted.append(arg)
            redact_next = True
            continue
        if "=" in arg:
            key, value = arg.split("=", 1)
            if key in SECRET_FLAGS or SECRET_KEY_RE.search(key):
                redacted.append(f"{key}=[REDACTED]")
                if len(value) >= 3:
                    secrets.add(value)
                continue
        redacted.append(redact_text(arg, secrets))
    return {"argv0": Path(str(argv[0])).name if argv else "", "argv": redacted}


def sanitize_provider_provenance(value: dict[str, Any], secrets: set[str]) -> dict[str, Any]:
    safe_keys = {"model", "request_id", "id", "provider", "runner", "notes"}
    transcript_keys = {"transcript", "messages", "raw_prompt", "prompt", "rendered_prompt", "stdout", "stderr"}
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        normalized = key_text.lower()
        if normalized in transcript_keys or SECRET_KEY_RE.search(key_text):
            continue
        if key_text not in safe_keys:
            continue
        if isinstance(item, str):
            sanitized[key_text] = redact_text(item[:512], secrets)
        elif isinstance(item, bool) or isinstance(item, (int, float)) or item is None:
            sanitized[key_text] = item
    return sanitized


def sanitize_json_value(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [sanitize_json_value(item, secrets) for item in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            safe_key = "[REDACTED_KEY]" if SECRET_KEY_RE.search(key_text) else key_text
            sanitized[safe_key] = "[REDACTED]" if SECRET_KEY_RE.search(key_text) else sanitize_json_value(item, secrets)
        return sanitized
    return value


def redact_text(text: str, secrets: set[str] | None = None) -> str:
    redacted = text
    for secret in sorted(secrets or set(), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(agent\(\.\.\.\) request:\s*).*", r"\1[REDACTED]", redacted, flags=re.DOTALL)
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(
        r"(?i)\b([A-Za-z0-9_.-]*(?:TOKEN|KEY|SECRET|PASSWORD|AUTH|COOKIE)[A-Za-z0-9_.-]*)(\s*[:=]\s*)(\"?)[^\s,}\]\"']+",
        r"\1\2\3[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[REDACTED]", redacted)
    return redacted


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def main(argv: Sequence[str] | None = None) -> int:
    started = time.monotonic()
    args = parse_args(argv)
    base_agent_argv = [str(args.agent_command), *[str(part) for part in args.agent_arg]]
    agent_argv = list(base_agent_argv)
    request: dict[str, Any] | None = None
    try:
        stdin_text = sys.stdin.read()
        request = load_runner_request(stdin_text)
        agent_argv = [*base_agent_argv, *expand_model_arg_templates(args.agent_model_arg, request)]
        prompt = build_provider_prompt(request)
        provider_stdin = prompt
        if args.agent_prompt_arg:
            agent_argv = [*agent_argv, str(args.agent_prompt_arg), prompt]
            provider_stdin = ""
        provider_result = run_agent_command(
            agent_argv,
            provider_stdin,
            args.timeout_seconds,
            args.max_agent_stdout_bytes,
            args.max_agent_stderr_bytes,
            cwd=_request_workspace_dir(request),
        )
        if provider_result.timed_out:
            raise AdapterError("provider_timeout", f"provider timed out after {args.timeout_seconds:g}s", provider_result=provider_result)
        if provider_result.stdout_exceeded:
            raise AdapterError(
                "provider_stdout_exceeded",
                f"provider stdout exceeded {args.max_agent_stdout_bytes} bytes",
                provider_result=provider_result,
            )
        if provider_result.stderr_exceeded:
            raise AdapterError(
                "provider_stderr_exceeded",
                f"provider stderr exceeded {args.max_agent_stderr_bytes} bytes",
                provider_result=provider_result,
            )
        if provider_result.exit_code is None:
            raise AdapterError("provider_start_failed", "provider command could not start", provider_result=provider_result)
        if provider_result.exit_code != 0:
            raise AdapterError("provider_nonzero_exit", f"provider exited with code {provider_result.exit_code}", provider_result=provider_result)
        try:
            stdout_text = provider_result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterError("provider_invalid_utf8", "provider stdout was not valid UTF-8", provider_result=provider_result) from exc
        response = parse_provider_response(stdout_text, request, provider_result, args)
        json.dump(response, sys.stdout, sort_keys=True, separators=(",", ":"))
        return 0
    except AdapterError as exc:
        provider_result = exc.provider_result
        diagnostic = redacted_error(
            exc.code,
            exc.message,
            argv=agent_argv,
            duration_ms=_duration_ms(started),
            request=request,
            provider_result=provider_result,
        )
        json.dump(diagnostic, sys.stderr, sort_keys=True, separators=(",", ":"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

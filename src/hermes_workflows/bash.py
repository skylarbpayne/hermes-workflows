from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .authoring import current_context
from .decorators import _STEP_REGISTRY

DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
_REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class BashResult:
    command: str
    cwd: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: float
    finished_at: float
    duration_seconds: float
    timed_out: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @classmethod
    def from_value(cls, value: Any) -> "BashResult":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                command=str(value.get("command") or ""),
                cwd=str(value["cwd"]) if value.get("cwd") is not None else None,
                exit_code=int(value["exit_code"]) if value.get("exit_code") is not None else None,
                stdout=str(value.get("stdout") or ""),
                stderr=str(value.get("stderr") or ""),
                started_at=float(value.get("started_at") or 0.0),
                finished_at=float(value.get("finished_at") or 0.0),
                duration_seconds=float(value.get("duration_seconds") or 0.0),
                timed_out=bool(value.get("timed_out")),
                stdout_truncated=bool(value.get("stdout_truncated")),
                stderr_truncated=bool(value.get("stderr_truncated")),
            )
        raise TypeError(f"cannot coerce {type(value).__name__} to BashResult")


class BashStepError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any]):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class BashCall:
    command: str
    name: str | None = None
    key: str | None = None
    cwd: str | os.PathLike[str] | None = None
    timeout_seconds: float | None = 300
    env: Mapping[str, str] | None = None
    max_stdout_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    redact_values: Sequence[str] | None = None
    redact_patterns: Sequence[str] | None = None
    shell: str = "/bin/bash"

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command:
            raise TypeError("bash(...) requires a non-empty command string")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive or None")
        if self.max_stdout_bytes < 0 or self.max_stderr_bytes < 0:
            raise ValueError("max_stdout_bytes and max_stderr_bytes must be non-negative")
        if not isinstance(self.shell, str) or not self.shell:
            raise TypeError("shell must be a non-empty string")

    def __await__(self):
        return self._run(block=True).__await__()

    async def _run(self, *, block: bool) -> BashResult | Any:
        ctx = current_context()
        return await self._run_with_context(ctx, block=block)

    async def _run_with_context(self, ctx: Any, *, block: bool = True) -> BashResult | Any:
        key = self.step_key(ctx)
        payload = self._payload(key)
        result = await ctx.run_step(
            "bash",
            tuple(payload["args"]),
            dict(payload["kwargs"]),
            key=key,
            block=block,
            payload_builder=lambda: payload,
        )
        if hasattr(result, "key") and type(result).__name__ == "PendingStep":
            return result
        return BashResult.from_value(result)

    def step_key(self, ctx: Any) -> str:
        if self.key is not None:
            return _safe_key(self.key)
        if self.name is not None:
            return f"bash:{_safe_key(self.name)}"
        counts = getattr(ctx, "_authoring_bash_call_counts", None)
        if counts is None:
            counts = {}
            setattr(ctx, "_authoring_bash_call_counts", counts)
        base = "bash"
        index = int(counts.get(base, 0))
        counts[base] = index + 1
        return f"step:bash:{index}"

    def _request(self, key: str) -> dict[str, Any]:
        env = {str(k): str(v) for k, v in dict(self.env or {}).items()}
        request: dict[str, Any] = {
            "kind": "bash.request.v1",
            "key": key,
            "name": self.name,
            "command": self.command,
            "cwd": str(self.cwd) if self.cwd is not None else None,
            "timeout_seconds": self.timeout_seconds,
            "env": env,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "redact_values": [str(value) for value in (self.redact_values or [])],
            "redact_patterns": [str(pattern) for pattern in (self.redact_patterns or [])],
            "shell": self.shell,
        }
        request["fingerprint"] = _sha256_json(
            {
                "kind": request["kind"],
                "command": request["command"],
                "cwd": request["cwd"],
                "timeout_seconds": request["timeout_seconds"],
                "env": request["env"],
                "max_stdout_bytes": request["max_stdout_bytes"],
                "max_stderr_bytes": request["max_stderr_bytes"],
                "redact_patterns": request["redact_patterns"],
                "shell": request["shell"],
            }
        )
        return request

    def _payload(self, key: str) -> dict[str, Any]:
        return {"step_name": "bash", "args": [self._request(key)], "kwargs": {}}


async def _bash_body(ctx: Any, request: Mapping[str, Any]) -> BashResult:
    return _run_bash_request(request)


_STEP_REGISTRY["bash"] = _bash_body


def bash(
    command: str,
    *,
    name: str | None = None,
    key: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
    timeout_seconds: float | None = 300,
    env: Mapping[str, str] | None = None,
    max_stdout_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_stderr_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    redact_values: Sequence[str] | None = None,
    redact_patterns: Sequence[str] | None = None,
    shell: str = "/bin/bash",
) -> BashCall:
    """Run a shell command as a first-class durable workflow step."""

    return BashCall(
        command,
        name=name,
        key=key,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        env=env,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        redact_values=redact_values,
        redact_patterns=redact_patterns,
        shell=shell,
    )


def _run_bash_request(request: Mapping[str, Any]) -> BashResult:
    command = str(request.get("command") or "")
    if not command:
        raise ValueError("bash step request must include a non-empty command")
    cwd = str(request["cwd"]) if request.get("cwd") is not None else None
    timeout_seconds = request.get("timeout_seconds", 300)
    timeout = None if timeout_seconds is None else float(timeout_seconds)
    max_stdout_bytes = int(request.get("max_stdout_bytes", DEFAULT_MAX_OUTPUT_BYTES))
    max_stderr_bytes = int(request.get("max_stderr_bytes", DEFAULT_MAX_OUTPUT_BYTES))
    shell = str(request.get("shell") or "/bin/bash")
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in dict(request.get("env") or {}).items()})
    redactor = _Redactor(
        values=[str(value) for value in request.get("redact_values") or []],
        patterns=[str(pattern) for pattern in request.get("redact_patterns") or []],
    )

    started_at = time.time()
    started_monotonic = time.monotonic()
    timed_out = False
    exit_code: int | None
    stdout_bytes: bytes | None
    stderr_bytes: bytes | None
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            executable=shell,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        exit_code = int(completed.returncode)
        stdout_bytes = completed.stdout
        stderr_bytes = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout_bytes = _as_bytes(exc.output)
        stderr_bytes = _as_bytes(exc.stderr)

    finished_at = time.time()
    duration_seconds = time.monotonic() - started_monotonic
    stdout, stdout_truncated = _decode_capture(stdout_bytes or b"", max_stdout_bytes)
    stderr, stderr_truncated = _decode_capture(stderr_bytes or b"", max_stderr_bytes)
    result = BashResult(
        command=redactor.apply(command),
        cwd=cwd,
        exit_code=exit_code,
        stdout=redactor.apply(stdout),
        stderr=redactor.apply(stderr),
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
    if timed_out:
        raise BashStepError(
            f"bash command timed out after {timeout:g} seconds" if timeout is not None else "bash command timed out",
            details=_result_details(result),
        )
    if exit_code != 0:
        raise BashStepError(f"bash command exited with status {exit_code}", details=_result_details(result))
    return result


def _result_details(result: BashResult) -> dict[str, Any]:
    return {
        "command": result.command,
        "cwd": result.cwd,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }


class _Redactor:
    def __init__(self, *, values: Sequence[str], patterns: Sequence[str]):
        self.values = [value for value in values if value]
        self.patterns = [re.compile(pattern) for pattern in patterns]

    def apply(self, text: str) -> str:
        redacted = text
        for value in self.values:
            redacted = redacted.replace(value, _REDACTED)
        for pattern in self.patterns:
            redacted = pattern.sub(_REDACTED, redacted)
        return redacted


def _decode_capture(data: bytes, max_bytes: int) -> tuple[str, bool]:
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), truncated


def _as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", errors="replace")


def _safe_key(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("bash step key source must be non-empty")
    safe = "".join(char if char.isalnum() or char in "._-:" else "_" for char in text)
    safe = "_".join(part for part in safe.split("_") if part)
    if safe == text and len(safe) <= 80:
        return safe
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:64].strip('._-:') or 'bash'}-{digest}"


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = ["BashCall", "BashResult", "BashStepError", "bash"]

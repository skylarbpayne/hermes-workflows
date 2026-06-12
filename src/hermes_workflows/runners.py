from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


class AgentRunnerError(RuntimeError):
    """Raised when an external agent runner fails closed."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class SubprocessAgentRunner:
    """JSON-over-stdin adapter for trusted external agent(...) runner commands."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 300,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        max_stdout_bytes: int = 1_000_000,
        redact_env_keys: tuple[str, ...] = ("TOKEN", "KEY", "SECRET", "PASSWORD"),
    ):
        if isinstance(command, (str, bytes)):
            raise TypeError("SubprocessAgentRunner command must be an argv sequence, not a shell string")
        self.command = [str(part) for part in command]
        if not self.command:
            raise ValueError("SubprocessAgentRunner command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_stdout_bytes <= 0:
            raise ValueError("max_stdout_bytes must be positive")
        self.timeout_seconds = timeout_seconds
        self.cwd = Path(cwd) if cwd is not None else None
        self.env = {str(key): str(value) for key, value in (env or {}).items()}
        self.max_stdout_bytes = max_stdout_bytes
        self.redact_env_keys = tuple(redact_env_keys)

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        request_bytes = self._encode_request(request)
        returncode, stdout, stderr = self._run_command(request_bytes, started)

        duration_ms = self._duration_ms(started)
        if returncode != 0:
            raise AgentRunnerError(
                f"agent runner exited with code {returncode}: {self._command_label()}",
                details={
                    "command": self.command,
                    "exit_code": returncode,
                    "duration_ms": duration_ms,
                    "stdout_tail": self._tail(stdout),
                    "stderr_tail": self._tail(stderr),
                },
            )

        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentRunnerError(
                f"agent runner returned invalid JSON on stdout: {self._command_label()}",
                details={
                    "command": self.command,
                    "duration_ms": duration_ms,
                    "stdout_tail": self._tail(stdout),
                    "stderr_tail": self._tail(stderr),
                },
            ) from exc

        if not isinstance(response, dict):
            raise AgentRunnerError(
                "agent runner response must be a JSON object",
                details={"command": self.command, "duration_ms": duration_ms, "response_type": type(response).__name__},
            )
        if "output" not in response:
            raise AgentRunnerError(
                "agent runner response must include an 'output' field",
                details={"command": self.command, "duration_ms": duration_ms, "response_keys": sorted(response)},
            )
        if "provenance" in response and response["provenance"] is not None and not isinstance(response["provenance"], dict):
            raise AgentRunnerError(
                "agent runner provenance must be a JSON object when provided",
                details={"command": self.command, "duration_ms": duration_ms},
            )
        if response.get("provenance") is None:
            response["provenance"] = {
                "runner": "subprocess",
                "command": Path(self.command[0]).name,
                "duration_ms": duration_ms,
            }
        return response

    def _run_command(self, request_bytes: bytes, started: float) -> tuple[int, bytes, bytes]:
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd is not None else None,
                env=self._subprocess_env(),
            )
        except OSError as exc:
            duration_ms = self._duration_ms(started)
            raise AgentRunnerError(
                f"agent runner could not start: {self._command_label()}: {exc}",
                details={"command": self.command, "duration_ms": duration_ms, "error": str(exc)},
            ) from exc

        stdout = bytearray()
        stderr = bytearray()
        deadline = started + self.timeout_seconds

        try:
            if process.stdin is not None:
                process.stdin.write(request_bytes)
                process.stdin.close()

            selector = selectors.DefaultSelector()
            try:
                if process.stdout is not None:
                    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                if process.stderr is not None:
                    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    events = selector.select(timeout=min(0.1, remaining))
                    if not events:
                        continue
                    for key, _ in events:
                        if key.data == "stdout":
                            read_size = min(65536, self.max_stdout_bytes + 1 - len(stdout))
                            chunk = os.read(key.fd, read_size)
                        else:
                            chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == "stdout":
                            stdout.extend(chunk)
                            if len(stdout) > self.max_stdout_bytes:
                                raise ValueError("stdout_exceeded")
                        else:
                            stderr.extend(chunk)
                            del stderr[:-4096]
            finally:
                selector.close()

            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            return returncode, bytes(stdout), bytes(stderr)
        except TimeoutError as exc:
            process.kill()
            process.wait()
            duration_ms = self._duration_ms(started)
            raise AgentRunnerError(
                f"agent runner timed out after {self.timeout_seconds:g}s: {self._command_label()}",
                details={
                    "command": self.command,
                    "timeout_seconds": self.timeout_seconds,
                    "duration_ms": duration_ms,
                    "stdout_tail": self._tail(bytes(stdout)),
                    "stderr_tail": self._tail(bytes(stderr)),
                },
            ) from exc
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            duration_ms = self._duration_ms(started)
            raise AgentRunnerError(
                f"agent runner timed out after {self.timeout_seconds:g}s: {self._command_label()}",
                details={
                    "command": self.command,
                    "timeout_seconds": self.timeout_seconds,
                    "duration_ms": duration_ms,
                    "stdout_tail": self._tail(bytes(stdout)),
                    "stderr_tail": self._tail(bytes(stderr)),
                },
            ) from exc
        except ValueError as exc:
            if str(exc) != "stdout_exceeded":
                raise
            process.kill()
            process.wait()
            duration_ms = self._duration_ms(started)
            raise AgentRunnerError(
                f"agent runner stdout exceeded {self.max_stdout_bytes} bytes: {self._command_label()}",
                details={
                    "command": self.command,
                    "duration_ms": duration_ms,
                    "stdout_bytes": len(stdout),
                    "stdout_tail": self._tail(bytes(stdout)),
                    "stderr_tail": self._tail(bytes(stderr)),
                },
            ) from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def _encode_request(self, request: dict[str, Any]) -> bytes:
        try:
            return json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AgentRunnerError("agent runner request is not JSON serializable", details={"command": self.command}) from exc

    def _subprocess_env(self) -> dict[str, str] | None:
        if not self.env:
            return None
        merged = dict(os.environ)
        merged.update(self.env)
        return merged

    def _duration_ms(self, started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _command_label(self) -> str:
        return " ".join(self.command)

    def _tail(self, stream: bytes, *, limit: int = 4096) -> str:
        return stream[-limit:].decode("utf-8", errors="replace")

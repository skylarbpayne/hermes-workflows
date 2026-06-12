# Real Hermes/Codex agent(...) Subprocess Adapter Implementation Plan

> **For Hermes:** This is an approval artifact. Do not implement until the maintainer explicitly approves this plan. After approval, use the `subagent-driven-development` skill to implement task-by-task with review after each slice.

**Goal:** Add the first real CLI-backed agent(...) adapter so `WorkflowEngine(agent_runner=SubprocessAgentRunner(...))` can call a Hermes/Codex-style subprocess through the existing JSON stdin/stdout boundary instead of only static fixture scripts.

**Architecture:** Keep `SubprocessAgentRunner` provider-agnostic. Add a small JSON-contract adapter command that is run by `SubprocessAgentRunner`; the adapter reads `agent.runner_request.v1` from stdin, invokes a configured agent CLI using argv-only subprocess calls, parses a strict JSON answer, and writes `{output, provenance}` to stdout. Tests use fake CLI commands first; any real Hermes/Codex local smoke is opt-in and skipped by default.

**Tech Stack:** Python stdlib (`argparse`, `json`, `subprocess`, `hashlib`, `time`, `os`), existing `hermes_workflows.agent(...)`, `WorkflowEngine`, `SubprocessAgentRunner`, pytest.

---

## Approval scope

Approved by this plan, if the maintainer says yes:

1. Create a generic CLI adapter command for Hermes/Codex-style agents.
2. Add fake CLI tests proving request transformation, strict JSON response parsing, provenance, redaction, timeout/error behavior, and generated-workflow approval behavior.
3. Add one deterministic example workflow using the fake CLI path.
4. Add one optional real local smoke command that is disabled unless an explicit environment flag is set.
5. Update docs to show the adapter contract and safety boundaries.

Not approved by this plan:

- No credential creation, mutation, import, or provider login.
- No default use of the maintainer's real Codex/Hermes auth.
- No production workflow execution.
- No network calls in default tests.
- No autonomous generated-code execution; existing generated-workflow approval gates stay in front of imports/child workflows.
- No provider-specific config inside the workflow runtime core.

## Current codebase facts this plan relies on

- `src/hermes_workflows/runners.py` already provides `SubprocessAgentRunner(command, timeout_seconds, cwd, env, max_stdout_bytes)`.
- `src/hermes_workflows/prompts.py` builds `agent.runner_request.v1` with `name`, `prompt`, hashes, `rendered_prompt`, `input`, `returns`, `workflow_id`, and `step_key`.
- Live agent(...) metadata already persists the runner request, response, and provenance in `StepCompleted.metadata`.
- `agent(...)(..., returns=Workflow)` already marks live generated workflow output as `approval_required=True` and waits for `approval.decision` before import/execution.
- `examples/runners/static_json_agent.py` is only a deterministic fixture; it is not a real agent/provider adapter.

## Proposed public usage

Default fake/local example:

```python
from pathlib import Path
import sys

from hermes_workflows import agent(...), SubprocessAgentRunner, WorkflowEngine, workflow


@workflow
async def summarize_with_cli_agent(ctx, inputs):
    return await agent(...)(
        "summarize_item",
        prompt="Summarize this item as JSON: {{item}}",
        input={"item": inputs["item"]},
    )(ctx)


repo_root = Path(__file__).resolve().parent.parent
engine = WorkflowEngine(
    "/tmp/hermes-agent-cli-adapter.sqlite",
    agent_runner=SubprocessAgentRunner(
        [
            sys.executable,
            "-m",
            "hermes_workflows.agent_cli_adapter",
            "--agent-command",
            sys.executable,
            "--agent-arg",
            str(repo_root / "examples" / "runners" / "fake_json_cli_agent.py"),
        ],
        timeout_seconds=120,
        max_stdout_bytes=1_000_000,
    ),
)
```

The adapter command itself is a thin JSON bridge:

```bash
PYTHONPATH=src:. python -m hermes_workflows.agent_cli_adapter \
  --agent-command codex \
  --agent-arg exec \
  --agent-arg --json
```

That command is what `SubprocessAgentRunner` launches. `SubprocessAgentRunner` still owns the outer timeout, stdout cap, stderr tail capture, and fail-closed JSON contract.

## Adapter command/API shape

Create: `src/hermes_workflows/agent_cli_adapter.py`

Command-line options:

- `--agent-command <argv0>`: required executable, e.g. `codex`, `hermes`, or `python` in tests.
- `--agent-arg <arg>`: repeatable argv entries appended after `--agent-command`.
- `--response-mode json-object`: default and only v1 mode; the agent CLI must ultimately produce one JSON object.
- `--timeout-seconds <float>`: inner timeout for the provider CLI; must be lower than or equal to the outer `SubprocessAgentRunner(timeout_seconds=...)` in examples.
- `--max-agent-stdout-bytes <int>`: cap raw provider CLI stdout before parsing.
- `--max-agent-stderr-bytes <int>`: cap raw provider CLI stderr retained for diagnostics.
- `--provenance-runner-name <name>`: optional stable label; default `hermes_workflows.agent_cli_adapter`.

Request on stdin from `SubprocessAgentRunner`:

```json
{
  "kind": "agent.runner_request.v1",
  "name": "summarize_item",
  "prompt": "Summarize this item as JSON: {{item}}",
  "prompt_sha256": "...",
  "rendered_prompt": "Summarize this item as JSON: alpha",
  "rendered_prompt_sha256": "...",
  "input": {"item": "alpha"},
  "input_sha256": "...",
  "returns": "json",
  "workflow_id": "wf_summary",
  "step_key": "step:agent:0"
}
```

The adapter transforms it into the provider prompt packet:

```text
You are being called by hermes-workflows agent(...).
Return exactly one JSON object and no surrounding prose.

Required response schema:
{
  "output": <JSON-compatible value>,
  "provenance": {"model": "optional", "request_id": "optional", "notes": "optional non-secret text"}
}

If the requested return type is "workflow", output must be:
{
  "source": "Python source defining one @workflow",
  "symbol": "workflow_function_name"
}

agent(...) request:
<pretty-printed request JSON>
```

The adapter passes this packet to the configured agent CLI on stdin. It does not interpolate it into a shell string.

Adapter response on stdout back to `SubprocessAgentRunner`:

```json
{
  "output": {"summary": "alpha in one sentence"},
  "provenance": {
    "runner": "hermes_workflows.agent_cli_adapter",
    "adapter_version": 1,
    "agent_command": {"argv0": "codex", "argv": ["codex", "exec", "--json"]},
    "request_kind": "agent.runner_request.v1",
    "request_name": "summarize_item",
    "request_sha256": "sha256 of canonical request JSON",
    "rendered_prompt_sha256": "...",
    "provider_provenance": {"model": "optional non-secret value"},
    "duration_ms": 1234,
    "exit_code": 0
  }
}
```

For `returns=Workflow`, `output` must remain the existing generated-workflow shape:

```json
{
  "output": {
    "source": "from hermes_workflows import workflow\n\n@workflow\nasync def process_item(ctx, item):\n    return item\n",
    "symbol": "process_item"
  },
  "provenance": {
    "runner": "hermes_workflows.agent_cli_adapter",
    "adapter_version": 1,
    "provider_provenance": {"model": "optional"}
  }
}
```

## Response parsing rules

Implement strict parsing. Do not make a clever garbage-eating parser in v1.

1. Provider CLI exit code must be zero.
2. Provider CLI stdout must be UTF-8 JSON.
3. Parsed value must be a JSON object.
4. Parsed value must include `output`.
5. `provenance`, if present, must be an object.
6. For `returns == "workflow"`, `output` must be either existing supported workflow output shape (`{"source": ..., "symbol": ...}`) or plain Python source string. Prefer object shape in docs/examples.
7. The adapter may add/merge its own provenance, but must never include raw environment values or full raw prompt text in provenance.
8. If parsing fails, write a small JSON error object to stderr and exit non-zero. Let `SubprocessAgentRunner` turn that into `AgentRunnerError` with bounded tails.

## Environment/auth boundaries and redaction

- The adapter must not read or modify provider config files.
- The adapter must not synthesize auth tokens or import credentials.
- The caller may pass environment via `SubprocessAgentRunner(env={...})`; those values are inherited by the adapter/provider process, but never printed.
- Error details and provenance must not include raw `env`.
- Treat argv as potentially secret-bearing. The adapter must expose only sanitized command metadata in provenance/errors: `argv0` basename plus a redacted argv list where values following known secret flags (`--api-key`, `--token`, `--password`, `--secret`, `--auth`, `--cookie`, `-k`) and inline `KEY=VALUE`/`TOKEN=VALUE`/`SECRET=VALUE` arguments are replaced with `[REDACTED]`.
- The adapter should fail fast for obviously secret-bearing argv when redaction cannot preserve useful diagnostics. Provider credentials should be supplied through the caller's environment or the provider's own local auth store, not command-line flags.
- Redact keys containing `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `AUTH`, or `COOKIE` if any future diagnostic path includes key names.
- Include only non-secret provenance: runner name, adapter version, sanitized command metadata, request hash, prompt hash, duration, exit code, provider model/request id if the provider returned them.
- Do not include raw provider stdout/stderr in provenance. Error diagnostics may include only redacted bounded tails, and redaction must run before writing stderr so provider output cannot leak tokens, cookies, or echoed credential-bearing prompts.
- Default tests use fake local Python CLIs, not real provider commands.
- Real smoke requires an explicit flag like `HERMES_WORKFLOWS_REAL_AGENT_ADAPTER=1` and a caller-supplied command env var; otherwise it skips.

## Timeout/stdout/stderr behavior

Outer boundary remains `SubprocessAgentRunner`:

- It enforces `timeout_seconds` for the adapter process.
- It enforces `max_stdout_bytes` on the adapter's stdout.
- It captures bounded stderr tail for diagnostics.
- It validates final `{output, provenance}` JSON.

Inner adapter boundary for provider CLI:

- Do **not** use `subprocess.run(..., capture_output=True)` for the provider process. That buffers unbounded output before the adapter can enforce the cap.
- Use `subprocess.Popen` plus a bounded-reader helper that enforces stdout/stderr byte caps while reading. Kill the provider and fail closed as soon as stdout exceeds `--max-agent-stdout-bytes` or stderr exceeds the retained-tail policy.
- Retain at most the configured stdout bytes needed for JSON parsing and at most the configured stderr tail bytes for diagnostics. The implementation must not hold arbitrarily large provider output in memory.
- On timeout/non-zero/oversized/invalid JSON, exit non-zero and write a redacted diagnostic JSON to stderr:

```json
{
  "kind": "agent_cli_adapter.error.v1",
  "error": "provider_invalid_json",
  "agent_command": {"argv0": "codex", "argv": ["codex", "exec", "--json"]},
  "duration_ms": 1234,
  "stdout_tail": "redacted bounded non-secret tail",
  "stderr_tail": "redacted bounded non-secret tail"
}
```

## Files to create/modify

Create:

- `src/hermes_workflows/agent_cli_adapter.py`
- `tests/test_agent_cli_adapter.py`
- `examples/runners/fake_json_cli_agent.py`
- `examples/agent_cli_adapter_runner.py`

Modify:

- `README.md` — add a short “CLI-backed agent(...) adapter” section after the existing subprocess runner section.
- `docs/architecture/dynamic-sub-workflows.md` — replace the current limitation “no built-in vendor/LLM adapter yet” with the new state: generic CLI adapter exists, real provider smoke is opt-in, no default credentials/config mutation.
- `src/hermes_workflows/__init__.py` only if the adapter exposes a reusable Python helper. Do not export it if the implementation is purely `python -m hermes_workflows.agent_cli_adapter`.

## Task breakdown

### Task 1: Add fake provider CLI fixture

**Objective:** Create a deterministic agent-like command for tests and examples.

**Files:**
- Create: `examples/runners/fake_json_cli_agent.py`
- Test later from: `tests/test_agent_cli_adapter.py`

**Implementation shape:**

```python
from __future__ import annotations

import json
import sys


def main() -> int:
    prompt = sys.stdin.read()
    if "FAIL_INVALID_JSON" in prompt:
        sys.stdout.write("not json")
        return 0
    if "WORKFLOW_OUTPUT" in prompt:
        output = {
            "source": "from hermes_workflows import workflow\n\n@workflow\nasync def process_item(ctx, item):\n    return {'processed': item}\n",
            "symbol": "process_item",
        }
    else:
        output = {"kind": "fake.agent_response.v1", "prompt_seen": "agent(...) request:" in prompt}
    json.dump({"output": output, "provenance": {"runner": "fake_json_cli_agent", "model": "fake-1"}}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Verification:**

Run:

```bash
python examples/runners/fake_json_cli_agent.py <<<'hello'
```

Expected: JSON object containing `output` and fake provenance.

### Task 2: Write failing adapter tests first

**Objective:** Lock the adapter contract before implementation.

**Files:**
- Create: `tests/test_agent_cli_adapter.py`

**Tests to write:**

1. `test_agent_cli_adapter_turns_agentstep_request_into_provider_prompt_and_records_provenance`
   - Build a fake request with `kind=agent.runner_request.v1`.
   - Call the adapter module through `SubprocessAgentRunner([sys.executable, "-m", "hermes_workflows.agent_cli_adapter", ...])`.
   - Assert output comes from fake CLI.
   - Assert provenance includes adapter runner, adapter version, sanitized agent command metadata, request name, request hash, prompt hash.
   - Assert provenance does not include `env`, raw prompt text, or secret values.

2. `test_agent_cli_adapter_fails_closed_on_provider_invalid_json`
   - Fake provider emits `not json`.
   - Assert `AgentRunnerError` from the outer runner.
   - Assert stderr tail mentions `provider_invalid_json` but not env secrets.

3. `test_agent_cli_adapter_fails_closed_on_provider_nonzero_exit`
   - Fake provider exits 7 with stderr.
   - Assert non-zero is converted to outer `AgentRunnerError`.

4. `test_agent_cli_adapter_provider_timeout_is_redacted`
   - Fake provider sleeps.
   - Assert timeout error path is non-zero and no token leaks.

5. `test_agent_cli_adapter_redacts_secret_bearing_argv_and_provider_output`
   - Configure fake provider argv with `--api-key sk-test-secret` and provider stderr/stdout containing token-looking strings.
   - Assert provenance/error diagnostics contain `[REDACTED]` and do not contain the raw secret.

6. `test_agent_cli_adapter_enforces_provider_stdout_cap_while_reading`
   - Fake provider writes much more than `--max-agent-stdout-bytes` without producing valid JSON.
   - Assert the adapter kills/fails closed at the cap and the retained diagnostic stays near the configured cap, proving it did not buffer the whole provider output first.

7. `test_agent_cli_adapter_generated_workflow_still_waits_for_approval`
   - Use `WorkflowEngine(..., agent_runner=SubprocessAgentRunner(adapter argv))`.
   - Run an `agent(...)(..., returns=Workflow)` pipeline.
   - Assert workflow status is `waiting` on generated-workflow approval.
   - Assert no `ChildWorkflowRequested` occurred before approval.
   - Assert approval artifact includes adapter provenance.

**Verification:**

Run:

```bash
PYTHONPATH=src:. pytest tests/test_agent_cli_adapter.py -q
```

Expected before implementation: tests fail because `hermes_workflows.agent_cli_adapter` does not exist.

### Task 3: Implement minimal adapter module

**Objective:** Make the fake-provider JSON path pass without adding provider-specific logic.

**Files:**
- Create: `src/hermes_workflows/agent_cli_adapter.py`

**Implementation functions:**

- `main(argv: list[str] | None = None) -> int`
- `parse_args(argv) -> argparse.Namespace`
- `load_runner_request(stdin_text: str) -> dict[str, Any]`
- `build_provider_prompt(request: dict[str, Any]) -> str`
- `run_agent_command(argv: list[str], prompt: str, timeout_seconds: float, max_stdout_bytes: int, max_stderr_bytes: int) -> ProviderResult`
- `parse_provider_response(stdout: str, request: dict[str, Any], provider_result: ProviderResult, args) -> dict[str, Any]`
- `redacted_error(...) -> dict[str, Any]`
- `sha256_json(value: Any) -> str`

**Important code constraints:**

- Use argv list, never `shell=True`.
- Use a streaming/bounded provider process reader; never use `subprocess.run(..., capture_output=True)` for the nested provider command.
- Sanitize/redact command metadata and provider output before putting it in provenance or diagnostics.
- Read one JSON request from stdin.
- Write one JSON object to stdout on success.
- Write only redacted diagnostics to stderr on failure.
- Return non-zero on adapter/provider failure.

**Verification:**

Run:

```bash
PYTHONPATH=src:. pytest tests/test_agent_cli_adapter.py -q
```

Expected: adapter tests pass.

### Task 4: Add example workflow using the adapter command

**Objective:** Provide a runnable product proof without real provider auth.

**Files:**
- Create: `examples/agent_cli_adapter_runner.py`

**Example shape:**

```python
from __future__ import annotations

import sys
from pathlib import Path

from hermes_workflows import agent(...), SubprocessAgentRunner, WorkflowEngine, workflow


@workflow
async def cli_agent_adapter_example(ctx, inputs):
    return await agent(...)(
        "summarize_item",
        prompt="Summarize {{item}} as JSON.",
        input={"item": inputs["item"]},
    )(ctx)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    db_path = Path("/tmp/hermes-agent-cli-adapter-example.sqlite")
    if db_path.exists():
        db_path.unlink()
    runner = SubprocessAgentRunner(
        [
            sys.executable,
            "-m",
            "hermes_workflows.agent_cli_adapter",
            "--agent-command",
            sys.executable,
            "--agent-arg",
            str(repo_root / "examples" / "runners" / "fake_json_cli_agent.py"),
        ]
    )
    engine = WorkflowEngine(db_path, agent_runner=runner)
    result = engine.run_until_idle(cli_agent_adapter_example, {"item": "alpha"}, workflow_id="wf_agent_cli_adapter_example")
    print(result)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

**Verification:**

Run:

```bash
PYTHONPATH=src:. python examples/agent_cli_adapter_runner.py
```

Expected: `RunResult(... status='completed' ...)`.

### Task 5: Add optional real local smoke, skipped by default

**Objective:** Prove a real local Hermes/Codex-style command can sit behind the adapter without baking credentials into tests.

**Files:**
- Modify: `tests/test_agent_cli_adapter.py`

**Test shape:**

```python
@pytest.mark.skipif(
    os.environ.get("HERMES_WORKFLOWS_REAL_AGENT_ADAPTER") != "1",
    reason="real agent adapter smoke is opt-in",
)
def test_real_agent_cli_adapter_smoke(tmp_path):
    command = shlex.split(os.environ["HERMES_WORKFLOWS_AGENT_COMMAND"])
    # Example env value: codex exec --json
    # The prompt asks for harmless JSON only; no file writes, no network assumptions beyond provider CLI behavior.
```

Acceptance for the real smoke:

- The command must be supplied by the developer/operator.
- The prompt asks for a trivial JSON response, e.g. `{"answer": 42}`.
- The workflow DB lives under `tmp_path`.
- No repo files are modified.
- If the provider emits invalid JSON, the smoke fails usefully; do not weaken parser rules to pass a chatty CLI.

Manual smoke command after approval/implementation:

```bash
HERMES_WORKFLOWS_REAL_AGENT_ADAPTER=1 \
HERMES_WORKFLOWS_AGENT_COMMAND='codex exec --json' \
PYTHONPATH=src:. pytest tests/test_agent_cli_adapter.py::test_real_agent_cli_adapter_smoke -q
```

Do not run this unless the maintainer explicitly approves using the local provider command/auth.

### Task 6: Update docs

**Objective:** Make the new boundary understandable without chat context.

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture/dynamic-sub-workflows.md`

Doc points to include:

- `SubprocessAgentRunner` remains the safe process boundary.
- `hermes_workflows.agent_cli_adapter` is an optional command behind that boundary.
- Fake CLI tests are default; real provider smoke is opt-in.
- The provider CLI must produce strict JSON; chatty prose fails closed.
- Provider credentials are not managed by hermes-workflows.
- Generated workflow output still hits the existing approval gate before import/execution.

**Verification:**

Run:

```bash
PYTHONPATH=src:. python -m compileall -q src tests examples
PYTHONPATH=src:. pytest tests/test_agent_cli_adapter.py tests/test_subprocess_agent_runner.py -q
```

Expected: all selected tests pass.

### Task 7: Full verification and evidence packet

**Objective:** Prove the approved slice works and preserve review evidence.

Run:

```bash
PYTHONPATH=src:. pytest -q
PYTHONPATH=src:. python examples/agent_cli_adapter_runner.py
PYTHONPATH=src:. python -m compileall -q src tests examples
```

Expected:

- Full suite remains green.
- Example exits 0 with completed RunResult.
- Compileall exits 0.

Write a Kanban comment with:

- changed files
- tests run and exact pass/fail counts
- example command output summary
- whether optional real local smoke was skipped or explicitly run
- any deviations from this plan

Then block for review/merge approval instead of auto-merging.

## Acceptance criteria

This slice is done only when all default acceptance criteria are true:

1. `SubprocessAgentRunner` can launch `python -m hermes_workflows.agent_cli_adapter ...` as its command.
2. The adapter reads `agent.runner_request.v1` from stdin and writes exactly one `{output, provenance}` JSON object to stdout on success.
3. The adapter invokes the provider/fake CLI using argv-only subprocess calls, never shell strings.
4. Fake CLI tests cover success, invalid JSON, non-zero exit, timeout/redaction, and generated-workflow approval wait.
5. Live generated workflow output from the adapter still waits for `approval.decision` before import or child execution.
6. Default tests do not require network, credentials, Codex, Hermes CLI auth, or provider config changes.
7. Optional real smoke is gated by `HERMES_WORKFLOWS_REAL_AGENT_ADAPTER=1` and a caller-supplied command.
8. Provenance is useful and non-secret: request hash, rendered prompt hash, adapter runner/version, command argv, duration, provider model/request id if returned.
9. No error path prints raw environment values or tokens.
10. README/docs explain the boundary and non-goals.
11. The implementation ends in a review-required Kanban block; no merge or production workflow run happens without separate approval.

## Anti-patterns / loopholes

- Do not teach the adapter to scrape arbitrary chatty prose unless the maintainer approves a separate parser design. v1 should be strict JSON or fail closed.
- Do not move provider-specific model/auth config into `WorkflowEngine` or `SubprocessAgentRunner`.
- Do not make default tests depend on a local Codex/Hermes install.
- Do not weaken generated-workflow approval gates for a smoother demo.
- Do not record raw prompts, provider transcripts, tokens, cookies, or env values in provenance.
- Do not implement retries yet; retry policy around external agents is a separate design because it affects cost and side effects.
- Do not hide provider stderr entirely; keep bounded redacted tails so failures are debuggable.
- Do not claim real-provider support unless the opt-in smoke was actually run and recorded.

## Recommended implementation order

1. Fake CLI fixture.
2. Failing adapter tests.
3. Minimal adapter implementation.
4. Example workflow.
5. Optional real smoke test, skipped by default.
6. Docs.
7. Full verification and Kanban review block.

This is the right next slice because it proves the product path without increasing blast radius: real command boundary first, fake test harness by default, strict JSON, durable provenance, and existing human gates for generated code.

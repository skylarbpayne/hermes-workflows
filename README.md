# hermes-workflows v0

Code-first durable workflow control-plane spike for Hermes.

This is intentionally small. It proves the core idea before we build Kanban, artifact UI, agent workers, or a Hermes plugin:

- `@workflow` plain async function authoring
- `@step` durable awaits
- SQLite append-only-ish event log
- step memoization across process restarts
- graceful exit when a step/signal is pending
- local step worker execution through `run_until_idle()` / `drain()`
- command claiming/leasing for external worker processes
- approval request primitive through `ctx.approval.request(...)`
- durable fan-out/fan-in through `ctx.gather(step_a(...), step_b(...))`
- render-only prompt-file steps through `AgentPrompt("prompt.md", **vars)`
- JSON-over-stdin subprocess agent runners through `SubprocessAgentRunner([...])`
- workflow-backed repository PR path through `examples.repo_pr_workflow`
- manual `signal()` resume API
- tiny cross-process CLI: `python -m hermes_workflows start|run|worker|signal|status|list|events|outbox`

## Architecture boundary

`hermes-workflows` should stay a boring durable runtime: event history, replay, worker leases, approval provenance, memoized steps, inspectability, and reviewable artifact/PR packets. Planning taste, TDD discipline, milestone review, artifact quality, and model-specific prompts belong in skills, Codex `/goal`, and subagent review loops.

See [`docs/architecture/runtime-vs-skills-subagents.md`](docs/architecture/runtime-vs-skills-subagents.md) for the accepted boundary.

Dynamic sub-workflow generation now uses Python as the workflow language: an `AgentStep` can return a typed `Workflow` value backed by generated Python source, and the parent can call or `ctx.map_workflow(...)` it as a durable child workflow. See [`docs/architecture/dynamic-sub-workflows.md`](docs/architecture/dynamic-sub-workflows.md) for the implemented first slice.

## The core runtime idea

A workflow function is a **decider**, not a daemon.

```text
external event arrives
  -> append event to SQLite history
  -> replay workflow function from the top
  -> completed step awaits resolve from history
  -> missing step/signal emits request/command
  -> workflow exits as waiting
```

So this looks like normal Python:

```python
@workflow
async def trip_planning(ctx, inputs):
    constraints = await collect_constraints(ctx, inputs)
    options = await draft_options(ctx, constraints)
    approval = await ctx.wait_for("approval.granted", key="approve_trip_plan")
    return {"options": options, "approved_by": approval["by"]}
```

But it does **not** keep a coroutine alive. A pending step or signal raises an internal `WorkflowWaiting` exception that the engine catches, persists, and exits cleanly.

## V0 example

```python
from hermes_workflows import WorkflowEngine, workflow, step

@step
async def collect_constraints(ctx, inputs):
    # v0 decider does not execute step bodies.
    # Workers/adapters complete step commands out-of-band.
    ...

@step
async def draft_options(ctx, constraints):
    ...

@workflow
async def trip_planning(ctx, inputs):
    constraints = await collect_constraints(ctx, inputs)
    options = await draft_options(ctx, constraints)
    approval = await ctx.wait_for("approval.granted", key="approve_trip_plan")
    return {"options": options, "approved_by": approval["by"]}

engine = WorkflowEngine("workflow.sqlite")

# Start and drain local step commands until approval is needed.
print(engine.run_until_idle(trip_planning, {"destination": "NYC"}, workflow_id="wf_trip"))

# Manual signal resumes the decider after a process restart and drains downstream steps.
engine = WorkflowEngine("workflow.sqlite")
print(engine.signal(
    "wf_trip",
    "approval.decision",
    key="approve_trip_plan",
    payload={"action": "approve", "by": "skylar"},
    source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://..."},
    idempotency_key="discord-message-1",
))
```

## Durable gather

`ctx.gather(...)` is the first fan-out/fan-in primitive. It only accepts `@step` calls in this spike:

```python
@workflow
async def research_brief(ctx, inputs):
    competitors, pricing = await ctx.gather(
        research_competitors(ctx, inputs),
        research_pricing(ctx, inputs),
    )
    return {"competitors": competitors, "pricing": pricing}
```

On the first decider pass it records `StepRequested` for every missing child and exits on `gather:0`. When workers complete the children, replay resolves the gathered results in argument order without re-running completed steps.

## Prompt-file steps

`AgentPrompt` keeps workflow control flow in Python while moving editable prompt text into markdown files:

```python
from hermes_workflows import AgentPrompt, workflow


@workflow
async def plan_workflow(ctx, inputs):
    rendered = await AgentPrompt(
        "examples/prompts/repo_change_plan.md",
        goal=inputs["goal"],
        repo_path=inputs["repo_path"],
        verification_commands=inputs["verification_commands"],
    )(ctx)

    approval = await ctx.approval.request(
        "Approve this implementation plan?",
        key="approve_implementation_plan",
        artifact=rendered,
        approver="human:skylar",
    )
    return {"prompt": rendered, "approval": approval}
```

Prompt files use a tiny `{{variable}}` syntax:

```markdown
# Implementation plan request

Goal: {{goal}}

Repository: {{repo_path}}

Verification commands:

{{verification_commands}}
```

V0 is deliberately render-only: `AgentPrompt` returns a JSON-serializable rendered prompt packet and does not call an LLM/agent runner yet. The prompt file can be a `str`, `pathlib.Path`, or any Python path-like object. List/dict variables render as pretty JSON, and missing variables fail closed.

The prompt content is snapshotted into the `StepRequested` event when the durable step is first requested. That means:

- an already-completed prompt step replays from workflow history without needing the prompt file to still exist
- a pending prompt step uses the original request-time prompt content even if the markdown file is edited before a worker drains it
- `AgentPrompt` works inside `ctx.gather(...)` like other durable step calls

`AgentPrompt` is not an approval bypass. Use normal `ctx.approval.request(...)` for human gates; prompt rendering is just another memoized step result.

## Subprocess AgentStep runner

`SubprocessAgentRunner` is the built-in bridge from durable `AgentStep` requests to a trusted external agent process. The runtime does not know about Hermes, Codex, Claude, or any vendor-specific model. It writes one JSON request to the configured command's stdin and expects one bounded JSON object on stdout.

```python
from hermes_workflows import AgentStep, SubprocessAgentRunner, WorkflowEngine, workflow


@workflow
async def summarize(ctx, inputs):
    return await AgentStep(
        "summarize_item",
        prompt="Summarize {{item}}",
        variables={"item": inputs["item"]},
    )(ctx)

engine = WorkflowEngine(
    "workflow.sqlite",
    agent_runner=SubprocessAgentRunner(
        ["python", "scripts/my_agent_runner.py"],
        timeout_seconds=120,
        max_stdout_bytes=1_000_000,
    ),
)
```

The subprocess receives this contract on stdin:

```json
{
  "kind": "agent_step.runner_request.v1",
  "name": "summarize_item",
  "prompt": "Summarize {{item}}",
  "rendered_prompt": "Summarize alpha",
  "variables": {"item": "alpha"},
  "returns": "json",
  "workflow_id": "wf_summary",
  "step_key": "step:agent_step:0"
}
```

It must return a JSON object with `output`; `provenance` is optional but strongly recommended and must not contain secrets:

```json
{
  "output": {"summary": "ALPHA"},
  "provenance": {"runner": "my-agent-runner", "model": "example-model", "request_id": "abc123"}
}
```

The runner fails closed on non-zero exit, timeout, invalid JSON, missing `output`, non-object provenance, and stdout larger than `max_stdout_bytes`. Error details include command, duration, exit code, and bounded stdout/stderr tails where useful, but never dump the subprocess environment. Command selection is trusted local code; do not pass unreviewed shell strings. Generated Python returned via `AgentStep(..., returns=Workflow)` still requires the generated-workflow approval gate before import or child execution, and approval is not a sandbox.

Try the deterministic local smoke path:

```bash
PYTHONPATH=src:. python examples/subprocess_agent_runner.py
```

### CLI-backed AgentStep adapter

`hermes_workflows.agent_cli_adapter` is an optional command you can put behind `SubprocessAgentRunner` when a Hermes/Codex-style local CLI already knows how to authenticate and can return strict JSON. `SubprocessAgentRunner` remains the safe outer process boundary; the adapter is a thin JSON bridge that:

- reads the `agent_step.runner_request.v1` JSON object from stdin
- builds the provider prompt packet
- invokes the configured provider CLI with an argv list (`shell=True` is never used)
- requires the provider to write exactly one JSON object with `output` and optional non-secret `provenance`
- adds redacted adapter provenance, request hashes, command metadata, duration, and exit code
- fails closed on non-zero exit, timeout, invalid/chatty JSON, oversized output, or invalid provenance

Deterministic fake-provider example:

```bash
PYTHONPATH=src:. python examples/agent_cli_adapter_runner.py
```

Adapter command shape:

```bash
PYTHONPATH=src:. python -m hermes_workflows.agent_cli_adapter \
  --agent-command codex \
  --agent-arg exec \
  --agent-arg --json
```

Provider credentials are not created, imported, or mutated by `hermes-workflows`; use the provider CLI's own local auth store or environment. Default tests use `examples/runners/fake_json_cli_agent.py` and require no network, credentials, Hermes, Codex, or provider auth. A real provider smoke test exists only behind `HERMES_WORKFLOWS_REAL_AGENT_ADAPTER=1` plus a caller-supplied `HERMES_WORKFLOWS_AGENT_COMMAND`; do not treat real-provider support as verified unless that opt-in smoke was explicitly run. Generated Python returned through `AgentStep(..., returns=Workflow)` still waits at the existing generated-workflow approval gate before import or child execution.

## Workflow-backed PR path

`examples.repo_pr_workflow` is the first repo PR operating path. It is intentionally explicit instead of magical, and it now hard-requires pre-implementation plan approval. The plan workflow dogfoods `AgentPrompt`: the editable implementation-plan template lives at `examples/prompts/repo_change_plan.md`, while workflow control flow and approval semantics stay in Python.

The expected sequence is:

1. run `repo_change_plan_workflow` to render an `AgentPrompt`-backed implementation plan artifact from `examples/prompts/repo_change_plan.md`
2. pause on `approve_implementation_plan` and record human approval provenance
3. pass that approved `implementation_plan` result into `repo_pr_workflow`
4. gather git evidence from the branch against `origin/main`
5. run verification commands such as `pytest -q`
6. write a PR body with commits, changed files, diff stat, tests, implementation-plan provenance, and approval/merge placeholders
7. optionally push/open the GitHub PR with `gh pr create` or refresh an existing branch PR body/title
8. optionally watch GitHub checks with `gh pr checks --watch`, retrying briefly while GitHub attaches checks to a newly pushed branch
9. pause on `approve_pr_landing` so the human approval/merge provenance is recorded before status is finalized
10. write a reviewable status/landing packet under `.hermes/pr-workflows/` before approval, then overwrite it with final approval provenance after a human signal

`repo_pr_workflow` fails closed if `implementation_plan` is missing, not marked `ready_for_implementation`, missing the plan artifact/workflow id/SHA-256, or missing human Skylar approval provenance. It does not trust the caller-supplied dict by itself: the referenced workflow must exist in the same workflow DB as a completed `repo_change_plan_workflow`, the durable result must match the supplied plan fields, the workflow event log must contain the matching human approval signal, and the plan artifact must still exist, be non-empty, and match the approved SHA-256. New plan workflow results also carry prompt provenance (`plan_prompt_path`, prompt SHA-256, and rendered prompt SHA-256) so landing packets show which prompt file rendered the reviewed plan.

For a real self-dogfood run, start with the plan workflow:

```bash
PYTHONPATH=src:. python -m hermes_workflows run \
  examples.repo_pr_workflow:repo_change_plan_workflow \
  --db .hermes/pr-workflows/repo-pr.sqlite \
  --id wf_repo_pr_<slug>_plan \
  --input-json '{"repo_path":"/Users/skylarpayne/code/hermes-workflows","goal":"Add workflow-backed PR operating path","verification_commands":["pytest -q","python -m compileall -q src tests examples"]}'
```

After the plan artifact is reviewed, resume with a human-sourced approval signal. The completed durable result, including `plan_artifact_sha256` and AgentPrompt provenance (`plan_prompt_path`, `plan_prompt_sha256`, `plan_rendered_prompt_sha256`), becomes the `implementation_plan` input for the PR workflow; do not fabricate this object by hand or copy it from chat without checking workflow status:

```bash
PYTHONPATH=src:. python -m hermes_workflows run \
  examples.repo_pr_workflow:repo_pr_workflow \
  --db .hermes/pr-workflows/repo-pr.sqlite \
  --id wf_repo_pr_<slug> \
  --input-json '{"repo_path":"/Users/skylarpayne/code/hermes-workflows","goal":"Add workflow-backed PR operating path","implementation_plan":{"ready_for_implementation":true,"plan_workflow_id":"wf_repo_pr_<slug>_plan","plan_artifact_path":".hermes/pr-workflows/wf_repo_pr_<slug>_plan-implementation-plan.md","plan_artifact_sha256":"<sha256-from-completed-plan-workflow-status>","plan_prompt_path":"examples/prompts/repo_change_plan.md","plan_prompt_sha256":"<sha256-from-completed-plan-workflow-status>","plan_rendered_prompt_sha256":"<sha256-from-completed-plan-workflow-status>","approved_by":"skylar","approval_source":{"kind":"human","id":"skylar","channel":"discord","message_url":"discord://..."}},"verification_commands":["pytest -q","python -m compileall -q src tests examples"],"create_pr":true,"push_branch":true,"watch_checks":true,"gh_home":"/Users/skylarpayne"}'
```

The PR workflow should end waiting on `signal:approval.decision:approve_pr_landing`. That is the separate landing gate: review the PR/status packet, then send a human-sourced approval signal if merge/landing should proceed. Plan approval is not merge approval.

## Human approval provenance

`ctx.approval.request(..., approver="human:...")` now requires `approval.decision` signals to include human provenance. Agent-authored or missing-source approval signals fail closed instead of quietly advancing the workflow.

```python
engine.signal(
    "wf_trip",
    "approval.decision",
    key="approve_trip_plan",
    payload={"action": "approve", "by": "skylar"},
    source={"kind": "human", "id": "skylar", "channel": "discord", "message_url": "discord://..."},
)
```

The decision returned to workflow code includes the validated `source` so final reports can show who approved, where, and with what provenance.

## Minimal CLI

The CLI is intentionally boring and requires the workflow module path so a fresh process can import/register the decider and steps:

```bash
PYTHONPATH=src:. python -m hermes_workflows start \
  examples.first_real_trip_workflow:first_real_trip_workflow \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --input-json '{"destination":"NYC"}'

PYTHONPATH=src:. python -m hermes_workflows worker \
  examples.first_real_trip_workflow:first_real_trip_workflow \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --worker-id worker-1 \
  --once

PYTHONPATH=src:. python -m hermes_workflows signal \
  examples.first_real_trip_workflow:first_real_trip_workflow \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --type approval.decision \
  --key approve_trip_plan \
  --payload-json '{"action":"approve","by":"skylar"}' \
  --source-json '{"kind":"human","id":"skylar","channel":"discord","message_url":"discord://..."}' \
  --idempotency-key manual-approval-1

PYTHONPATH=src:. python -m hermes_workflows status \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip

# When a workflow failed or stuck and pending_commands is empty, include
# bounded command history directly in the status packet:
PYTHONPATH=src:. python -m hermes_workflows status \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --commands failed \
  --command-limit 10

PYTHONPATH=src:. python -m hermes_workflows events \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --limit 20

PYTHONPATH=src:. python -m hermes_workflows outbox \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --status pending

PYTHONPATH=src:. python -m hermes_workflows cancel \
  --db /tmp/hermes-workflows.sqlite \
  --id wf_first_real_trip \
  --reason 'superseded by wf_next_trip' \
  --source-json '{"kind":"human","id":"skylar","channel":"discord","message_url":"discord://..."}' \
  --superseded-by wf_next_trip

PYTHONPATH=src:. python -m hermes_workflows list \
  --db /tmp/hermes-workflows.sqlite \
  --status waiting
```

`status` and `outbox` include read-only diagnostics for pending/running command rows. This makes stale approval notifications visible without mutating the database:

- `active_wait` means the workflow is currently waiting on that command/signal.
- `matching_signal_exists` means a matching approval signal is already in the event log, so the notification row is historical/stale.
- `terminal_workflow_has_pending_command` means the workflow is completed/failed/cancelled while the command row still says pending/running.
- `orphaned_or_inconsistent` means the row does not match the workflow's current wait state.

These labels are advisory only. They do not delete commands, rewrite history, or resume workflows.

`status --commands failed|recent|all` is opt-in command history for debugging failures from one packet. It adds `command_history` with bounded `payload_context`, `last_error`, `attempts`, `claimed_by`, lease metadata, timestamps, and diagnostic labels when applicable; default `status` output stays compact and only includes active `pending_commands`.

`cancel` is the explicit mutation path for retiring stale or superseded workflows. It appends a `WorkflowCancelled` event, sets the instance to `status="cancelled"`, clears `waiting_on`, marks pending/running outbox rows `cancelled`, and exposes the audit payload as `terminal_reason` in `status`/`list`. It does not clean up real workflow DB rows unless you run it against that DB deliberately.

## Current limitations

V0 is a spike, not production runtime:

- external worker process exists, but it is intentionally small and workflow-specific
- command claiming/leasing exists, but no backoff policy yet
- `ctx.gather` fan-out enqueues child steps together, but the local v0 drain loop still executes runnable commands serially
- no full approval policy engine yet, only approval request + source-provenance validation
- no feedback/rerun invalidation yet
- no Kanban/artifact adapters yet
- no workflow versioning/determinism guard yet
- no async caller support; `WorkflowEngine` is sync for the spike

Those are next slices after proving the durability model.

## Run tests

```bash
pytest -q
```

Expected now:

```text
106 passed, 1 skipped
```

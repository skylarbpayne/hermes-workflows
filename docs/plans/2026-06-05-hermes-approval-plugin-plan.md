# Hermes Workflows Approval Plugin Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Give the maintainer a reliable way to approve `hermes-workflows` gates from a real operator surface now, while tracking cleanly toward a Hermes plugin and keeping the core runtime open for other agent runtimes.

**Architecture:** Keep `hermes-workflows` as the runtime-agnostic approval state machine. Extract a small approval adapter API from core, then let CLI, local dashboard, Hermes plugin, Discord/Telegram, Kanban, MCP, or other runtimes translate human actions into the same canonical `approval.decision` signal. Hermes-specific identity, delivery, dashboard tabs, gateway hooks, and message provenance belong in a plugin/adapter, not in core.

**Tech Stack:** Python, SQLite, `hermes-workflows` `WorkflowEngine`, Hermes Agent plugin system, Hermes gateway hooks, optional dashboard backend routes, pytest.

---

## Research summary

Subagents inspected the current branch, Hermes Agent plugin internals, gateway approval patterns, dashboard docs, Kanban docs, and prior approval UI patterns.

### Decision

Build in this order:

1. **Core approval adapter API** inside `hermes-workflows`.
2. **Local approval inbox** backed by that API, using the existing `serve-dashboard` capability as the usable surface.
3. **Hermes plugin thin adapter** that lists waiting approvals and resolves decisions through the core API.
4. **Notification/card surfaces** for Discord/Telegram/Kanban once the approval object and provenance contract are stable.

Do **not** make chat the canonical source of truth first. Chat should notify and eventually approve, but local dashboard + durable DB + Kanban-linked blocker is the shortest safe path that avoids approval theater.

### Core invariant

Every approval surface must resolve to this shape:

```python
WorkflowEngine.signal(
    workflow_id,
    "approval.decision",
    key=approval_key,
    payload={"action": "approve", "by": human_id},
    source={
        "kind": "human",
        "id": human_id,
        "channel": surface,
        "message_url": "...",  # or message_id / event_id
    },
    idempotency_key=stable_external_event_id,
)
```

If a surface cannot provide durable provenance, it may display approval state but must not approve.

---

## Current state

Already implemented on branch `feat/workflows-launch-hardening`:

- approval state-machine hardening
- `hermes-workflows approve|reject`
- static `dashboard`
- local mutating `serve-dashboard`
- packaged `hermes_workflows.examples.trip:trip_planning_workflow`
- in-repo skill
- adapter/plugin architecture note
- full suite verification: `122 passed, 2 skipped`

Gaps found by research:

1. Plugin/adapters currently have to scrape `workflow_status()` packets; no first-class `list_approvals()` API.
2. Approval summaries omit useful fields: `allowed`, `authority`, `timeout`, request event seq, notification status.
3. `WorkflowEngine.signal()` both records the decision and resumes/drains workflow work in the caller process. A Hermes chat callback should not accidentally execute arbitrary downstream workflow work.
4. Workflow instances do not persist an importable `workflow_ref`; CLI solves this by passing it on every `signal`, but a plugin needs a registry or persisted ref.
5. Generated-workflow approval still has a hard-coded `human:operator` path in core that should become policy/config.
6. Notification delivery lifecycle is under-modeled: delivered card IDs, retries, stale cards, fanout, and receipts need a home.
7. Hermes plugin hooks exist, but `pre_gateway_dispatch` runs before normal auth/pairing, so chat approval adapters must do their own exact pending-approval matching and auth checks.

---

## Anti-patterns / dumb zone

Do not do these:

- Do not build a bespoke Hermes-only approval model in core.
- Do not let a dashboard button bypass core approval validation.
- Do not make every approval into a new Kanban task.
- Do not let a chat callback execute arbitrary workflow steps inside the gateway process.
- Do not approve by fuzzy text like “yes looks good” unless it is bound to an exact approval token and payload hash.
- Do not treat agent messages as human approvals.
- Do not approve mutated payloads with stale approval decisions.
- Do not store credentials/secrets in approval payloads.
- Do not make the plugin a workflow engine.
- Do not market approvals in the blog until this path has an end-to-end smoke with real provenance.

---

## Milestone 1 — Core approval adapter API

**Acceptance criteria:** A plugin or any other runtime can list pending approvals and submit a decision without scraping dashboard HTML/status internals. The API remains Hermes-agnostic.

### Task 1.1: Add typed approval view models

**Objective:** Create runtime-agnostic dataclasses for approval cards and receipts.

**Files:**

- Create: `src/hermes_workflows/approvals.py`
- Test: `tests/test_approval_adapter_api.py`

**Implementation sketch:**

```python
@dataclass(frozen=True)
class ApprovalView:
    db_path: str
    workflow_id: str
    workflow_name: str
    key: str
    status: str
    prompt: str
    artifact: Any
    approver: str | None
    allowed: list[str]
    authority: dict[str, Any] | None
    waiting_on: str | None
    requested_seq: int | None
    source: dict[str, Any] | None
    decision: dict[str, Any] | None
    diagnostics: list[dict[str, Any]]

@dataclass(frozen=True)
class ApprovalDecisionInput:
    workflow_id: str
    key: str
    action: str
    by: str
    source: dict[str, Any]
    note: str | None = None
    idempotency_key: str | None = None

@dataclass(frozen=True)
class ApprovalReceipt:
    workflow_id: str
    key: str
    action: str
    by: str
    source: dict[str, Any]
    status: str
    waiting_on: str | None
    result_summary: dict[str, Any] | None
```

**Tests:**

- `test_list_pending_approvals_returns_allowed_authority_and_artifact`
- `test_approval_view_does_not_require_dashboard_renderer`

### Task 1.2: Add `WorkflowEngine.list_approvals(...)`

**Objective:** Give adapters one read-only call for approval inbox state.

**Files:**

- Modify: `src/hermes_workflows/engine.py`
- Test: `tests/test_approval_adapter_api.py`

**API:**

```python
engine.list_approvals(status="waiting") -> list[ApprovalView]
engine.get_approval(workflow_id, key) -> ApprovalView
```

**Verification:**

```bash
PYTHONPATH=src:. pytest -q tests/test_approval_adapter_api.py::test_list_pending_approvals_returns_allowed_authority_and_artifact
```

Expected: fails before implementation, passes after.

### Task 1.3: Add `WorkflowEngine.submit_approval_decision(...)`

**Objective:** Make adapters call a single explicit decision API rather than manually shaping `signal()`.

**Files:**

- Modify: `src/hermes_workflows/engine.py`
- Modify: `src/hermes_workflows/cli.py`
- Modify: `src/hermes_workflows/dashboard_server.py`
- Test: `tests/test_approval_adapter_api.py`

**API:**

```python
engine.submit_approval_decision(
    ApprovalDecisionInput(...),
    resume=True,
) -> ApprovalReceipt
```

`resume=True` preserves CLI/local-dashboard behavior. The next task adds a safe plugin mode.

**Tests:**

- `test_submit_approval_decision_validates_human_source`
- `test_submit_approval_decision_returns_receipt`
- `test_cli_approve_uses_submit_approval_decision`
- `test_dashboard_server_uses_submit_approval_decision`

### Task 1.4: Split record-vs-resume semantics

**Objective:** Prevent plugin/chat callbacks from executing downstream workflow work in the gateway process.

**Files:**

- Modify: `src/hermes_workflows/engine.py`
- Test: `tests/test_approval_adapter_api.py`

**API:**

```python
engine.submit_approval_decision(decision, resume=False)
engine.resume(workflow_fn, workflow_id)
```

`resume=False` should:

- validate approval
- append signal
- complete approval notification command if appropriate
- return receipt showing `status="waiting"` or `status="decision_recorded"`
- **not** replay/drain downstream steps

`resume=True` should match existing CLI behavior.

**Tests:**

- `test_submit_approval_decision_resume_false_records_without_running_next_step`
- `test_submit_approval_decision_resume_true_completes_workflow`

### Task 1.5: Persist or configure workflow refs

**Objective:** Let a plugin know how to resume a workflow instance.

**Files:**

- Modify: `src/hermes_workflows/engine.py`
- Modify: `src/hermes_workflows/cli.py`
- Test: `tests/test_workflow_ref_registry.py`

**Preferred minimal API:**

- Add optional `workflow_ref` metadata when starting/running from CLI.
- Store it with workflow instance metadata.
- Expose in `workflow_status()` and `ApprovalView`.

**Out of scope:** dynamic import safety/sandboxing beyond explicit configured refs.

---

## Milestone 2 — Better local Approval Inbox

**Acceptance criteria:** the maintainer can open a local approval surface, see pending workflow approvals, approve/reject exact gates, and get receipts. This remains useful even before the Hermes plugin.

### Task 2.1: Make `serve-dashboard` render bound approval cards

**Objective:** Stop asking humans to type workflow ID/key when the page already knows them.

**Files:**

- Modify: `src/hermes_workflows/dashboard_server.py`
- Modify: `src/hermes_workflows/dashboard.py`
- Test: `tests/test_cli.py`

**Behavior:**

Each pending approval row renders:

- prompt
- artifact summary
- allowed actions
- approver
- authority/scope
- approve/reject buttons bound to `workflow_id` + `key`
- optional note field

**Tests:**

- `test_serve_dashboard_renders_bound_approval_forms`
- `test_serve_dashboard_reject_button_records_reject_action`

### Task 2.2: Add payload hash to approval card

**Objective:** Make approvals visibly bind to an artifact snapshot.

**Files:**

- Modify: `src/hermes_workflows/approvals.py`
- Modify: `src/hermes_workflows/dashboard.py`
- Test: `tests/test_approval_adapter_api.py`

**Behavior:**

`ApprovalView` includes `artifact_sha256` computed from canonical JSON.

**Rule:** If artifact changes, the workflow must request a new approval key or new request event.

### Task 2.3: Add local inbox command

**Objective:** Provide a one-command approval inbox the maintainer can run.

**Files:**

- Modify: `src/hermes_workflows/cli.py`
- Test: `tests/test_cli.py`

**Current landing:** the Review Queue lives in the Hermes dashboard plugin and local `serve-dashboard` approval server rather than a separate inbox command.

---

## Milestone 3 — Hermes plugin MVP

**Acceptance criteria:** Hermes can list and resolve workflow approvals through plugin tools without adding Hermes imports to `hermes-workflows` core.

### Plugin research facts

Actual Hermes plugin extension points found:

- Plugin manager: `hermes_cli/plugins.py`
- Plugin directory shape:

```text
plugins/hermes-workflows-approvals/
  plugin.yaml
  __init__.py
```

- User plugin directory for operator agent profile:

```text
~/.hermes/profiles/<profile>/plugins/hermes-workflows-approvals/
```

- Required plugin function:

```python
def register(ctx): ...
```

- Tool registration:

```python
ctx.register_tool(..., toolset="hermes_workflows_approvals")
```

- Useful hook:

```python
ctx.register_hook("pre_gateway_dispatch", on_gateway_message)
```

- Plugin config shape:

```yaml
plugins:
  enabled:
    - hermes-workflows-approvals
  entries:
    hermes-workflows-approvals:
      workflow_dbs:
        - name: default
          path: /tmp/workflow.sqlite
```

### Task 3.1: Create plugin package skeleton outside core runtime

**Objective:** Build a thin Hermes adapter that imports `hermes_workflows` but is not imported by core.

**Recommended location:** Start in this repo as distributable plugin code, then install into Hermes profile or package as entry point.

**Files:**

- Create: `plugins/hermes-workflows-approvals/plugin.yaml`
- Create: `plugins/hermes-workflows-approvals/__init__.py`
- Create: `plugins/hermes-workflows-approvals/config.py`
- Create: `plugins/hermes-workflows-approvals/cards.py`
- Test: `tests/test_hermes_plugin_approvals.py`

**Manifest:**

```yaml
name: hermes-workflows-approvals
version: "0.1.0"
description: "Hermes Agent adapter for hermes-workflows human approvals"
kind: standalone
provides_tools:
  - workflow_review_requests_list
  - workflow_approval_decide
provides_hooks:
  - pre_gateway_dispatch
```

### Task 3.2: Register `workflow_review_requests_list`

**Objective:** Let Hermes inspect pending approvals from configured DBs.

**Tool input:**

```json
{
  "db": "optional configured db name/path",
  "status": "waiting"
}
```

**Tool output:** list of `ApprovalView` dicts, redacted and bounded.

**Test:** fake `PluginContext`, temp workflow DB, pending approval, call registered tool.

### Task 3.3: Register `workflow_approval_decide`

**Objective:** Let Hermes approve/reject with explicit provenance.

**Tool input:**

```json
{
  "db": "default",
  "workflow_id": "wf_...",
  "key": "approve_...",
  "action": "approve",
  "by": "operator",
  "channel": "discord",
  "message_id": "...",
  "message_url": "...",
  "resume": false
}
```

**Important default:** `resume=false` for plugin tools until a worker/resumer story exists. The plugin should record the decision and tell the operator surface what needs to resume, not execute arbitrary downstream workflow work inside a chat/tool call.

### Task 3.4: Add gateway hook for explicit approval replies only

**Objective:** Capture chat approvals when bound to an exact pending approval token.

**Behavior:**

- Only handle messages matching a plugin-issued opaque token/callback format.
- Validate platform/chat/user against pending approval record.
- Call `workflow_approval_decide` path.
- Return `{"action":"skip"}` only after successful decision.
- Otherwise return allow/no-op.

**Do not:** parse random “yes” messages as approvals.

### Task 3.5: Add plugin README and install notes

**Objective:** Make plugin usage obvious without requiring the maintainer to remember Hermes internals.

**Docs:**

- `docs/integrations/hermes-plugin.md`
- config examples
- local profile install path
- expected tool names
- failure modes

---

## Milestone 4 — Kanban integration

**Acceptance criteria:** Workflow approvals are visible in the execution truth without spawning noisy fake tasks.

### Task 4.1: Add task reference metadata to approvals

**Objective:** Connect workflow gate to the parent Kanban task when available.

**Files:**

- Modify: `src/hermes_workflows/approvals.py`
- Modify: workflow examples/docs
- Test: `tests/test_approval_adapter_api.py`

**Metadata:**

```json
{
  "task_ref": "kanban:t_3111e771",
  "project": "hermes-workflows",
  "reason": "approve generated workflow execution"
}
```

### Task 4.2: Plugin comments on parent task instead of creating new tasks

**Objective:** Keep Kanban as execution truth without approval-task spam.

**Behavior:**

- When approval is pending: comment or mark blocked on parent task.
- When approved/rejected: comment receipt.
- Create a separate task only when review/revision is actual work.

---

## Milestone 5 — Chat approval cards

**Acceptance criteria:** the maintainer can approve from Discord/Telegram where supported, but chat remains a convenience surface over the canonical approval record.

### Task 5.1: Telegram first if adapter supports buttons cleanly

**Why:** Hermes Agent already has Telegram dangerous-command approval button patterns.

**Behavior:**

- Send approval card with opaque token.
- Button callback carries token/action.
- Plugin captures user/chat/message provenance.
- Decision routed through `submit_approval_decision(resume=false)`.

### Task 5.2: Discord only after verifying adapter support

Research found no Discord platform adapter in inspected `hermes-agent-origin-main/gateway/platforms`. Do not assume button support. If Discord only supports plain messages in this deployment, use tokenized reply commands first:

```text
/workflow approve <token>
/workflow reject <token> reason...
```

No fuzzy yes/no.

---

## Milestone 6 — Worker/resume path

**Acceptance criteria:** Approval decisions recorded from plugin/chat wake or notify a safe runner instead of running workflow code in the callback process.

### Task 6.1: Add explicit resume worker command

```bash
hermes-workflows resume <workflow_ref> --db ... --id ...
```

### Task 6.2: Plugin posts “decision recorded” receipt and queues resume

Options:

- call a configured local command
- create a Kanban comment/blocker
- send webhook to a runner
- leave explicit manual resume for MVP

Do not hide this. If approval is recorded but execution has not resumed, receipt should say so.

---

## Verification plan

Run after each milestone:

```bash
PYTHONPATH=src:. pytest -q
python -m build
git diff --check
```

End-to-end approval smoke:

```bash
hermes-workflows run hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-approval-smoke.sqlite \
  --id wf_approval_smoke \
  --input-json '{"destination":"NYC","approver":"human:operator"}'

hermes-workflows serve-dashboard hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-approval-smoke.sqlite \
  --host 127.0.0.1 \
  --port 8765 \
  --enable-approval-actions
```

Expected receipt after approval:

```json
{
  "status": "completed",
  "approved_by": "operator",
  "source": {
    "kind": "human",
    "id": "operator",
    "channel": "local-dashboard",
    "message_id": "..."
  }
}
```

Plugin smoke later:

```text
workflow_review_requests_list -> shows pending approval
workflow_approval_decide(resume=false) -> records decision without running next step
hermes-workflows resume ... -> completes exact approved payload
Kanban task comment -> includes approval receipt
```

---

## Launch/blog gate

Do not resume the launch blog until at least Milestones 1–2 are merged and verified. The blog can mention the plugin path after Milestone 3 has a working smoke, but should not imply Discord/Telegram one-tap approvals are done until they are actually implemented and tested.

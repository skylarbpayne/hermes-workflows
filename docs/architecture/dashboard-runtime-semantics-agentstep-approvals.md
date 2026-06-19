# Dashboard runtime semantics, agent(...) naming, and approval artifacts

> **Design archive / superseded detail.** This 2026-06-07 implementation guide preserves historical dashboard/runtime decisions. Current launch docs distinguish trusted bundled local dashboard actions, which may use `resume=true`, from remote/gateway/plugin tools, which default to `resume=false`. Start with [Setup](../setup-for-agents.html) and [Hermes dashboard plugin](../integrations/hermes-plugin.html).

Status: accepted / implementation guide
Date: 2026-06-07
Scope: Hermes Workflows dashboard/API clarity without breaking existing workflow history or public imports.

## Decision summary

1. **Public docs and new examples should say `agent(...)`.** `agent(...)` is the durable boundary for asking a configured agent runner to produce JSON or a typed `Workflow` value. Use `render_prompt` for explicit prompt-file rendering; do not expose a separate durable prompt object API.
2. **The dashboard shows a read-only active workflow state source.** It is not a registry, branch, deployment, remote execution environment, or user-selectable database debugger. Browser APIs continue to reject raw SQLite paths and return configured aliases plus existence status only.
3. **Dashboard approval actions are record-only.** Approve/reject from the dashboard records server-derived human provenance with `resume=false`. Workflow execution resumes only when a trusted local resumer/operator continues the run.
4. **Artifacts get a typed render descriptor.** Approval/run artifact payloads remain persisted in workflow history, but dashboard responses include a redacted preview plus `artifact_render` so text/JSON/image/audio/video/file references can be handled consistently later.

## Where workflow code runs

Hermes Workflows is a Python runtime. Workflow code is imported and executed by the Python process that owns the `WorkflowEngine` instance for the active workflow state source:

- CLI `hermes-workflows run ...` executes workflow code in that CLI process.
- A local trusted resumer executes workflow code in its local process when it drains/resumes a run.
- The dashboard `POST /runs` route imports the configured `workflow_ref` and runs it in the Hermes dashboard server process because that route owns the engine for the active configured state-source alias.
- The dashboard `POST /approvals/decision` route **does not** continue workflow code. It records a decision only.

The workflow DB is durable state, not an execution sandbox. The dashboard presents the resolved configured state-source alias read-only; it does not let the browser pick arbitrary SQLite paths or move execution to another machine or deployment.

## agent(...) execution and failure modes

`await agent(...)` builds an `agent.request.v1` durable step request. On first execution:

1. The request is persisted as a normal durable step request.
2. If `WorkflowEngine(agent_runner=...)` is configured and `mock_output` is not supplied, the runner receives `agent.runner_request.v1` with the rendered prompt, input, workflow id, and step key.
3. The runner must return JSON-serializable output, optionally `{ "output": ..., "provenance": ... }`.
4. The live response and provenance are persisted in `StepCompleted.metadata` and replay uses history instead of calling the runner again.

Expected fail-closed behavior:

- Missing or misconfigured runner: the step fails; no downstream child workflow/import is attempted.
- Non-JSON-serializable runner response: the step fails before `StepCompleted` is recorded.
- Runner subprocess/adapter error: the step fails and the run status exposes the failure in events/status.
- `returns=Workflow` from a live runner: generated source is snapshotted as a `Workflow` value with `approval_required=True`; import/execution of the generated child workflow waits for a human approval decision.
- Approval rejection or missing approval: generated workflow execution remains blocked.

`render_prompt(...)` is narrower: it snapshots and renders a prompt file as a durable `agent_prompt.rendered.v1` packet. It does not call an agent runner. Keep it available for old render-only workflows, but avoid introducing it in new public examples unless the example is specifically about prompt-file rendering.

## Dashboard approval semantics

Dashboard approval cards and detail views should present:

- what is being approved,
- the artifact/evidence preview,
- risk/blast-radius copy,
- the consequence sentence, and
- decision semantics.

Current consequence: “Records approve/reject only; a trusted local resumer must continue the workflow.”

Server-side identity is required for dashboard decisions (`dashboard_approver_id` or `HERMES_WORKFLOWS_DASHBOARD_APPROVER_ID`). The browser must not provide `by`, `channel`, DB paths, or provenance fields.

## Multimodal approval artifact seam

Approval artifacts are arbitrary JSON-compatible workflow values today. For dashboard/API clarity, responses include:

```json
{
  "artifact_preview": {"path": "[REDACTED_LOCAL_PATH]", "caption": "Generated preview"},
  "artifact_render": {
    "kind": "image",
    "render": "file-reference",
    "media_type": "image/png",
    "persisted": "workflow_history",
    "servable_by_dashboard": false,
    "reference": {"type": "local_path", "field": "path", "href": "[REDACTED_LOCAL_PATH]"},
    "warning": "Local/private files are not served by the dashboard; attach or expose them through an explicit artifact store before rendering media inline."
  }
}
```

Supported descriptor kinds/render modes in this slice:

| Artifact form | Descriptor | Dashboard behavior |
| --- | --- | --- |
| plain string | `kind=text`, `render=inline-text` | Render/copy as text. |
| JSON object/list | `kind=json`, `render=inline-json` | Show redacted JSON preview. |
| Markdown object (`kind=markdown` or `media_type=text/markdown`) | `kind=markdown`, `render=inline-markdown` | Safe renderer can be added later. |
| External URL | `kind=link` or media kind, `render=external-link/media-reference` | Link/reference only; no hosting added. |
| Local image/audio/video/file path | media/file kind, `render=file-reference` | Redact path; dashboard does not serve it. |

Persistence remains the workflow history DB. This slice does **not** introduce external hosting, signed URLs, file copying, or a media store. A future artifact store can fill the same descriptor fields with `servable_by_dashboard=true` once the storage/auth/redaction contract is explicit.

## Open design questions

- Should local dashboard `POST /runs` remain synchronous or return an immediate durable run receipt before long-running execution?
- What is the canonical artifact store if media should be previewed inline later: workflow DB blobs, profile-local files under a served artifact root, or an operator-provided object store?
- Should `render_prompt` emit a runtime deprecation warning, or is docs-only standardization enough until a migration plan exists?

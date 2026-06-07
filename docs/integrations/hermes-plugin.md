# Hermes Agent approval plugin

`hermes-workflows` ships a thin Hermes Agent plugin adapter for human approval gates. The plugin does **not** turn Hermes into the workflow runtime; it lists and records approval decisions through the core runtime-agnostic adapter API:

- `WorkflowEngine.list_approvals(...)`
- `WorkflowEngine.get_approval(...)`
- `WorkflowEngine.submit_approval_decision(...)`
- `ApprovalDecisionInput`

The core package does not import Hermes. Hermes discovers the adapter through the `hermes_agent.plugins` Python entry point:

```toml
[project.entry-points."hermes_agent.plugins"]
hermes-workflows-approvals = "hermes_workflows.hermes_plugin_approvals"
```

## Install

From a checkout:

```bash
cd /path/to/hermes-workflows
pip install -e '.[dev]'
```

Hermes will discover the plugin when that environment is on the active Hermes Python path. In local development, the simplest path is running Hermes from the same venv where `hermes-workflows` is installed.

## Configure workflow DBs

The plugin can use an explicit SQLite path per tool call, but aliases are cleaner.

Hermes config shape:

```yaml
plugins:
  enabled:
    - hermes-workflows-approvals
  entries:
    hermes-workflows-approvals:
      workflow_dbs:
        - name: palmer
          path: /tmp/hermes-workflows-approval-smoke.sqlite
      # Required only if using the dashboard tab's approve/reject buttons.
      # The browser cannot assert human identity; the server stamps this id.
      dashboard_approver_id: skylar
```

Environment fallback for tests/scripts:

```bash
export HERMES_WORKFLOWS_DB=/tmp/hermes-workflows-approval-smoke.sqlite
export HERMES_WORKFLOWS_DBS='{"palmer":"/tmp/hermes-workflows-approval-smoke.sqlite"}'
export HERMES_WORKFLOWS_DASHBOARD_APPROVER_ID=skylar
```

## Hermes dashboard plugin

The same plugin directory also ships a Hermes dashboard extension under `plugins/hermes-workflows-approvals/dashboard/`:

```text
plugins/hermes-workflows-approvals/
  plugin.yaml
  __init__.py
  dashboard/
    manifest.json
    plugin_api.py
    dist/index.js
    dist/style.css
```

Install it into a Hermes profile by copying or symlinking the plugin directory into that profile's plugin root:

```bash
mkdir -p /Users/skylarpayne/.hermes/profiles/palmer/plugins
cp -R plugins/hermes-workflows-approvals /Users/skylarpayne/.hermes/profiles/palmer/plugins/
hermes -p palmer plugins enable hermes-workflows-approvals
```

Dashboard discovery is runtime-only: Hermes scans `$HERMES_HOME/plugins/<name>/dashboard/manifest.json`, serves the JS/CSS bundle, and mounts `plugin_api.py` under `/api/plugins/hermes-workflows-approvals`. No dashboard source fork or npm build is required.

Runtime/API semantics are documented in [`docs/architecture/dashboard-runtime-semantics-agentstep-approvals.md`](../architecture/dashboard-runtime-semantics-agentstep-approvals.md). In short: the dashboard DB dropdown selects a configured workflow DB alias, dashboard approval buttons are record-only (`resume=false`), and local/private artifact file paths are redacted rather than served.

The dashboard tab at `/workflows` shows:

- configured workflow DB aliases
- status counts
- workflow waiting/running/completed state
- recent events
- pending and historical commands
- diagnostics
- approval artifacts with secret-looking fields redacted
- record-only approve/reject decisions (`resume=false` always from the dashboard API)

Dashboard HTTP APIs are intentionally alias-only. They reject explicit SQLite paths, even though the lower-level CLI/tool adapter can accept paths, because dashboard routes run inside the Hermes process and must not become arbitrary local file readers/writers.

Dashboard approve/reject buttons are disabled unless `dashboard_approver_id` (or `HERMES_WORKFLOWS_DASHBOARD_APPROVER_ID`) is configured server-side. The browser does not send `by`, `channel`, or message provenance; the backend stamps `source={kind: human, id: <configured id>, channel: hermes-dashboard}` and records the decision without resuming the workflow.

## Tools

### `workflow_approvals_list`

Lists bounded, redacted approval cards from a configured DB alias or explicit SQLite path.

Input:

```json
{
  "db": "palmer",
  "status": "waiting",
  "limit": 20
}
```

Output includes `ApprovalView` fields plus exact decision tokens:

```json
{
  "success": true,
  "db": "/tmp/hermes-workflows-approval-smoke.sqlite",
  "count": 1,
  "approvals": [
    {
      "workflow_id": "wf_trip",
      "workflow_ref": "hermes_workflows.examples.trip:trip_planning_workflow",
      "key": "approve_trip_plan",
      "prompt": "Approve this trip plan?",
      "allowed": ["approve", "reject"],
      "decision_token_approve": "hwf-approval:v1:approve:...",
      "decision_token_reject": "hwf-approval:v1:reject:..."
    }
  ]
}
```

Secret-looking artifact keys are redacted before leaving the plugin (`token`, `secret`, `password`, `api_key`, etc.). This is a guardrail, not a replacement for avoiding secrets in approval artifacts.

### `workflow_approval_decide`

Records an approve/reject decision with human provenance.

Input:

```json
{
  "db": "palmer",
  "workflow_id": "wf_trip",
  "key": "approve_trip_plan",
  "action": "approve",
  "by": "skylar",
  "channel": "discord",
  "message_id": "...",
  "resume": false
}
```

`resume` defaults to `false`. This is intentional: a Hermes gateway/tool callback should record the decision, not accidentally run downstream workflow steps in the chat process. The returned receipt tells the operator what needs to resume.

Receipt shape:

```json
{
  "success": true,
  "receipt": {
    "workflow_id": "wf_trip",
    "key": "approve_trip_plan",
    "action": "approve",
    "status": "decision_recorded",
    "resume_requested": false,
    "workflow_ref": "hermes_workflows.examples.trip:trip_planning_workflow"
  },
  "next_step": "Run or queue a trusted workflow resumer for workflow_ref hermes_workflows.examples.trip:trip_planning_workflow."
}
```

Set `resume=true` only from a trusted local/operator context that is allowed to run workflow code immediately.

## Gateway hook

The plugin registers `pre_gateway_dispatch`, but it deliberately refuses fuzzy approvals.

Handled:

```text
hwf-approval:v1:approve:<structured-token>
hwf-approval:v1:reject:<structured-token>
```

Ignored:

```text
yes
looks good
approve it
sure
```

On exact token match, the hook records the decision with provenance from the gateway event and returns:

```json
{
  "action": "skip",
  "reason": "workflow approval decision recorded"
}
```

Otherwise it returns no-op so normal Hermes processing continues.

## Safe smoke

```bash
hermes-workflows run hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-approval-smoke.sqlite \
  --id wf_approval_smoke \
  --input-json '{"destination":"NYC","approver":"human:operator"}'

python - <<'PY'
import json
from hermes_workflows.hermes_plugin_approvals import _handle_workflow_approvals_list, _handle_workflow_approval_decide

print(_handle_workflow_approvals_list({"db":"/tmp/hermes-workflows-approval-smoke.sqlite"}))
print(_handle_workflow_approval_decide({
    "db":"/tmp/hermes-workflows-approval-smoke.sqlite",
    "workflow_id":"wf_approval_smoke",
    "key":"approve_trip_plan",
    "action":"approve",
    "by":"operator",
    "channel":"local-smoke",
    "message_id":"smoke-1",
    "resume": False,
}))
PY
```

Expected: approval decision recorded, workflow remains waiting until a trusted resumer continues it.

## Boundaries

- No Hermes imports in `hermes-workflows` core runtime.
- No fuzzy chat parsing.
- No public mutation or sends.
- No downstream workflow execution from gateway callbacks by default.
- Chat/buttons are convenience surfaces over the canonical workflow approval record, not the source of truth.

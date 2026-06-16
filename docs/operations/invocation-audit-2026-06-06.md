# hermes-workflows invocation audit receipt — 2026-06-06

Generated: 2026-06-06 14:02 PDT
Task: t_83bc39d0
Repo HEAD at audit start: `1005f8cc9f15d9e52c745d812335fd36b8882431`
Workspace: `/path/to/hermes-workflows`

## Verdict

Blog-readiness for synthetic/operator invocation claims: green after the docs reconciliation in this change set.

Do not claim real-provider agent(...) support was smoke-tested. The real-provider path remains explicitly opt-in behind `HERMES_WORKFLOWS_REAL_AGENT_ADAPTER=1` plus caller-supplied `HERMES_WORKFLOWS_AGENT_COMMAND`; this audit verified the deterministic fake-provider path only.

Generated workflow approval remains a review/audit gate, not a sandbox claim.

## Fresh clone / clean venv smoke

Command shape:

```bash
AUDIT_ROOT=$(mktemp -d /tmp/hermes-workflows-audit.XXXXXX)
git clone --depth 1 git@github.com:<owner>/hermes-workflows.git "$AUDIT_ROOT/repo"
cd "$AUDIT_ROOT/repo"
git rev-parse HEAD
uv venv .venv
. .venv/bin/activate
uv pip install -e '.[dev]'
python -m pytest -q
python -m hermes_workflows doctor \
  --db /tmp/hermes-workflows-doctor-smoke.sqlite \
  --workflow-ref hermes_workflows.examples.trip:trip_planning_workflow
```

Observed output:

```text
AUDIT_ROOT=/tmp/hermes-workflows-audit.nuvUej
HEAD=1005f8cc9f15d9e52c745d812335fd36b8882431
uv pip install -e '.[dev]' -> installed hermes-workflows==0.0.1 editable + pytest/build deps
pytest -> 148 passed, 2 skipped in 9.50s
doctor with explicit --db -> {"ok": true, "workflow_ref_importable": true, "db_parent_writable": true}
```

Note: `doctor` without `--db` returned `ok=false` in the fresh clone because the DB parent check was not writable/resolved. README/setup now use an explicit `/tmp/...sqlite` DB in the copy/paste path.

## Quickstart approval path

Command shape:

```bash
hermes-workflows run hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-quickstart.XXXXXX.sqlite \
  --id wf_trip_quickstart_audit \
  --input-json '{"destination":"NYC","approver":"human:operator"}'

hermes-workflows status --db /tmp/hermes-workflows-quickstart.XXXXXX.sqlite --id wf_trip_quickstart_audit

hermes-workflows approve hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-quickstart.XXXXXX.sqlite \
  --id wf_trip_quickstart_audit \
  --key approve_trip_plan \
  --by operator \
  --channel cli \
  --message-id audit-cli-approval-1 \
  --note 'audit quickstart approval path'
```

Observed output:

```text
run -> status=waiting, waiting_on=signal:approval.decision:approve_trip_plan
status -> approvals[0].key=approve_trip_plan, status=waiting, pending_commands=1
approve -> status=completed, waiting_on=null
final status -> approvals[0].source={kind: human, id: operator, channel: cli, message_id: audit-cli-approval-1}
recent events -> WaitRequested, SignalReceived, WorkflowCompleted
```

## Dashboard path

Static dashboard render:

```bash
hermes-workflows dashboard --db /tmp/hermes-workflows-dashboard-audit.XXXXXX.sqlite --out /tmp/hermes-workflows-dashboard-audit.html
```

Observed checks:

```text
/tmp/hermes-workflows-dashboard-audit.html exists
contains wf_trip_dashboard_audit=true
contains approve_trip_plan=true
contains prompt text=true
contains waiting=true
```

Read-only `serve-dashboard` smoke:

```bash
hermes-workflows serve-dashboard hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-dashboard-audit.XXXXXX.sqlite \
  --host 127.0.0.1 \
  --port 59776

curl GET / -> 200
curl POST /approve -> 405
```

Observed non-mutation:

```text
readonly_notice=true
no_local_form=true
post_error_mentions_disabled=true
event_count_before=6
event_count_after=6
pending_before=1
pending_after=1
status_after=waiting
waiting_on_after=signal:approval.decision:approve_trip_plan
```

Approval-enabled `serve-dashboard` smoke:

```bash
hermes-workflows serve-dashboard hermes_workflows.examples.trip:trip_planning_workflow \
  --db /tmp/hermes-workflows-dashboard-audit.XXXXXX.sqlite \
  --host 127.0.0.1 \
  --port 59793 \
  --once \
  --enable-approval-actions

curl GET / -> 200
curl POST /approve workflow_id=wf_trip_dashboard_audit key=approve_trip_plan by=operator channel=local-dashboard message_id=dashboard-approval-1 action=approve -> 200
```

Observed output:

```text
local_form=true
post_receipt=true
status=completed
waiting_on=null
pending_commands=0
approval_source={kind: human, id: operator, channel: local-dashboard, message_id: dashboard-approval-1}
SignalReceived.payload.signal_type=approval.decision
```

## Hermes plugin approval path in operator agent context

Created waiting trip workflow in an explicit DB, then used the live Hermes plugin tools available to operator agent:

```text
workflow_review_requests_list(db=/tmp/hermes-workflows-plugin-audit.XXXXXX.sqlite, status=waiting)
  -> success=true, count=1, key=approve_trip_plan, workflow_id=wf_trip_plugin_audit

workflow_approval_decide(
  db=/tmp/hermes-workflows-plugin-audit.XXXXXX.sqlite,
  workflow_id=wf_trip_plugin_audit,
  key=approve_trip_plan,
  action=approve,
  by=operator,
  channel=plugin-smoke,
  message_id=plugin-smoke-1,
  resume=false
)
  -> success=true, receipt.status=decision_recorded, resume_requested=false
```

Observed after decision:

```text
workflow status=waiting
waiting_on=signal:approval.decision:approve_trip_plan
pending_commands=[]
approval_status=approve
approval_source={kind: human, id: operator, channel: plugin-smoke, message_id: plugin-smoke-1}
recent_events include SignalReceived(signal:approval.decision:approve_trip_plan)
```

That is the intended plugin behavior: record the approval with provenance, do not run workflow code in the chat/plugin callback when `resume=false`.

## agent(...) / dynamic generated workflow path

Targeted tests:

```bash
python -m pytest -q \
  tests/test_agent_cli_adapter.py \
  tests/test_subprocess_agent_runner.py \
  tests/test_agent_runner.py \
  tests/test_dynamic_workflow_return.py \
  tests/test_workflows_demo_2026_06_05.py
```

Observed output:

```text
51 passed, 1 skipped in 2.66s
```

Synthetic generated-workflow demo:

```bash
python examples/workflows_demo_2026_06_05.py \
  --db /tmp/hermes-workflows-demo-audit.XXXXXX.sqlite \
  --id wf_workflows_demo_audit \
  --artifact /tmp/hermes-workflows-demo-audit/index.html \
  --receipt-json /tmp/hermes-workflows-demo-audit/receipt.json
```

Observed output:

```json
{
  "status": "completed",
  "agent_calls": 7,
  "draft_count": 4,
  "event_count": 41,
  "approvals": [
    "generated_workflow_execution",
    "agent_email_quality_approval",
    "human_email_batch_approval"
  ],
  "side_effects": {"emails_sent": 0, "gmail_drafts_created": 0},
  "generated_workflow": {
    "symbol": "participant_email_personalization_workflow",
    "source_sha256": "0c3177522fcbcaf74efafdb9296da555fcaf11b7c3c723e15975670d8b43d071"
  }
}
```

## Docs reconciled in this change set

Patched:

- `README.md`
  - doctor command now includes explicit `--db /tmp/hermes-workflows-doctor.sqlite`.
  - quickstart distinguishes read-only `serve-dashboard` from approval-enabled local button path.
  - approval-enabled dashboard example now includes `--enable-approval-actions`.
- `docs/setup-for-agents.md`
  - install smoke now includes doctor with explicit `--db`.
  - dashboard server section now says default mode is read-only/non-mutating.
  - local approval buttons require `--enable-approval-actions`.
  - real-provider smoke is documented as opt-in only.
- `skills/devops/hermes-workflows/SKILL.md`
  - local dashboard guidance now starts read-only and requires `--enable-approval-actions` for POST approval buttons.

## Remaining blockers / caveats

- No blog was drafted or published.
- Repo was not made public.
- No credentials were created, imported, or mutated.
- No real-provider agent(...) smoke was run; only deterministic fake-provider tests/demos were verified.
- This audit changed docs/skill files locally; they should be reviewed/committed/merged before treating the docs reconciliation as landed in the repo.

# Set up `hermes-workflows` for an agent

`hermes-workflows` is a small durable runtime you put underneath an agent when the work needs memory, approval gates, generated child workflows, and receipts.

Use it when the agent should prepare work and stop before side effects.

## Install locally

```bash
git clone https://github.com/skylarbpayne/hermes-workflows.git
cd hermes-workflows
python -m pip install -e '.[dev]'
pytest -q
```

Run the CLI:

```bash
hermes-workflows --help
python -m hermes_workflows --help
```

Render a local read-only dashboard for any existing workflow DB:

```bash
hermes-workflows dashboard --db /tmp/workflow.sqlite --out /tmp/workflows-dashboard.html
```

Or run a local approval server that still routes every button through the canonical `approval.decision` signal path:

```bash
hermes-workflows serve-dashboard my_package.workflow:main \
  --db /tmp/workflow.sqlite \
  --host 127.0.0.1 \
  --port 8765
```

That local server is intentionally boring: it is not an agent runtime, and it does not invent a second approval model. It only captures human provenance and calls the same engine signal API that Discord, Telegram, a Hermes plugin, or another runtime adapter should call.

## The mental model

A workflow function is a decider. It replays from the top on every run, resolves completed steps from SQLite history, and exits cleanly whenever it needs a step, signal, or approval.

```text
agent/user request
  -> workflow starts
  -> step commands are recorded
  -> agent runner completes agent steps
  -> generated workflow source is recorded + hashed
  -> approval signal gates generated execution
  -> output packet is created
  -> side effects stay blocked until a separate approval
```

## Minimal workflow

```python
from hermes_workflows import WorkflowEngine, step, workflow

@step
async def draft_packet(ctx, inputs):
    return {"draft": "prepared"}

@workflow
async def approval_gated_workflow(ctx, inputs):
    packet = await draft_packet(ctx, inputs)
    decision = await ctx.approval.request(
        "Approve this prepared packet?",
        key="approve_packet",
        artifact=packet,
        approver="human:operator",
        allowed=["approve", "reject"],
    )
    return {"packet": packet, "decision": decision, "side_effects": {"sent": 0}}

engine = WorkflowEngine("workflow.sqlite")
print(engine.run_until_idle(approval_gated_workflow, {"topic": "demo"}, workflow_id="wf_demo"))
```

When the approval is ready, submit a typed decision. This is the adapter seam for CLIs, dashboards, Hermes plugins, Discord, Telegram, or any other runtime:

```python
from hermes_workflows import ApprovalDecisionInput

receipt = engine.submit_approval_decision(
    ApprovalDecisionInput(
        workflow_id="wf_demo",
        key="approve_packet",
        action="approve",
        by="operator",
        source={"kind": "human", "id": "operator", "channel": "review-ui", "message_id": "approval-message-1"},
        idempotency_key="approval-message-1",
    ),
    resume=True,
)
```

Use `resume=False` for chat/plugin callbacks that should record the approval but leave downstream work to a separate worker/resumer.

## Add an agent step

Agent steps call a runner through JSON. The included `agent_cli_adapter` wraps a provider CLI so the workflow runtime sees a strict, sanitized response.

```python
from hermes_workflows import AgentStep, SubprocessAgentRunner, Workflow, WorkflowEngine, workflow

runner = SubprocessAgentRunner([
    "hermes-workflows-agent-cli-adapter",
    "--agent-command", "python",
    "--agent-arg", "examples/runners/workflows_demo_agent.py",
])

@workflow
async def agent_workflow(ctx, inputs):
    generated = await AgentStep(
        "workflow_architect",
        prompt="Write a child workflow for {{event_name}} participant follow-up.",
        variables={"event_name": inputs["event_name"]},
        returns=Workflow,
    )(ctx)

    decision = await ctx.approval.request(
        "Approve generated workflow execution?",
        key="generated_workflow_execution",
        artifact={"symbol": generated.symbol, "source_sha256": generated.source_sha256},
        approver="human:operator",
        allowed=["approve", "reject"],
    )

    return {"generated": generated.symbol, "decision": decision}

engine = WorkflowEngine("workflow.sqlite", agent_runner=runner)
```

The runner can be deterministic for tests and demos, or it can call a real provider. The boundary is the same: JSON request in, structured response and provenance out.

## Run the Hack the Valley demo

Synthetic public demo:

```bash
PYTHONPATH=src:. pytest tests/test_workflows_demo_2026_06_05.py -q
PYTHONPATH=src:. python examples/workflows_demo_2026_06_05.py \
  --db /tmp/workflows-demo-2026-06-05.sqlite \
  --id wf_workflows_demo_2026_06_05 \
  --artifact dist/workflows-demo-2026-06-05/index.html
```

Private real-data dry run:

```bash
PYTHONPATH=src:. python examples/build_hackathon_email_snapshot.py \
  --registration-csv /path/to/private-registration-export.csv \
  --submissions-json /path/to/private-submissions-export.json \
  --prizes-json /path/to/reviewed-prizes.json \
  --out /tmp/workflows-real-run/snapshot.json

HERMES_WORKFLOWS_HACKATHON_SNAPSHOT=/tmp/workflows-real-run/snapshot.json \
PYTHONPATH=src:. python examples/workflows_demo_2026_06_05.py \
  --db /tmp/workflows-real-run/workflow.sqlite \
  --id wf_htv_real_snapshot_dry_run \
  --artifact /tmp/workflows-real-run/review-packet/index.html \
  --receipt-json /tmp/workflows-real-run/receipt.json
```

Then render a public-safe output packet:

```bash
PYTHONPATH=src:. python examples/redact_hackathon_review_packet.py \
  --receipt /tmp/workflows-real-run/receipt.json \
  --snapshot /tmp/workflows-real-run/snapshot.json \
  --out-dir dist/workflows-real-run-output
cp dist/workflows-real-run-output/packet.json examples/outputs/hackathon-real-dry-run.redacted.json
```

## Safety defaults

- Generated workflow code is inspectable, not silently trusted.
- Approval decisions are accepted only after the matching approval request exists.
- Invalid approval decisions fail closed before they are appended to workflow history or used to complete approval notification commands.
- Approval to execute generated code is separate from approval to create drafts or send email.
- The static dashboard is read-only; `serve-dashboard` can approve, but only through `submit_approval_decision()` / canonical `approval.decision` validation with human provenance.
- Hermes Agent integration lives behind the `hermes-workflows-approvals` plugin entry point; see `docs/integrations/hermes-plugin.md`. Plugin/gateway approvals default to `resume=false` so chat callbacks record decisions without running downstream workflow code.
- The CLI prints redacted summaries by default.
- Public packets omit raw draft bodies, raw event payloads, private file paths, participant names/emails, project text, and URLs.
- Raw snapshots, receipts, workflow DBs, and real review packets stay private.
- Side effects should be their own explicit workflow step with its own approval key.

If your agent cannot explain what it read, what it generated, who approved it, and what it did not do, it is not ready to touch production systems.

---
layout: page
title: Set up hermes-workflows for an agent
---

# Set up `hermes-workflows` for an agent

`hermes-workflows` is a small durable runtime you put underneath an agent when the work needs memory, approval gates, generated child workflows, and receipts.

Use it when the agent should prepare work and stop before side effects.

## Install locally inside a Hermes workspace

Use a trusted Hermes workspace (or another local operator workspace) to own the source checkout, virtualenv, workflow DBs, and registry files. This repository does **not** currently implement a `hermes workflows` subcommand; use the tested CLI entry point `hermes-workflows` or `python -m hermes_workflows`.

```bash
git clone https://github.com/<owner>/hermes-workflows.git
cd hermes-workflows

# Runtime/user install. Do not require dev extras for user setup.
python -m pip install .

hermes-workflows doctor \
  --db /tmp/hermes-workflows-doctor.sqlite \
  --workflow-ref hermes_workflows.examples.trip:trip_planning_workflow
```

For contributor work, install development extras separately and run tests:

```bash
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

Or run a local dashboard server. By default this is read-only and does not import the workflow or mutate the workflow DB:

```bash
hermes-workflows serve-dashboard my_package.workflow:main \
  --db /tmp/workflow.sqlite \
  --host 127.0.0.1 \
  --port 8765
```

To expose local approval POST buttons, opt in explicitly:

```bash
hermes-workflows serve-dashboard my_package.workflow:main \
  --db /tmp/workflow.sqlite \
  --host 127.0.0.1 \
  --port 8765 \
  --enable-approval-actions
```

That local server is intentionally boring: it is not an agent runtime, and it does not invent a second approval model. When `--enable-approval-actions` is present, it only captures human provenance and calls the same engine signal API that Discord, Telegram, a Hermes plugin, or another runtime adapter should call.

## Agent/operator bridge

For recurring agent-owned workflows, keep aliases in `.hermes/workflows.registry.json` and invoke them with the product-shaped runner:

```json
{
  "dbs": {"agent": "/tmp/agent-workflows.sqlite"},
  "workflows": {
    "demo": {
      "workflow_ref": "hermes_workflows.examples.trip:trip_planning_workflow",
      "db": "agent",
      "trusted_resume": true
    }
  }
}
```

```bash
hermes-workflows run demo \
  --config .hermes/workflows.registry.json \
  --id wf_demo \
  --input-json '{"destination":"NYC","approver":"human:operator"}'
```

`run` resolves registry aliases, module refs, and workflow file paths, then re-invokes through `uv`. If no DB is configured or passed, it uses `<project-root>/.hermes/workflows.sqlite` so a runner, worker, and later resume do not accidentally split state across multiple SQLite files. For direct `uv run workflow.py`, the workflow project's uv environment must be able to import `hermes_workflows`.

For adapter surfaces that need redacted receipts and source metadata, use the bridge invocation command:

```bash
hermes-workflows invoke demo \
  --config .hermes/workflows.registry.json \
  --id wf_demo \
  --input-json '{"destination":"NYC","approver":"human:operator"}' \
  --source-json '{"kind":"operator","channel":"kanban","task_id":"t_..."}' \
  --receipt-json /tmp/wf-demo-invoke.json
```

If a Hermes plugin/gateway decision records the approval with `resume=false`, do not run workflow code in the gateway process. Use a trusted local operator or cron path instead:

```bash
hermes-workflows resume-trusted demo \
  --config .hermes/workflows.registry.json \
  --id wf_demo \
  --receipt-json /tmp/wf-demo-resume.json

hermes-workflows resume-pending \
  --config .hermes/workflows.registry.json \
  --registry-name demo \
  --limit 5
```

`resume-pending` fails closed unless the registry entry has `trusted_resume: true`. Receipts are redacted JSON that can be pasted into Kanban comments or attached to dashboard artifacts.

## The mental model

A workflow function is a decider. It replays from the top on every run, resolves completed steps from SQLite history, and exits cleanly whenever it needs a step, signal, or approval. Workers do not resume a suspended Python stack; they publish durable outputs/signals. The runner or supervisor re-runs the same entrypoint against the same DB/run id, and memoized values let control flow advance.

```text
operator runs `hermes-workflows run <name-or-path>` or `uv run workflow.py`
  -> workflow emits missing commands and exits when waiting
worker/adapter publishes output, signal, or approval
  -> `hermes-workflows run --watch` or another trusted runner re-invokes the entrypoint
  -> completed calls replay from SQLite, then the decider reaches the next wait or terminal result
```

## Minimal workflow

```python
from hermes_workflows import step, workflow

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

if __name__ == "__main__":
    raise SystemExit(approval_gated_workflow.run())
```

Run it as a normal uv script or through the CLI:

```bash
uv run workflows/approval_gated.py --id wf_demo --input-json '{"topic":"demo"}'
hermes-workflows run workflows/approval_gated.py --id wf_demo --input-json '{"topic":"demo"}'
```

When the approval is ready, submit a typed decision. This is the adapter seam for CLIs, dashboards, Hermes plugins, Discord, Telegram, or any other runtime:

```bash
hermes-workflows approve workflows/approval_gated.py \
  --db .hermes/workflows.sqlite \
  --id wf_demo \
  --key approve_packet \
  --by operator \
  --channel review-ui \
  --message-id approval-message-1
```

Use `resume=False` in embedding/plugin callbacks that should record the approval but leave downstream work to a separate worker/resumer.

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

The default test/demo runner is deterministic and credential-free. A real-provider smoke is opt-in only: set `HERMES_WORKFLOWS_REAL_AGENT_ADAPTER=1` and provide `HERMES_WORKFLOWS_AGENT_COMMAND` in the caller's environment. Do not report real-provider support as verified unless that explicit smoke was run; the boundary is the same either way: JSON request in, structured response and provenance out.

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

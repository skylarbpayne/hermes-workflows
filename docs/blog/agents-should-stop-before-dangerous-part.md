# Agents Should Stop Before the Dangerous Part

Status: draft 0.2
Audience: builders adding durable workflow control to agents
Example: Hack the Valley participant follow-up workflow

## Thesis

Useful agent infrastructure is not measured by how quickly it can touch production. It is measured by whether it can prepare the work, show its reasoning trail, ask for the right approvals, and stop before a side effect.

That is what `/workflows` is for.

An agent can write an email. That is table stakes. The useful system answers harder questions first:

- Who is in the target set?
- What source data was used?
- Which records failed to join cleanly?
- What workflow code did the agent generate?
- Who approved that generated code before it ran?
- What did the QA step check?
- What output was produced for review?
- What external systems were touched?
- What proof shows that Gmail, Sheets, the database, or another production system stayed unchanged?

Without those answers, “human in the loop” is mostly decoration.

## The example: hackathon participant follow-up

The practical workflow is ordinary on purpose:

```text
registration roster
  -> project submissions
  -> prize / judging lookup
  -> personalized participant email draft packet
  -> generated child workflow source + SHA
  -> agent email-quality approval
  -> human batch approval
  -> side-effect receipt
```

This is a good test case because the mistakes are obvious.

A bad agent sends the wrong person a confident email about the wrong project. A slightly better agent asks for review. A useful workflow system shows the join quality, draft coverage, generated workflow hash, approval trail, blocker summary, and zero-send receipt before anyone can create Gmail drafts.

## The actual dry-run output

We ran the Hack the Valley workflow against private real event exports, then generated a public-safe derivative packet.

Raw private packet stayed local. The public-safe packet keeps operational proof and removes names, emails, project titles, URLs, raw draft bodies, raw event payloads, and private paths.

Latest redacted evidence:

```text
registration rows: 62
submission rows: 16
participants drafted: 28
participants without confident project match: 15
agent calls: 7
audit events: 41
approval gates: 3
Gmail drafts created: 0
emails sent: 0
```

Approval gates:

```text
generated_workflow_execution
agent_email_quality_approval
human_email_batch_approval
```

Generated workflow receipt:

```text
symbol: participant_email_personalization_workflow
sha256: recorded in the redacted packet
```

The most useful result was the blocker: 15 checked-in participants could not be confidently matched to a submitted project. That is the kind of issue an automation layer should surface before it puts polished language around bad data.

Demo/output artifacts in the repo:

- `docs/output/hackathon-redacted-packet-2026-06-05/index.html`
- `docs/output/hackathon-redacted-packet-2026-06-05/packet.json`
- `examples/outputs/hackathon-real-dry-run.redacted.json`

The protected review artifact is registered as:

```text
/workflows-real-run-output
```

## Why generated workflows should be code

The parent workflow is Python. The generated child workflow is Python too.

That matters because generated operational behavior should be inspectable with normal engineering tools. The review target should have:

- source code
- selected symbol
- SHA-256 hash
- provenance
- approval key
- testable behavior

YAML can describe metadata. It is a bad place to hide operational judgment.

In this run, the parent workflow asked a workflow-architect agent for child workflow code. The runtime recorded the source and hash. Execution stopped at `generated_workflow_execution` until that generated code was approved.

## How to set this up for your agent

The setup pattern is simple:

1. Put a durable workflow runtime under the agent.
2. Make agent calls explicit steps.
3. Store every command, result, generated source file, approval decision, and side-effect receipt.
4. Treat generated workflow execution as a separate approval from production side effects.
5. Run private data through snapshots first.
6. Publish only redacted derivative outputs.

Install locally:

```bash
git clone https://github.com/<owner>/hermes-workflows.git
cd hermes-workflows
python -m pip install -e '.[dev]'
pytest -q
```

Run the synthetic demo:

```bash
PYTHONPATH=src:. python examples/workflows_demo_2026_06_05.py \
  --db /tmp/workflows-demo.sqlite \
  --id wf_demo \
  --artifact dist/workflows-demo-2026-06-05/index.html
```

For a private real-data dry run, build an explicit snapshot:

```bash
PYTHONPATH=src:. python examples/build_hackathon_email_snapshot.py \
  --registration-csv /path/to/private-registration-export.csv \
  --submissions-json /path/to/private-submissions-export.json \
  --prizes-json /path/to/reviewed-prizes.json \
  --out /tmp/workflows-real-run/snapshot.json
```

Then run the same workflow with that snapshot:

```bash
HERMES_WORKFLOWS_HACKATHON_SNAPSHOT=/tmp/workflows-real-run/snapshot.json \
PYTHONPATH=src:. python examples/workflows_demo_2026_06_05.py \
  --db /tmp/workflows-real-run/workflow.sqlite \
  --id wf_real_dry_run \
  --artifact /tmp/workflows-real-run/review-packet/index.html \
  --receipt-json /tmp/workflows-real-run/receipt.json
```

Render a public-safe output packet:

```bash
PYTHONPATH=src:. python examples/redact_hackathon_review_packet.py \
  --snapshot /tmp/workflows-real-run/snapshot.json \
  --receipt /tmp/workflows-real-run/receipt.json \
  --out-dir docs/output/hackathon-redacted-packet-2026-06-05
```

## The agent runner boundary

For demos and tests, the repo uses a deterministic subprocess runner. That keeps demos stable.

The runtime boundary is the important part:

```text
workflow step -> JSON request -> agent runner -> structured JSON response -> provenance event
```

The runner can call a deterministic script, Hermes, Claude Code, Codex, OpenAI, Anthropic, or a local model. The workflow runtime does not need to own the model loop. It needs a strict contract: request, response, provenance, approval gates, and receipts.

## Minimal shape

```python
from hermes_workflows import AgentStep, SubprocessAgentRunner, Workflow, WorkflowEngine, workflow

runner = SubprocessAgentRunner([
    "hermes-workflows-agent-cli-adapter",
    "--agent-command", "your-agent-cli",
])

@workflow
async def production_shaped_agent_workflow(ctx, inputs):
    generated = await AgentStep(
        "workflow_architect",
        prompt="Write a child workflow for {{operation_name}}.",
        variables={"operation_name": inputs["operation_name"]},
        returns=Workflow,
    )(ctx)

    await ctx.approval.request(
        "Approve generated workflow execution?",
        key="generated_workflow_execution",
        artifact={
            "symbol": generated.symbol,
            "source_sha256": generated.source_sha256,
        },
        approver="human:operator",
        allowed=["approve", "reject"],
    )

    packet = await generated(ctx, inputs)

    await ctx.approval.request(
        "Approve the side-effect packet?",
        key="human_side_effect_approval",
        artifact=packet,
        approver="human:operator",
        allowed=["approve", "reject"],
    )

    return {
        "packet": packet,
        "side_effects": {"emails_sent": 0},
    }

engine = WorkflowEngine("workflow.sqlite", agent_runner=runner)
```

The side-effect step should come after this, under its own approval key. Draft approval and send approval are different decisions.

## What this buys you

The value is operational:

- replayable workflow state instead of one-shot chat history
- inspectable generated code instead of invisible agent behavior
- approval keys tied to specific artifacts
- private dry runs before production writes
- receipts that prove what happened and what did not happen
- redacted derivative outputs you can share in docs or a blog post
- eval cases from real blockers

This is the path from “agent did a thing” to “we can review and trust this work.”

## Production rule

The production rule is blunt:

> If an agent cannot show what it read, what it generated, who approved it, what changed, and what did not change, it should not touch the system of record.

The whole point of `/workflows` is to make that rule easy to enforce.

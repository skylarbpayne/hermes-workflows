# hermes-workflows

`hermes-workflows` makes long-running agent work reviewable instead of ephemeral. It gives a Hermes-operated workspace durable workflow state, memoized steps, explicit human approval gates, and receipts that survive process exits, model handoffs, and restarts. The goal is not to replace Hermes Agent with a smarter orchestrator; it is to give Hermes a small, auditable runtime for work that must stop, wait, resume, and prove what happened.

## Quickstart

Use the `hermes-workflows` CLI from inside a Hermes workspace or another trusted local operator workspace. This repository does **not** currently install a `hermes workflows` subcommand; until such a wrapper exists and is tested, use `hermes-workflows` or `python -m hermes_workflows`.

```bash
# From a source checkout. This installs runtime/user dependencies only, not dev extras.
python -m pip install .

hermes-workflows doctor \
  --db /tmp/hermes-workflows-doctor.sqlite \
  --workflow-ref hermes_workflows.examples.trip:trip_planning_workflow

hermes-workflows run hermes_workflows.examples.trip:trip_planning_workflow \
  --id wf_trip_quickstart \
  --input-json '{"destination":"NYC","approver":"human:operator"}'

hermes-workflows status \
  --db .hermes/workflows.sqlite \
  --id wf_trip_quickstart
```

`hermes-workflows run <name-or-path>` is the default operator command. It re-invokes itself through `uv`, resolves registry names, `module:function` refs, or workflow files, and stores state in `<project-root>/.hermes/workflows.sqlite` unless `--db` is explicitly supplied. A workflow file can also opt into the same behavior directly:

```python
if __name__ == "__main__":
    trip_planning_workflow.run()
```

Then `uv run workflow.py --id wf_demo --input-json '{...}'` uses the same memoized runtime without embedding engine plumbing in the workflow definition. The workflow project's uv environment must be able to import `hermes_workflows` — add this package as a dependency or run from a source checkout with `PYTHONPATH=src` during development.

The quickstart stops at `approve_trip_plan`. That is intentional: a workflow can do deterministic local work, persist the wait state, and exit cleanly before a human-authorized side effect. To resume it from the CLI, record a human-sourced approval:

```bash
hermes-workflows approve hermes_workflows.examples.trip:trip_planning_workflow \
  --db .hermes/workflows.sqlite \
  --id wf_trip_quickstart \
  --key approve_trip_plan \
  --by operator \
  --channel cli \
  --message-id manual-approval-1
```

## Toy workflow

Workflow code is ordinary Python, but `@step` calls are durable awaits. Completed steps replay from SQLite history; missing steps, signals, or approvals are recorded and the decider exits.

```python
from hermes_workflows import ApprovalDecisionInput, WorkflowEngine, step, workflow


@step
async def draft_release_note(ctx, inputs):
    return {
        "title": f"Release note for {inputs['feature']}",
        "body": "This is a toy artifact for human review.",
    }


@workflow
async def release_note_workflow(ctx, inputs):
    note = await draft_release_note(ctx, inputs)
    decision = await ctx.approval.request(
        "Approve publishing this release note?",
        key="approve_release_note",
        artifact=note,
        approver="human:operator",
        allowed=["approve", "reject"],
        authority=["publish_release_note"],
    )
    return {"note": note, "approval": decision, "published": False}


engine = WorkflowEngine(".hermes/workflows.sqlite")
print(engine.run_until_idle(
    release_note_workflow,
    {"feature": "durable approvals"},
    workflow_id="wf_release_note_demo",
))

receipt = engine.submit_approval_decision(
    ApprovalDecisionInput(
        workflow_id="wf_release_note_demo",
        key="approve_release_note",
        action="approve",
        by="operator",
        source={
            "kind": "human",
            "id": "operator",
            "channel": "cli",
            "message_id": "manual-approval-1",
        },
        idempotency_key="manual-approval-1",
    ),
    resume=True,
)
print(receipt.status)
```

## Runtime model in one screen

```text
operator runs `hermes-workflows run <name-or-path>` or `uv run workflow.py`
  -> the same workflow entrypoint opens the same SQLite DB/run id
  -> completed steps/signals/approvals replay as memoized values
  -> missing work is emitted as durable commands and the process can exit
workers/adapters
  -> publish step outputs, signals, and approval decisions durably
runner/supervisor
  -> re-invokes the same entrypoint/DB/run (`hermes-workflows run --watch` when desired)
  -> control flow advances until the workflow is waiting again or terminal
```

Workflow code runs in the Python process that imports it: the CLI, a worker, a trusted resumer, or an embedding Hermes adapter. Agent steps are not magic in-process model calls; they execute through configured runner seams such as `SubprocessAgentRunner`, which sends bounded JSON to a trusted local command and fails closed on timeout, invalid JSON, non-zero exit, or oversized output.

## Documentation

- [Docs site index](docs/index.md)
- [Architecture, domain model, seams, execution environments, and failure modes](docs/architecture/domain-model-and-seams.md)
- [Hermes/operator setup guide](docs/setup-for-agents.md)
- [Runtime vs skills/subagents boundary](docs/architecture/runtime-vs-skills-subagents.md)
- [Approval adapters and Hermes plugin](docs/architecture/approval-adapters-and-hermes-plugin.md)
- [Inspectability cookbook](docs/operations/inspectability-cookbook.md)
- [Documentation summary and CI notes](docs/summary.md)

## Examples directories

- `examples/` contains runnable repository/demo workflows, deterministic test runners, scripts, prompts, and larger scenario assets. These are for contributors and operators working from the source tree.
- `src/hermes_workflows/examples/` contains small installed examples that can be imported after package installation, such as `hermes_workflows.examples.trip:trip_planning_workflow`. Quickstarts should prefer these installed examples.

### Bounded email triage example

`hermes_workflows.examples.email_triage:email_triage_workflow` is the first real operator-style example: it classifies a bounded email candidate set, stops at a human approval gate, and only writes local proposal artifacts after approval. It never sends, archives, deletes, marks mail, creates Gmail drafts, mutates calendars/accounts, or changes credentials.

For a live private dry run, build a redacted Gmail search fixture first. The helper reads only `gog gmail search` metadata through a profile-aware command, then writes symbolic handles/signals for the workflow input; raw senders, subjects, snippets, bodies, thread IDs, and account emails are not written to the fixture.

```bash
uv run python examples/build_email_triage_live_fixture.py \
  --account you@example.com \
  --query 'newer_than:2d in:inbox' \
  --max-per-account 5 \
  --out /tmp/email-triage-input.json

uv run python -m hermes_workflows run \
  hermes_workflows.examples.email_triage:email_triage_workflow \
  --id wf_email_triage_private_dry_run \
  --input-json "$(cat /tmp/email-triage-input.json)"
```

The run should stop at `approve_email_triage_packet`. Record-only approvals from chat/dashboard adapters should use `resume=false`; a trusted local operator can resume later to write the local proposal packet. Approval to write the local packet is still **not** approval to draft, send, archive, or mutate email.

## Development checks

```bash
python -m pip install -e '.[dev]'
pytest -q
python -m compileall -q src tests examples
git diff --check
```

Pull requests are covered by `.github/workflows/test.yml`, which runs on `pull_request` to `main` for Python 3.9 and 3.11. The docs site workflow also validates the GitHub Pages/Jekyll build on pull requests without deploying.

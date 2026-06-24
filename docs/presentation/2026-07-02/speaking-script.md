# Hermes Workflows July 2 speaking script

Working title: The agent had the instruction. The system did not.

Length: 12-18 minutes. Goal: make Hermes Workflows feel obvious to builders who already use agents and have been burned by them.

## 0. Opening, 60 seconds

The problem is not that agents never follow instructions. The problem is worse: they often follow the instruction once, then the requirement disappears into chat history, a prompt, a handoff, or a checklist nobody can inspect.

You can tell an agent: do not send without approval. Run the tests. Capture evidence. Stop at review. It may do that today. Then tomorrow a different run, a different model, a different context window, or a different helper misses it.

That is the gap Hermes Workflows is built for.

## 1. The pain, 2 minutes

Use a concrete example:

- A coding agent drafts a fix.
- The real requirement is not only "change the code." It is plan, isolate the branch, run tests, build a review packet, stop before merge, preserve approval provenance, and keep a receipt.
- Those requirements are too important to leave as vibes inside a prompt.

The uncomfortable line: if it must survive restarts, handoffs, approvals, and future review, it should be workflow state.

## 2. The mental model, 2 minutes

Prompts, skills, and subagents are influence surfaces. They shape behavior.

Workflows are obligation surfaces. They record what happened, what is waiting, who approved, what artifacts exist, and which side effects did or did not occur.

The authoring API stays small:

```python
from hermes_workflows import agent, ask, bash, goal, parallel, pipeline, workflow
```

Explain each in one line:

- `agent(...)`: typed AI or worker work.
- `ask(...)`: typed Review Queue input.
- `bash(...)`: deterministic checks with captured output.
- `parallel(...)` and `pipeline(...)`: fan-out and staged work without losing durable identity.
- `goal(...)`: bounded improve-until-accepted loops.
- `workflow`: ordinary Python, durable execution underneath.

## 3. Demo one: tiny reviewable workflow, 4 minutes

Say: I am going to run the smallest possible public demo. It does no external side effects. It drafts a packet with deterministic mock output, then stops at a typed Review Queue request.

Show commands from `demo-runbook.md`:

```bash
python -m pip install -e '.[dev]'
hermes-workflows run reviewable-draft   --config docs/presentation/2026-07-02/workflows.registry.example.json   --project-root .   --db default   --id wf_july2_reviewable_draft   --input-json '{"topic":"Hermes Workflows July 2 demo"}'
hermes-workflows worker   --config docs/presentation/2026-07-02/workflows.registry.example.json   --db default   --worker-id july2-demo-worker   --max-commands 5   --idle-exit-after 0.1
hermes-workflows status --db .hermes/presentation-july2/workflows.sqlite --id wf_july2_reviewable_draft
```

Narrate what matters:

- The CLI did not pretend to be a forever worker.
- The worker leased commands and stopped at human input.
- The Review Queue schema came from the return type.
- The artifact says what the human is approving.
- Side effects are still zero.

## 4. Demo two: dynamic workflow composition, 3 minutes

Say: the second demo shows that workflows are not just static scripts. An agent can return a durable workflow value. The runtime stores the generated workflow source hash, runs child workflows, and records the child results.

Run or show the verified output:

```bash
hermes-workflows run dynamic-workflow-return   --config docs/presentation/2026-07-02/workflows.registry.example.json   --project-root .   --db default   --id wf_july2_dynamic_return   --input-json '{}'
hermes-workflows worker   --config docs/presentation/2026-07-02/workflows.registry.example.json   --db default   --worker-id july2-demo-worker   --max-commands 20   --idle-exit-after 0.1
```

Verified result from local smoke: completed, generated symbol `process_launch_item`, processed `dynamic-examples` and `subworkflow-ui`.

## 5. What this is not, 90 seconds

This is not a new prompt format.
This is not a replacement for skills.
This is not a dashboard bolted onto chat logs.
This is not an excuse to automate external side effects without approval.

It is a way to make agent work inspectable when the work has obligations.

## 6. Close, 60 seconds

The pitch:

Hermes Workflows turns the parts of agent work that keep getting dropped into explicit state: typed work, review gates, workers, checks, artifacts, and receipts.

Use agents for judgment. Use workflows for obligations.

End by showing the public examples map and docs URL.

# Approval adapters and Hermes plugin path

`hermes-workflows` core should stay runtime-agnostic. The approval capability is useful only if humans can approve from the surface they already use, but the core library should not become Discord-specific, Telegram-specific, or Hermes-specific.

## Stable core contract

Core owns:

- workflow status/read APIs
- `ApprovalRequested` events
- canonical `approval.decision` signals
- validation: prior request, allowed action, approver match, human source, external provenance, duplicate/conflict rejection
- receipts/events/status packets

Core should not own:

- Discord/Telegram/Gmail/Kanban identity mapping
- message delivery
- notification fanout
- Hermes profile config
- chat-specific buttons or callbacks
- credential storage

## Adapter shape

Every approval surface should do the same thing:

```text
waiting workflow
  -> adapter calls engine.list_approvals() or engine.get_approval(workflow_id, key)
  -> adapter displays ApprovalView as an approval card
  -> human clicks/types approve or reject
  -> adapter captures provenance
  -> adapter calls engine.submit_approval_decision(ApprovalDecisionInput(...), resume=True|False)
  -> adapter posts ApprovalReceipt/status
```

The canonical adapter API is intentionally boring:

```python
from hermes_workflows import ApprovalDecisionInput, WorkflowEngine

engine = WorkflowEngine("/tmp/workflow.sqlite")
approvals = engine.list_approvals(status="waiting")

receipt = engine.submit_approval_decision(
    ApprovalDecisionInput(
        workflow_id="wf_trip",
        key="approve_trip_plan",
        action="approve",
        by="skylar",
        source={"kind": "human", "id": "skylar", "channel": "discord", "message_id": "150828..."},
        idempotency_key="discord:150828:approve_trip_plan:approve",
    ),
    # Use False for chat callbacks that should record the decision but hand resume to a worker.
    resume=True,
)
```

Under the hood this still records the same validated `approval.decision` event; adapters should use the typed API so they do not accidentally invent a parallel approval path.

The underlying signal payload remains boring for lower-level integrations:

```json
{
  "payload": {"action": "approve", "by": "skylar"},
  "source": {
    "kind": "human",
    "id": "skylar",
    "channel": "discord",
    "message_id": "150828..."
  },
  "idempotency_key": "discord:150828...:approve_trip_plan"
}
```

## Current adapters

- CLI: `hermes-workflows approve|reject`.
- Static dashboard: `hermes-workflows dashboard` renders status and approval shortcut commands.
- Local approval server: `hermes-workflows serve-dashboard` exposes a small local form and POSTs into the canonical signal path.

## Hermes plugin target

A Hermes plugin should add profile-aware workflow approval operations without changing core:

- configure one or more workflow DBs per Hermes profile
- list waiting approvals
- send approval cards to Discord/Telegram/Home
- capture human/channel/message provenance automatically
- call the core approval signal path
- post a workflow receipt after resume
- optionally register/update a Workspaces/Artifact dashboard Thing

Proposed tool names:

```text
workflows_list_approvals(db?: string)
workflows_get_status(workflow_id: string, db?: string)
workflows_approve(workflow_id, key, action, human_id, channel, provenance)
workflows_render_dashboard(db, slug?)
```

The plugin should be a thin adapter over `hermes_workflows`. It should not own replay, validation, or workflow execution.

## Other agent/runtime adapters

The same contract should support:

- a LangGraph node that pauses on `ApprovalRequested`
- a Temporal activity that records the decision signal
- a custom web app that posts approval decisions
- a Claude/Codex/OpenCode agent wrapper that waits for human approval before executing high-blast-radius steps

If an adapter cannot provide durable provenance, it should be allowed to show status but not approve.

---
name: hermes-workflows-creating
description: "Design and author Hermes Workflows with typed inputs/outputs, agent/bash/ask/parallel/pipeline/goal primitives, Review Queue gates, artifacts, and verification receipts. Use when creating or modifying workflow definitions."
---

# Hermes Workflows — creating workflows

Use this when designing or implementing a workflow definition. This skill is generic authoring guidance, not a place for project-specific workflow shapes.

## Product model

Hermes Workflows are ordinary Python orchestration code with durable execution state.

Use the public authoring primitives:

- `@workflow` for the durable workflow entrypoint.
- `agent(...)` for judgment-heavy or generative work.
- `bash(...)` for deterministic local commands and receipts.
- `ask(...)` for typed review input and external-action approval gates.
- `parallel(...)` for fan-out/fan-in.
- `pipeline(...)` for staged transformations over items.
- `goal(...)` for loop-until-done semantics when available/appropriate.

Do not expose runtime plumbing (`ctx.handoff`, raw signals, internal waits, leases, outbox commands) in normal workflow authoring unless debugging the runtime itself.

## Authoring checklist

1. Define the purpose and side-effect boundary.
2. Define typed dataclass inputs and outputs.
3. Split steps by ownership:
   - agent judgment/synthesis/editing → `agent(...)`
   - deterministic command/check/evidence → `bash(...)`
   - human decision or external side-effect authorization → `ask(...)`
4. Make artifacts reviewable inline in the Review Queue; paths alone are not enough.
5. Add explicit gates before external effects: send, publish, schedule, commit/push/PR/merge, deploy, payment, credentials, destructive data changes.
6. Record receipts: commands run, stdout/stderr/exit code, artifact paths, external handles, side-effect ledger.
7. Add smoke tests that run without provider credentials by using `mock_output` where appropriate.

## Canonical typed skeleton

This is the primary copyable workflow. Keep the workflow input, agent output, review response, and workflow result typed end to end; do not normalize a loose dictionary inside the workflow body.

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from hermes_workflows import agent, ask, workflow


@dataclass(frozen=True)
class ReleaseNoteInput:
    change: str


@dataclass(frozen=True)
class Draft:
    text: str


@dataclass(frozen=True)
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: Optional[str] = None


@dataclass(frozen=True)
class SideEffects:
    published: bool = False


@dataclass(frozen=True)
class ReleaseNoteResult:
    draft: Draft
    decision: ReviewDecision
    side_effects: SideEffects


@workflow
async def release_note_workflow(inputs: ReleaseNoteInput) -> ReleaseNoteResult:
    draft = await agent(
        "writer",
        prompt="Draft a release note for the supplied change.",
        input=inputs,
        returns=Draft,
        # The canonical quickstart must reach typed review without credentials.
        mock_output={"text": f"Release note: {inputs.change}"},
    )
    decision = await ask(
        "Review this release note.",
        key="review_release_note",
        input=draft,
        returns=ReviewDecision,
    )
    return ReleaseNoteResult(
        draft=draft,
        decision=decision,
        side_effects=SideEffects(),
    )


if __name__ == "__main__":
    raise SystemExit(release_note_workflow.run())  # type: ignore[attr-defined]
```

Serialized dictionary input is coerced at the framework boundary. A loose `dict` workflow signature is compatibility-only and must not replace this typed standard in generated or documented workflows.

## Step design rules

- Prefer small named steps with stable keys; keys should describe the public review/action, not internal plumbing.
- Keep workflow bodies intention-level. Hide retry loops, per-item keys, and feedback routing in helper functions when they get noisy.
- Rejections must roundtrip into workflow logic. If a human says `request_changes`, feed the feedback into a targeted regeneration step.
- For fan-out review, use `ask(...)` inside `parallel(...)` only when each review is independent and can emit its own Review Queue request.
- For code workflows, distinguish plan approval, implementation/review approval, and landing approval. One approval does not authorize all later side effects.
- For generated files, return paths plus summaries/checks/hashes where useful.

## Testing pattern

At minimum:

```bash
PYTHONPATH=src:. python -m compileall -q path/to/workflow.py
PYTHONPATH=src:. python -m pytest -q tests/test_your_workflow.py
```

Test:

- first wait/review key appears;
- typed dataclass outputs rehydrate correctly;
- approval/request-changes paths behave as expected;
- side-effect flags remain false before explicit final gates;
- mock-output path runs without provider credentials;
- deterministic receipts are preserved in status/artifacts.

## Placement guidance

- Runtime/package examples can live in the Hermes Workflows repo.
- Normal user/team workflows should live in the owning workspace or a separate private workflows repo and be exposed through that workspace’s workflow registry/catalog.
- Do not bake private business/blog/event workflows into the runtime package unless explicitly making a public example.

## Do not put here

- One-off content/event/email/coding workflow designs for a specific user or demo.
- Personal infrastructure preferences.
- Repo-specific launch history.
- Runtime implementation details better suited to package-development docs.

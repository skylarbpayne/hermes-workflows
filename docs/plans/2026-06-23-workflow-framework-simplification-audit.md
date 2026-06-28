# Hermes Workflows Simplification Audit and Migration Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan one PR at a time. Do not big-bang this; the current problem is already too much framework surface too early.

**Goal:** Simplify Hermes Workflows into a small, coherent framework where workflow authors write typed business flow, while the framework owns input parsing, review requests, artifacts, prompt templates, agent workspace execution, and UI rendering boundaries.

**Architecture:** Keep the public authoring surface tiny: `@workflow`, `@step`, `agent`, `ask`, `bash`, typed inputs, artifacts, prompt files. Move identity, formatting, JSON parsing, and rendering out of workflow implementations. Preserve compatibility by adding read-time adapters/aliases, but make the new path clean and boring.

**Current verdict:** Skylar's critique is correct. We abstracted the wrong things and left the ugly stuff in user workflows. `approver`/agent identity is dumb complexity, `PUBLIC_APPROVAL_GATES` is premature topology theater, workflow-local `from_value` parsing is the framework failing to do its job, and the big workflows are trying to be demos, product specs, renderers, agents, policy engines, and side-effect ledgers at once.

---

## Audit scope and evidence

Audited:

- `src/hermes_workflows/**/*.py`
- `src/hermes_workflows/examples/**/*.py`
- `examples/**/*.py`
- `plugins/hermes-workflows-approvals/dashboard/**`
- `docs/architecture/**`, `docs/integrations/**`
- API-encoding tests under `tests/`

Created Kanban execution card: `t_67b23d5d` — `Audit Hermes Workflows implementation simplification`.

---

## Design principles for the reset

1. **Small public surface first.** Starter workflows should teach `workflow`, `step`, `agent`, `ask`, `Artifact`, and typed input. Everything else is advanced.
2. **No actor identities in core.** Core does not know about `human:skylar`, `agent:reviewer`, `approver`, `assignee`, or `decision.by` matching. That belongs to gateway/plugin policy if it belongs anywhere.
3. **Framework parses inputs.** Workflows declare input types; raw JSON becomes that type before workflow code runs.
4. **Artifacts are first-class.** A workflow returns/reviews artifacts, not arbitrary dicts with magic `render` strings.
5. **Prompt bodies live in files.** Workflow code wires prompts; it should not contain giant Markdown f-strings.
6. **Formatting is a renderer concern.** Workflow code should produce structured data/artifacts; UI/API renderers decide presentation.
7. **Workspace is an agent execution concern.** Agents need `workspace_dir` / worktree support as first-class request fields, not paths smuggled inside prompts.
8. **`ctx` is an escape hatch.** Normal authoring should use context-managed helpers; explicit `ctx` should be advanced/internal.

---

## Findings

### P0 — Remove `approver` / agent identity concepts from core

Current identity concepts are everywhere:

- `src/hermes_workflows/approvals.py` exposes `ApprovalView.approver`, `authority`, and `ApprovalDecision.by`.
- `src/hermes_workflows/authoring.py` has `ask(..., approver=...)` and `approve(..., approver=..., authority=...)`.
- `src/hermes_workflows/engine.py` validates `human:<id>` and `decision.by` matching in `_validate_operator_source`.
- Dashboard plugin/API surfaces `approver`, `allowed`, `authority` and the frontend renders `approver: ...` pills.
- Examples hard-code identities: `human:skylar`, `human:operator`, `agent:email_quality_reviewer`.

**Why this is bad:** core workflow state should not encode social identity. It should encode: there is a pending review/action, these are the allowed responses, here is the artifact/context, here is policy/provenance metadata if an adapter wants it.

**Target model:**

```python
@dataclass(frozen=True)
class ReviewRequest:
    key: str
    prompt: str
    artifact: Artifact | None = None
    actions: tuple[str, ...] = ("approve", "request_changes")
    response_type: type | None = None
    policy: JsonObject = field(default_factory=dict)

@dataclass(frozen=True)
class ReviewResponse:
    action: str
    payload: JsonObject = field(default_factory=dict)
    provenance: JsonObject = field(default_factory=dict)  # adapter-owned, not core identity
```

**Implementation rule:** `approver`, `by`, `authority`, `allowed` become legacy adapter/read-time compatibility fields. New workflow code uses `ask(..., actions=..., policy=...)`.

---

### P0 — Delete static upfront gate lists like `PUBLIC_APPROVAL_GATES`

Direct offender:

- `src/hermes_workflows/workflows/coding.py:PUBLIC_APPROVAL_GATES`
- Used in generated plan/review packets and tests as fixed topology.

**Why this is bad:** it makes a workflow pretend it knows all review topology before runtime. Worse, it labels an implementation agent as an approval gate.

**Target:** Review/gate state is derived from actual runtime events:

```python
status.review_requests  # active/completed review requests emitted by ask()
status.steps            # actual executed step state
```

No workflow should export a static list of public gates. If UI wants a map, it derives it from executed/pending requests and step metadata.

---

### P0 — Framework-owned raw → typed input parsing

Current offenders:

- `src/hermes_workflows/examples/reviewable_draft.py` defines `ReviewableDraftInput.from_value()` and calls it in the workflow.
- `src/hermes_workflows/examples/email_triage.py` uses `__workflow_input_sanitizer__`.
- `engine.py` invokes workflow functions with raw dicts unless the workflow manually parses.

**Target API:**

```python
@dataclass(frozen=True)
class BlogWorkflowInput:
    topic: Annotated[str, "Topic or thesis for the workflow"]
    audience: Annotated[str, "Primary reader/user segment"]
    draft_path: Annotated[str | None, "Optional local draft path"] = None

@workflow
async def blog_workflow(inputs: BlogWorkflowInput):
    ...
```

The engine should:

1. Inspect the workflow function input annotation.
2. Parse raw JSON into that type.
3. Fail before workflow execution with a useful validation error when inputs are invalid.
4. Preserve legacy dict behavior only for unannotated workflows.

No workflow-authored `from_value` methods for normal input parsing.

---

### P1 — Add common JSON types and one serializer boundary

Current problem: core uses `Any`, `dict[str, Any]`, and multiple `_jsonable` / `_to_jsonable` helpers across `authoring.py`, `prompts.py`, and `engine.py`.

Add `src/hermes_workflows/types.py`:

```python
JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

def to_json_value(value: object) -> JsonValue: ...
def to_json_object(value: object) -> JsonObject: ...
```

Then migrate persisted/event/API payloads toward `JsonValue`/`JsonObject` instead of `Any`.

---

### P1 — Add field descriptions for generation/UI

Current schema generation has field names/kinds/options but not enough descriptions for useful generation.

Support both:

```python
@dataclass(frozen=True)
class EventInput:
    title: Annotated[str, "Public event title"]
    expected_attendees: int = field(
        metadata={"description": "Expected attendee count used for venue and promotion planning"}
    )
```

Generated schema field should include:

```json
{
  "name": "expected_attendees",
  "kind": "int",
  "required": true,
  "description": "Expected attendee count used for venue and promotion planning"
}
```

---

### P1 — Introduce base `Artifact` and renderable derived types

Current artifacts are magic dicts/strings inferred by dashboard plugin heuristics.

Add `src/hermes_workflows/artifacts.py`:

```python
@dataclass(frozen=True)
class ArtifactMetadata:
    title: str
    description: str | None = None
    tags: tuple[str, ...] = ()
    source: JsonObject = field(default_factory=dict)

@dataclass(frozen=True)
class ArtifactRender:
    mode: Literal[
        "inline-json",
        "inline-text",
        "inline-markdown",
        "python-source",
        "media-reference",
        "file-reference",
        "external-link",
        "none",
    ]
    media_type: str | None = None
    reference: JsonObject = field(default_factory=dict)

@dataclass(frozen=True)
class Artifact:
    id: str
    kind: str
    metadata: ArtifactMetadata
    value: JsonValue
    render: ArtifactRender
    sha256: str | None = None
```

Convenience constructors/classes:

```python
MarkdownArtifact(title: str, markdown: str, **metadata)
TextArtifact(title: str, text: str, **metadata)
JsonArtifact(title: str, data: JsonValue, **metadata)
FileArtifact(title: str, path: str, media_type: str | None = None, **metadata)
LinkArtifact(title: str, url: str, media_type: str | None = None, **metadata)
PythonSourceArtifact(title: str, source: str, symbol: str | None = None, **metadata)
```

Legacy dict artifacts normalize read-time into `JsonArtifact` so old runs still render.

---

### P1 — Load prompt templates from files

Good existing pattern:

- `examples/repo_pr_workflow.py` already loads `examples/prompts/repo_change_plan.md`, records path and hashes.

But the framework API is still string-first:

- `src/hermes_workflows/prompts.py::render_prompt(template: str, variables: dict[str, Any])`

Target:

```python
plan_prompt = prompt_file("prompts/coding_plan.md")
rendered = plan_prompt.render(inputs=inputs, repo=repo_summary)

await agent("plan", prompt=rendered, workspace_dir=inputs.repo_path)
```

`RenderedPrompt` should preserve:

- template path
- template sha256
- variables sha256
- rendered sha256
- rendered text, unless redaction policy suppresses it

---

### P1 — Separate formatting/rendering boundaries

Current formatting is scattered across:

- workflow bodies building Markdown packets
- `engine.py` building review/input surfaces
- dashboard plugin inferring artifact descriptors
- frontend JS guessing which keys are noisy or useful

Target boundaries:

- `artifacts.py`: artifact normalization and render descriptors
- `api_views.py`: redacted API view models for status/review/run artifacts
- plugin API: route orchestration only
- frontend: render declared descriptors, not heuristics
- workflow code: structured outputs/artifacts only

---

### P1 — Agent `workspace_dir` / worktree support

Current pattern in coding workflows: put `worktree_path` in agent input/prompt and set `isolation="none"`. That's wrong.

Target:

```python
await agent(
    "implement",
    prompt=prompt_file("prompts/implement.md").render(plan=plan),
    input=plan,
    workspace_dir=worktree.path,
    isolation="worktree",
)
```

Runner request should carry `workspace_dir` explicitly. `SubprocessAgentRunner` should use it as `cwd` after path validation. A helper can create/cleanup git worktrees, but per-call `workspace_dir` is the minimum useful primitive.

---

### P1 — Make explicit `ctx` advanced-only

Current framework already has `current_context()` in `authoring.py`, but examples still teach explicit `ctx` and direct `ctx.approval.request`.

Target normal workflow:

```python
@step
async def inspect_repo(inputs: RepoInput) -> RepoSummary:
    ...

@workflow
async def repo_workflow(inputs: RepoInput):
    summary = await inspect_repo(inputs)
    decision = await ask("Review summary", input=MarkdownArtifact(...))
    return decision
```

Advanced-only escape hatch:

```python
ctx = current_context()
```

Implementation should support no-ctx `@step` bodies and one-input workflow functions first, with legacy ctx signatures retained temporarily.

---

## Workflow implementation audit

| File | Problem | Move to framework |
|---|---|---|
| `examples/content_asset_lane.py` | 316-line workflow body; inline prompts; repeated review gates; asset rendering/mock logic | pipeline/lane primitive, prompt files, artifact adapters, side-effect ledger |
| `examples/coding_review_demo.py` | manual worktree setup; agent paths in prompts/input; git evidence and PR packets in workflow | `workspace_dir`, git worktree helper, validation evidence artifact, PR artifact |
| `examples/event_planning_demo.py` | timeline, artifacts, external action policy, render packets all inline | timeline/artifact fanout helpers, external-action ledger |
| `examples/repo_pr_workflow.py` | framework-level git/approval/provenance/rendering in example; hardcoded `human:skylar` | repo/PR helpers, prompt files, review API without identities |
| `src/hermes_workflows/workflows/coding.py` | `PUBLIC_APPROVAL_GATES`; `approver`; `implementer`; giant inline plan/review templates | runtime-derived reviews, workspace agents, prompt files, artifacts |
| `src/hermes_workflows/examples/email_triage.py` | sanitizer, redaction, approval identity, rendering, writeback ledger | typed input parser, policy/artifact primitives, remove approver |
| `src/hermes_workflows/examples/reviewable_draft.py` | workflow-local `from_value`; approver field | typed parser, neutral `ask` |
| `src/hermes_workflows/examples/trip.py` | direct `ctx.approval.request`; `approver`; `authority` | `ask`/`review` helper and policy metadata |

**Starter examples should be tiny:**

1. `examples/typed_review.py` — dataclass input + `ask` + `MarkdownArtifact`.
2. `examples/agent_workspace.py` — `agent(..., workspace_dir=...)`.
3. `examples/prompt_file.py` — `prompt_file(...).render(...)`.
4. `examples/bash_artifact.py` — shell command → typed artifact.

Everything else should be `advanced/`, `presentation/`, or private workflow-source material until the framework is stable.

---

## Migration plan

### PR 1 — Foundation types and field descriptions

**Objective:** Establish typed JSON and generation metadata without changing behavior.

**Files:**

- Create: `src/hermes_workflows/types.py`
- Modify: `src/hermes_workflows/authoring.py`
- Modify: `src/hermes_workflows/prompts.py`
- Modify: `src/hermes_workflows/engine.py`
- Test: `tests/test_json_types.py`
- Test: `tests/test_authoring_api.py`

**Work:**

1. Add `JsonValue`, `JsonObject`, `to_json_value`.
2. Replace duplicate JSON normalizers with shared helper.
3. Add field `description` extraction from `field(metadata={...})` and `Annotated`.
4. Keep existing schema shape backward-compatible.

**Acceptance:**

- Existing tests pass.
- New test proves descriptions appear in generated form/review schemas.
- No workflow implementation code changes yet.

---

### PR 2 — Framework-owned input parsing

**Objective:** Typed workflow functions receive typed inputs; workflows stop parsing raw JSON.

**Files:**

- Create: `src/hermes_workflows/inputs.py`
- Modify: `src/hermes_workflows/engine.py`
- Modify: `src/hermes_workflows/examples/reviewable_draft.py`
- Modify: `src/hermes_workflows/examples/email_triage.py`
- Test: `tests/test_workflow_input_parsing.py`

**Work:**

1. Implement `parse_workflow_input(raw, annotation)`.
2. Support dataclasses first; support `TypedDict` if cheap.
3. Engine invokes parser before workflow function.
4. Convert `reviewable_draft` away from `from_value`.
5. Replace `__workflow_input_sanitizer__` with parser-compatible defaults or explicit adapter hook.

**Acceptance:**

- Invalid input fails before workflow body runs.
- Legacy unannotated workflows still receive dicts.
- No normal example uses `from_value` for workflow input parsing.

---

### PR 3 — Neutral review model; kill approver identities in new APIs

**Objective:** New authoring path has no `approver`, no agent identity approver, and no `decision.by` matching in core.

**Files:**

- Modify: `src/hermes_workflows/approvals.py`
- Modify: `src/hermes_workflows/authoring.py`
- Modify: `src/hermes_workflows/engine.py`
- Modify: `src/hermes_workflows/cli.py`
- Modify: `src/hermes_workflows/hermes_plugin_approvals.py`
- Modify: `plugins/hermes-workflows-approvals/dashboard/plugin_api.py`
- Modify: `plugins/hermes-workflows-approvals/dashboard/dist/index.js`
- Test: `tests/test_approval_adapter_api.py`
- Test: `tests/test_approval_ergonomics.py`
- Test: `tests/test_hermes_plugin_approvals.py`

**Work:**

1. Introduce `ReviewRequest`/`ReviewResponse` DTOs.
2. `ask(..., actions=..., policy=...)` creates review requests without approver.
3. `approve(...)` becomes compatibility sugar over `ask`.
4. Remove approver display from dashboard cards.
5. Keep legacy `workflow_approval_decide` as adapter alias only.
6. Keep historical event read compatibility.

**Acceptance:**

- New examples/tests do not pass `approver`.
- No UI copy says `approver:`.
- Core no longer rejects responses because `decision.by` does not match `human:<id>`.

---

### PR 4 — Artifact model and render boundary

**Objective:** Review/run/dashboard payloads use typed artifacts with required metadata.

**Files:**

- Create: `src/hermes_workflows/artifacts.py`
- Create: `src/hermes_workflows/api_views.py`
- Modify: `src/hermes_workflows/authoring.py`
- Modify: `src/hermes_workflows/engine.py`
- Modify: `plugins/hermes-workflows-approvals/dashboard/plugin_api.py`
- Modify: `plugins/hermes-workflows-approvals/dashboard/dist/index.js`
- Test: `tests/test_artifacts.py`
- Test: `tests/test_dashboard_plugin.py`

**Work:**

1. Add base `Artifact` and derived constructors.
2. Normalize legacy string/dict artifacts read-time.
3. Move dashboard artifact descriptor logic into shared artifact module.
4. Dashboard renders declared `artifact.render`, not guessed shapes.

**Acceptance:**

- Artifact metadata includes at least id, kind, title, source/provenance metadata.
- Markdown/file/link/python-source artifacts render through the same descriptor path.
- Historical arbitrary artifacts still display as JSON artifacts.

---

### PR 5 — Prompt files as first-class templates

**Objective:** Large prompt bodies move out of Python workflow code.

**Files:**

- Modify: `src/hermes_workflows/prompts.py`
- Modify: `src/hermes_workflows/__init__.py`
- Add: `examples/prompts/*.md`
- Modify: `examples/repo_pr_workflow.py`
- Modify: `examples/content_asset_lane.py` or move to advanced/presentation
- Test: `tests/test_prompt_templates.py`
- Test: `tests/test_repo_pr_workflow.py`

**Work:**

1. Add `PromptTemplate`, `RenderedPrompt`, `prompt_file`.
2. Store path/hash metadata.
3. Migrate non-trivial inline prompts.
4. Keep inline strings for tiny prompts.

**Acceptance:**

- Repo PR workflow uses framework prompt template API, not hand-rolled file loading.
- Prompt hashes are preserved in evidence/receipts.
- Long Markdown prompt text is not embedded in workflow bodies.

---

### PR 6 — Agent workspace/worktree support and ctx ergonomics

**Objective:** Agents can run in worktrees/workspaces without prompt hacks; normal workflows avoid explicit `ctx`.

**Files:**

- Modify: `src/hermes_workflows/authoring.py`
- Modify: `src/hermes_workflows/prompts.py`
- Modify: `src/hermes_workflows/runners.py`
- Modify: `src/hermes_workflows/agent_runner.py`
- Modify: `src/hermes_workflows/decorators.py`
- Modify: `src/hermes_workflows/engine.py`
- Test: `tests/test_agent_runner.py`
- Test: `tests/test_authoring_api.py`
- Test: `tests/test_engine.py`

**Work:**

1. Add `workspace_dir` to `agent(...)` and runner request.
2. Runner uses `workspace_dir` as cwd after allowlist/path validation.
3. Add optional `worktree` helper later; do not overbuild in this PR.
4. Support `@step` and workflows without explicit `ctx`.
5. Keep `current_context()` / `current_step_context()` as advanced escape hatch.

**Acceptance:**

- Coding agent can execute in a temporary workspace dir in test.
- No simple example takes `ctx`.
- Existing explicit-ctx workflows still run.

---

### PR 7 — Prune/simplify examples and remove static gates

**Objective:** Public examples teach the small framework. Big workflows stop pretending to be starter surface.

**Files:**

- Modify: `examples/README.md`
- Move or demote: `examples/content_asset_lane.py`, `examples/coding_review_demo.py`, `examples/event_planning_demo.py`
- Modify: `src/hermes_workflows/workflows/coding.py`
- Modify: `tests/test_coding_workflow.py`
- Add: `examples/typed_review.py`, `examples/agent_workspace.py`, `examples/prompt_file.py`, `examples/artifact_review.py`

**Work:**

1. Delete `PUBLIC_APPROVAL_GATES`.
2. Derive review state from runtime events.
3. Remove `approver`/`implementer` inputs from public coding workflow.
4. Shrink starter examples.
5. Move large presentation/demo flows to `examples/advanced/` or docs/presentation; consider private repo for personal workflows.

**Acceptance:**

- Search for `PUBLIC_APPROVAL_GATES` returns no matches.
- Search for `human:skylar`, `agent:email_quality_reviewer`, and `approver=` in starter examples returns no matches.
- `examples/README.md` starts with four tiny examples, not the giant lanes.

---

## API shape after migration

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from hermes_workflows import (
    MarkdownArtifact,
    agent,
    ask,
    bash,
    prompt_file,
    step,
    workflow,
)

@dataclass(frozen=True)
class RepoInput:
    repo_path: Annotated[str, "Repository or worktree directory"]
    goal: Annotated[str, "Change or review goal"]
    checks: list[str] = field(
        default_factory=lambda: ["pytest -q"],
        metadata={"description": "Validation commands to run after implementation"},
    )

@dataclass(frozen=True)
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None

@step
async def inspect_repo(inputs: RepoInput) -> MarkdownArtifact:
    result = await bash("git status --short", cwd=inputs.repo_path)
    return MarkdownArtifact(
        title="Repository status",
        markdown=f"```text\n{result.stdout}\n```",
        source={"repo_path": inputs.repo_path},
    )

@workflow
async def repo_change(inputs: RepoInput):
    status = await inspect_repo(inputs)

    plan = await agent(
        "plan_change",
        prompt=prompt_file("prompts/plan_change.md").render(inputs=inputs, status=status),
        workspace_dir=inputs.repo_path,
        returns=MarkdownArtifact,
    )

    decision = await ask(
        "Review the plan",
        input=plan,
        returns=ReviewDecision,
        actions=("approve", "request_changes"),
        policy={"side_effect": "repo_write"},
    )

    if decision.action != "approve":
        return {"status": "changes_requested", "feedback": decision.feedback}

    return await agent(
        "implement_change",
        prompt=prompt_file("prompts/implement_change.md").render(plan=plan),
        workspace_dir=inputs.repo_path,
        isolation="worktree",
    )
```

What is absent on purpose:

- no `ctx` parameter
- no `approver`
- no `human:*` or `agent:*` identity strings
- no workflow-local JSON parsing
- no inline giant prompt strings
- no magic artifact render dicts
- no static gate list

---

## Immediate recommendation

Do **not** continue expanding content/code/event workflows right now. That is polishing the wrong layer.

Start with PR 1 and PR 2 only:

1. `JsonValue` + field descriptions.
2. Framework-owned typed input parsing.

Then stop and review the authoring API with one tiny example. If that feels right, proceed to review model and artifacts. If it does not, we adjust before more framework concrete sets.

---

## Verification plan

For each PR:

```bash
PYTHONPATH=src:. python -m pytest -q tests/test_authoring_api.py tests/test_engine.py
PYTHONPATH=src:. python -m pytest -q tests/test_hermes_plugin_approvals.py tests/test_dashboard_plugin.py
PYTHONPATH=src:. python -m pytest -q tests/test_launch_examples.py
PYTHONPATH=src:. python -m compileall -q src examples tests
```

Additional migration checks:

```bash
rg "PUBLIC_APPROVAL_GATES|human:skylar|agent:email_quality_reviewer|approver=|from_value\(" src examples tests
rg "render\": \"inline-|artifact_render|kind\": \"markdown\"" src examples plugins tests
rg "prompt=f\"\"\"|prompt=\"\"\"" src examples tests
```

The goal is not zero matches immediately; the goal is fewer matches per PR with legacy compatibility clearly quarantined.

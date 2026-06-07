---
layout: page
title: Workflow definition ergonomics, discovery, and uv script plan
---

# Workflow Definition Ergonomics Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after Skylar explicitly approves it.

**Goal:** Make workflow definition files author-owned and runtime-light: they define workflows, optionally expose a tiny guarded direct-run helper, and can be discovered without hand-maintained registry ceremony.

**Architecture:** Keep `WorkflowEngine`, registry aliases, workers, approvals, and dashboard behavior in runtime modules. Workflow definition modules should only import public authoring primitives (`workflow`, `step`, `AgentPrompt`, and a small `workflow_app`/`workflow.run()` helper if approved). Discovery reads trusted local source files, imports candidates in a controlled source-tree context, and produces registry entries or invocation candidates; it must not execute workflow code beyond import-time decorator registration.

**Tech Stack:** Python 3.9+, existing `hermes_workflows` package, pytest, stdlib `importlib`, `ast`, `pathlib`, optional PEP 723 inline metadata for `uv run` scripts.

---

## Approval boundary

This document is a pre-implementation plan. Do not implement it until Skylar approves this plan explicitly, e.g. `approve plan`.

Plan approval authorizes implementation and PR creation only. Merge/landing remains a separate gate.

## Current baseline

The repo currently has a clean public core:

```python
from hermes_workflows import step, workflow

@step
async def draft(ctx, inputs):
    return {"draft": inputs["goal"]}

@workflow
async def demo(ctx, inputs):
    draft_result = await draft(ctx, inputs)
    decision = await ctx.wait_for_signal("approval.decision", key="approve_demo")
    return {"draft": draft_result, "decision": decision}
```

But running or wiring that workflow currently pushes authors toward runtime ceremony elsewhere:

- `WorkflowEngine(db_path).run_until_idle(...)`
- `module:function` refs passed to CLI/registry
- `.hermes/workflows.registry.json` entries for aliases and DB binding
- invocation helpers living outside the definition file

That ceremony is correct for operators, but noisy in workflow definition files.

## Proposed user-facing shape

### 1. Workflow definition stays boring

A workflow file should mostly define steps and workflows:

```python
# workflows/repo_review.py
from hermes_workflows import AgentPrompt, step, workflow

@step
async def collect_diff(ctx, inputs):
    return ctx.shell(["git", "diff", "origin/main...HEAD"], cwd=inputs["repo_path"])

@workflow
async def repo_review(ctx, inputs):
    diff = await collect_diff(ctx, inputs)
    review = await AgentPrompt(
        "prompts/repo_review.md",
        repo_path=inputs["repo_path"],
        diff=diff,
    )(ctx)
    approval = await ctx.wait_for_signal("approval.decision", key="approve_review")
    return {"review": review, "approval": approval}
```

No `WorkflowEngine` import. No registry parsing. No command-line argument plumbing inside the workflow body.

### 2. Optional guarded direct-run helper

If authors want a file to be runnable directly, use one tiny public helper instead of engine plumbing:

```python
from hermes_workflows import workflow_app

app = workflow_app(repo_review, default_db=".hermes/workflows.sqlite")

if __name__ == "__main__":
    app.run()
```

CLI behavior from the file:

```bash
python workflows/repo_review.py \
  --id repo-review-2026-06-07 \
  --input-json '{"repo_path":"."}'
```

Expected behavior:

- imports the definition file;
- resolves the selected `@workflow` function;
- starts/runs until idle using `WorkflowEngine` internally;
- prints the same compact receipt shape as existing CLI paths;
- does not make workflow authors instantiate engines or know registry internals.

### 3. Directory discovery for trusted local definitions

Add a discovery API and CLI that can scan a directory for workflow definitions:

```python
from hermes_workflows.discovery import discover_workflows

for found in discover_workflows("workflows"):
    print(found.ref, found.title, found.path)
```

CLI:

```bash
python -m hermes_workflows discover workflows --format json
```

Example output:

```json
[
  {
    "name": "repo_review",
    "ref": "repo_review:repo_review",
    "path": "/repo/workflows/repo_review.py",
    "title": "Repo Review",
    "description": "Review a branch and wait for approval.",
    "tags": ["repo", "review"]
  }
]
```

Discovery is for trusted local source trees. It is not a sandbox and should say so loudly.

### 4. Optional registry generation/import

Discovery should be able to produce registry payloads without hand-written JSON:

```bash
python -m hermes_workflows discover workflows \
  --db default=.hermes/workflows.sqlite \
  --write-registry .hermes/workflows.registry.json
```

Generated registry shape stays compatible with existing `WorkflowRegistry`:

```json
{
  "dbs": {
    "default": {"path": ".hermes/workflows.sqlite"}
  },
  "workflows": {
    "repo_review": {
      "workflow_ref": "repo_review:repo_review",
      "db": "default",
      "title": "Repo Review",
      "description": "Review a branch and wait for approval.",
      "tags": ["repo", "review"],
      "trusted_resume": false
    }
  }
}
```

### 5. Self-contained `uv` script option

Support workflow files that can be run with `uv run` using PEP 723 inline dependencies:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "hermes-workflows @ git+https://github.com/skylarbpayne/hermes-workflows.git",
# ]
# ///

from hermes_workflows import step, workflow, workflow_app

@step
async def hello(ctx, inputs):
    return f"hello {inputs['name']}"

@workflow
async def hello_workflow(ctx, inputs):
    return await hello(ctx, inputs)

app = workflow_app(hello_workflow, default_db=".hermes/hello.sqlite")

if __name__ == "__main__":
    app.run()
```

Run:

```bash
uv run --script workflows/hello.py --id hello-1 --input-json '{"name":"Skylar"}'
```

This should be documented as an ergonomic packaging path, not as a replacement for operator-owned registries in production.

## Proposed implementation slices

### Slice A: `workflow_app` direct-run helper

**Files:**

- Create: `src/hermes_workflows/app.py`
- Modify: `src/hermes_workflows/__init__.py`
- Modify: `src/hermes_workflows/__main__.py` or `src/hermes_workflows/cli.py` only if shared receipt formatting is needed
- Test: `tests/test_workflow_app.py`
- Docs: `docs/architecture/domain-model-and-seams.md` and/or new `docs/workflow-authoring.md`

**Behavior:**

```python
app = workflow_app(my_workflow, default_db=".hermes/workflows.sqlite")
app.run(argv=["--id", "wf_1", "--input-json", '{"x":1}'])
```

Expected assertions:

- calls `WorkflowEngine(default_db).run_until_idle(my_workflow, {"x": 1}, workflow_id="wf_1", workflow_ref="module:my_workflow")`;
- returns/prints a compact JSON receipt;
- exits non-zero with a useful error on missing `--id`, bad JSON, or non-`@workflow` object;
- never requires workflow definition code to import `WorkflowEngine`.

### Slice B: discovery model and scanner

**Files:**

- Create: `src/hermes_workflows/discovery.py`
- Modify: `src/hermes_workflows/cli.py`
- Test: `tests/test_discovery.py`
- Docs: `docs/workflow-authoring.md`

**Public types:**

```python
@dataclass(frozen=True)
class DiscoveredWorkflow:
    name: str
    ref: str
    path: str
    module_name: str
    function_name: str
    title: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
```

**Scanner rules:**

- input is a trusted local directory or file;
- default glob is `**/*.py`, excluding `.venv`, `__pycache__`, `.git`, `generated_workflows`, and hidden directories;
- import each candidate under a deterministic temporary module name derived from path hash;
- inspect for callables with `__workflow_name__`;
- restore `sys.path`/`sys.modules` changes after each import where possible;
- surface import errors as structured diagnostics unless `--strict` is set;
- do not instantiate `WorkflowEngine` or run workflow functions during discovery.

### Slice C: registry generation from discovery

**Files:**

- Modify: `src/hermes_workflows/discovery.py`
- Modify: `src/hermes_workflows/registry.py` only if a helper serializer belongs there
- Modify: `src/hermes_workflows/cli.py`
- Test: `tests/test_discovery_registry.py`

**Behavior:**

```bash
python -m hermes_workflows discover workflows \
  --db default=.hermes/workflows.sqlite \
  --write-registry .hermes/workflows.registry.json
```

Assertions:

- preserves existing manually configured DB aliases unless `--replace` is passed;
- refuses to overwrite existing workflow aliases with different refs unless `--replace` is passed;
- emits deterministic sorted JSON;
- generated JSON loads with `WorkflowRegistry.from_sources(config_path=...)`.

### Slice D: uv script documentation and smoke examples

**Files:**

- Create: `docs/workflow-authoring.md`
- Create: `examples/uv_script_workflow.py`
- Test: `tests/test_uv_script_example.py` or a lightweight static smoke

**Behavior:**

- document PEP 723 inline metadata;
- include one source-tree example and one git dependency example;
- smoke with `uv run --script examples/uv_script_workflow.py --id uv-script-smoke --input-json '{"name":"Hermes"}'` when network/dependency constraints allow;
- when network is unavailable, run a static test that verifies the script metadata, direct-run guard, and public imports.

## TDD sequence

1. Write `tests/test_workflow_app.py::test_workflow_app_runs_until_idle_without_engine_in_definition`.
2. Verify it fails because `workflow_app` does not exist.
3. Implement minimal `workflow_app` and `WorkflowApp.run`.
4. Run `pytest tests/test_workflow_app.py -q`.
5. Add bad-input tests for missing id and invalid JSON.
6. Run `pytest tests/test_workflow_app.py -q`.
7. Write `tests/test_discovery.py::test_discovers_workflow_functions_in_directory`.
8. Verify failure.
9. Implement `discover_workflows` without registry writes.
10. Run `pytest tests/test_discovery.py -q`.
11. Add discovery import-error diagnostics and hidden-dir exclusions.
12. Run `pytest tests/test_discovery.py -q`.
13. Write registry-generation tests.
14. Implement registry generation.
15. Run `pytest tests/test_discovery_registry.py -q`.
16. Add docs and uv script example.
17. Run targeted docs/static tests.
18. Run full suite: `pytest -q`, `python -m compileall -q src tests examples`, `git diff --check`.

## Non-goals

- Do not remove or weaken `WorkflowRegistry`; operators still need explicit aliases, DB policy, and trusted resume settings.
- Do not make discovery scan untrusted arbitrary code. Importing Python files is code execution.
- Do not invent YAML/JSON-only workflow definitions.
- Do not hide approval provenance or plan/merge gates inside the direct-run helper.
- Do not implement workflow versioning/determinism guards in this slice.
- Do not turn `uv` scripts into the production deployment model; they are an authoring/distribution convenience.

## Risks and safeguards

| Risk | Safeguard |
| --- | --- |
| Discovery imports execute top-level code | Document trusted-only boundary, add `--strict`, keep examples boring, do not run discovered workflows. |
| Direct-run helper becomes a second CLI | Keep it tiny and delegate to existing `WorkflowEngine`/receipt helpers. |
| Registry generation overwrites operator config | Default to fail on conflicts; require `--replace` for overwrites. |
| uv script examples depend on network | Keep source-tree smoke separate from git dependency docs. |
| Workflow definitions regain runtime ceremony | Add tests/docs that examples import `workflow_app`, not `WorkflowEngine`. |

## Acceptance criteria

Implementation is acceptable when:

- workflow definition examples contain no `WorkflowEngine` import;
- direct-run helper works with a real temporary SQLite DB;
- discovery finds workflows in a fixture directory and returns stable refs;
- discovery does not create or mutate workflow DBs;
- registry generation output round-trips through `WorkflowRegistry`;
- uv script documentation includes copy-pasteable inline dependency examples;
- full test suite and compile smoke pass;
- PR landing packet includes plan approval provenance and states that merge is separate.

## Recommended landing order

1. Land docs UX fix separately first: Mermaid rendering and docs navigation.
2. Ask Skylar to approve this plan.
3. Implement `workflow_app` as the smallest API slice.
4. Implement discovery without registry writes.
5. Add registry generation.
6. Add uv script example/docs after the core ergonomics are stable.

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

from hermes_workflows import step, workflow


def _run(command: List[str] | str, *, cwd: Path, timeout: int = 300, shell: bool = False) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return {
        "command": command if isinstance(command, str) else " ".join(command),
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "output": completed.stdout.strip(),
    }


def _git(args: List[str], *, cwd: Path) -> Dict[str, Any]:
    return _run(["git", *args], cwd=cwd)


def _artifact_path(inputs: Dict[str, Any], key: str, repo: Path, default_name: str) -> Path:
    explicit = inputs.get(key)
    if explicit:
        return Path(str(explicit)).expanduser().resolve()
    return (repo / ".hermes" / "coding-task" / default_name).resolve()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@step
async def inspect_coding_repo(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repo: {repo}")
    if inputs.get("context_path"):
        context_path = _artifact_path(inputs, "context_path", repo, "repo-context.md")
    elif inputs.get("plan_path"):
        context_path = Path(str(inputs["plan_path"])).expanduser().resolve().with_name("repo-context.md")
    else:
        context_path = _artifact_path(inputs, "context_path", repo, "repo-context.md")
    branch = _git(["branch", "--show-current"], cwd=repo)
    head = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    status = _git(["status", "--short"], cwd=repo)
    changed_files = _git(["diff", "--name-only", "HEAD"], cwd=repo)
    diff_stat = _git(["diff", "--stat", "HEAD"], cwd=repo)
    recent_commits = _git(["log", "--oneline", "-5"], cwd=repo)
    content = f"""# Coding task repo context

## Task

{inputs['task']}

## Repository

- path: `{repo}`
- branch: `{branch['output']}`
- head: `{head['output']}`
- clean: `{status['output'] == ''}`

## Git status

```text
{status['output'] or '(clean)'}
```

## Changed files

```text
{changed_files['output'] or '(none)'}
```

## Diff stat

```text
{diff_stat['output'] or '(none)'}
```

## Recent commits

```text
{recent_commits['output']}
```
"""
    _write(context_path, content)
    return {
        "kind": "coding_task_repo_context",
        "artifact_path": str(context_path),
        "repo_path": str(repo),
        "task": inputs["task"],
        "branch": branch["output"],
        "head": head["output"],
        "status_short": status["output"],
        "clean": status["output"] == "",
        "changed_files": changed_files["output"],
        "diff_stat": diff_stat["output"],
    }


@step
async def write_coding_plan(ctx, inputs: Dict[str, Any], repo_context: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(repo_context["repo_path"])
    plan_path = _artifact_path(inputs, "plan_path", repo, "coding-plan.md")
    verification = inputs.get("verification_commands") or ["pytest -q"]
    acceptance = inputs.get("acceptance_checks") or [
        "Targeted tests fail before implementation and pass after implementation.",
        "Full relevant verification passes before PR or merge.",
        "The review packet names changed files, checks, and remaining risk.",
        "The output saves operator time compared with ad-hoc repo spelunking.",
    ]
    content = f"""# Coding task plan

## Task

{inputs['task']}

## Repository state

- repo: `{repo_context['repo_path']}`
- branch: `{repo_context['branch']}`
- head: `{repo_context['head']}`
- clean before implementation: `{repo_context['clean']}`

## Proposed workflow

1. Inspect the relevant code and existing tests.
2. Write failing tests for the requested behavior.
3. Implement the smallest useful slice.
4. Run targeted verification, then broader verification.
5. Produce a review packet with artifacts and remaining risks.

## Acceptance checks

""" + "\n".join(f"- {item}" for item in acceptance) + f"""

## Verification commands

""" + "\n".join(f"- `{command}`" for command in verification) + """

## Artifacts this workflow will produce

- repo context artifact
- coding task plan artifact
- verification evidence artifact
- review packet artifact
"""
    _write(plan_path, content)
    return {
        "kind": "coding_task_plan",
        "artifact_path": str(plan_path),
        "task": inputs["task"],
        "verification_commands": verification,
    }


@step
async def collect_coding_evidence(ctx, inputs: Dict[str, Any], repo_context: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(repo_context["repo_path"])
    evidence_path = _artifact_path(inputs, "evidence_path", repo, "coding-evidence.md")
    commands = inputs.get("verification_commands") or ["pytest -q"]
    results = [_run(str(command), cwd=repo, shell=True, timeout=int(inputs.get("verification_timeout", 300))) for command in commands]
    status = _git(["status", "--short"], cwd=repo)
    diff_stat = _git(["diff", "--stat", "HEAD"], cwd=repo)
    changed_files = _git(["diff", "--name-only", "HEAD"], cwd=repo)
    ok = all(item["ok"] for item in results)
    content = f"""# Coding task evidence

## Task

{inputs['task']}

## Plan artifact

`{plan['artifact_path']}`

## Git status

Command: `git status --short`

```text
{status['output'] or '(clean)'}
```

## Changed files

```text
{changed_files['output'] or '(none)'}
```

## Diff stat

```text
{diff_stat['output'] or '(none)'}
```

## Verification

"""
    for result in results:
        content += f"### `{result['command']}`\n\n- ok: `{result['ok']}`\n- returncode: `{result['returncode']}`\n\n```text\n{result['output']}\n```\n\n"
    _write(evidence_path, content)
    return {
        "kind": "coding_task_evidence",
        "artifact_path": str(evidence_path),
        "ok": ok,
        "results": results,
        "status_short": status["output"],
        "changed_files": changed_files["output"],
        "diff_stat": diff_stat["output"],
    }


@step
async def write_coding_review_packet(
    ctx,
    inputs: Dict[str, Any],
    repo_context: Dict[str, Any],
    plan: Dict[str, Any],
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    repo = Path(repo_context["repo_path"])
    review_path = _artifact_path(inputs, "review_packet_path", repo, "coding-review.md")
    ready_for_pr = bool(evidence["ok"] and not evidence["changed_files"])
    content = f"""# Coding task review packet

## Task

{inputs['task']}

## Result

ready_for_pr: {str(ready_for_pr).lower()}

## Artifacts

- plan: `{plan['artifact_path']}`
- evidence: `{evidence['artifact_path']}`

## Changed files

```text
{evidence['changed_files'] or '(none)'}
```

## Verification summary

- all checks passed: `{evidence['ok']}`

## Time-saving check

This packet should save operator time by collecting repo state, plan, verification output, changed files, and review risk in one place instead of forcing manual reconstruction from shell history.
"""
    _write(review_path, content)
    return {
        "kind": "coding_task_review_packet",
        "artifact_path": str(review_path),
        "ready_for_pr": ready_for_pr,
        "task": inputs["task"],
    }


@workflow
async def coding_task_workflow(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo_context = await inspect_coding_repo(ctx, inputs)
    plan = await write_coding_plan(ctx, inputs, repo_context)
    evidence = await collect_coding_evidence(ctx, inputs, repo_context, plan)
    review_packet = await write_coding_review_packet(ctx, inputs, repo_context, plan, evidence)
    return {
        "kind": "coding_task_result",
        "workflow_id": ctx.workflow_id,
        "task": inputs["task"],
        "repo_path": repo_context["repo_path"],
        "ready_for_pr": review_packet["ready_for_pr"],
        "verification": {"ok": evidence["ok"], "results": evidence["results"]},
        "artifact_paths": {
            "plan": plan["artifact_path"],
            "evidence": evidence["artifact_path"],
            "review_packet": review_packet["artifact_path"],
        },
    }

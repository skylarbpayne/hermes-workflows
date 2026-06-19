from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, TypedDict, cast

from hermes_workflows import agent, step, workflow


PUBLIC_APPROVAL_GATES = ["approve_coding_plan", "implementation_agent", "approve_coding_review"]
INTERNAL_IMPLEMENTATION_AGENT_KEY = "coding_ready"


class CodingWorkflowInput(TypedDict, total=False):
    """Public input contract for the first-class approval-gated coding workflow."""

    repo_path: str
    goal: str
    task: str
    verification_commands: List[str]
    verification_timeout: int
    acceptance_checks: List[str]
    plan_path: str
    evidence_path: str
    review_packet_path: str
    approver: str
    implementer: str
    commit: bool
    push: bool
    commit_message: str
    before_after: Dict[str, str]
    implementation_steps: List[str]
    examples: List[Dict[str, str]]
    visuals: List[Dict[str, Any]]
    non_goals: List[str]
    rollback: List[str]


class CodingWorkflowResult(TypedDict, total=False):
    """Public output contract returned by coding_workflow."""

    kind: str
    ready: bool
    stage: str
    goal: str
    repo_path: str
    approval_gates: List[str]
    verification: Dict[str, Any]
    artifact_paths: dict[str, str]
    packet: Dict[str, Any]
    plan: Dict[str, Any]
    decision: Dict[str, Any]
    committed: bool
    pushed: bool
    reason: str
    commit_output: str
    push_output: str
    head: str
    artifact_path: str
    bytes: int


def _run_shell(command: str, *, cwd: Path, timeout: int = 300) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "output": completed.stdout.strip(),
    }


def _run_git(args: List[str], *, cwd: Path, check: bool = False, timeout: int = 300) -> Dict[str, Any]:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
        timeout=timeout,
    )
    return {
        "command": "git " + " ".join(args),
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "output": completed.stdout.strip(),
    }


def _artifact_path(inputs: Dict[str, Any], key: str, repo: Path, default_name: str) -> Path:
    explicit = inputs.get(key)
    if explicit:
        return Path(str(explicit)).expanduser().resolve()
    return (repo / ".hermes" / "coding-workflow" / default_name).resolve()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _format_approval_source(decision: Dict[str, Any]) -> str:
    source = decision.get("source") or {}
    provenance = source.get("message_url") or source.get("message_id") or source.get("event_id") or "unknown"
    return f"{source.get('channel', 'unknown')} {provenance}"


@step
async def coding_inspect_repo(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repo: {repo}")
    branch = _run_git(["branch", "--show-current"], cwd=repo)
    head = _run_git(["rev-parse", "--short", "HEAD"], cwd=repo)
    status = _run_git(["status", "--short"], cwd=repo)
    remote = _run_git(["remote", "-v"], cwd=repo)
    return {
        "kind": "coding_repo_context",
        "repo_path": str(repo),
        "branch": branch["output"],
        "head": head["output"],
        "remote": remote["output"],
        "status_short": status["output"],
        "clean": status["ok"] and status["output"] == "",
    }


@step
async def coding_write_plan(ctx, inputs: Dict[str, Any], repo: Dict[str, Any]) -> Dict[str, Any]:
    repo_path = Path(repo["repo_path"])
    plan_path = _artifact_path(inputs, "plan_path", repo_path, "coding-plan.md")
    goal = inputs.get("goal") or inputs.get("task") or "Coding task"
    verification = inputs.get("verification_commands") or ["pytest -q"]
    acceptance = inputs.get("acceptance_checks") or [
        "A targeted regression test exists for the requested behavior.",
        "Implementation does not begin until approve_coding_plan is approved by the human approver.",
        "The workflow records an implementation handoff before collecting diff/evidence.",
        "The review packet is approved before the workflow reports ready=true or commits/pushes.",
    ]
    before_after = inputs.get("before_after") or {
        "before": "The workflow is at repo inspection only: no source files have been modified by this run, and implementation is still gated on approve_coding_plan.",
        "after": f"A scoped change satisfies `{goal}`, evidence is collected, and approve_coding_review gates the final ready=true report.",
    }
    implementation_steps = inputs.get("implementation_steps") or [
        "Confirm the working tree still matches the repo context captured below.",
        "Make only the files needed for the stated goal; do not bundle opportunistic cleanup.",
        "Run the verification commands exactly as listed and keep their output for the evidence packet.",
        "Return a short implementation summary plus artifact/diff pointers for the evidence packet.",
    ]
    examples = inputs.get("examples") or [
        {
            "case": "approved path",
            "input": "approve_coding_plan = approve",
            "expected": "implementation handoff opens; evidence and review approval are required before completion",
        },
        {
            "case": "rejected path",
            "input": "approve_coding_plan = reject/edit/rerun",
            "expected": "workflow stops or loops without source changes from this workflow stage",
        },
    ]
    visuals = inputs.get("visuals") or [
        {
            "type": "flow",
            "title": "approval-gated coding flow",
            "nodes": [
                "inspect repo",
                "approve plan",
                "implement after approval",
                "collect diff + evidence",
                "approve review",
                "ready/land",
            ],
        }
    ]
    non_goals = inputs.get("non_goals") or [
        "Do not modify files outside the stated repo scope.",
        "Do not commit, push, publish, or schedule anything unless explicitly enabled in the workflow input and review is approved.",
        "Do not use the plan artifact as a diff; the diff belongs in the evidence packet after approval and implementation.",
    ]
    rollback = inputs.get("rollback") or [
        "Before implementation: reject the plan; no source change should exist from this workflow path.",
        "After implementation but before review approval: revert the working-tree changes shown in the evidence diff.",
        "Stop immediately if verification fails or the changed-file list exceeds the approved scope.",
    ]
    visual_nodes = []
    if visuals and isinstance(visuals[0], dict):
        raw_nodes = visuals[0].get("nodes") or []
        if isinstance(raw_nodes, list):
            visual_nodes = [str(node) for node in raw_nodes]
    visual_line = " → ".join(visual_nodes) if visual_nodes else "approval-gated coding flow"
    implementation_boundary = "No source files will be modified before this approval is recorded. Implementation happens only after approve_coding_plan; the workflow records an internal implementation handoff before evidence collection."
    completion_boundary = "Workflow completion requires approve_coding_review from the human approver after evidence is collected."
    content = f"""# Coding workflow plan

## Goal

{goal}

## Repository

- path: `{repo['repo_path']}`
- branch: `{repo['branch']}`
- baseline head: `{repo['head']}`
- clean before workflow: `{repo['clean']}`

## Before / after

Before: {before_after['before']}

After: {before_after['after']}

## Concrete implementation steps

""" + "\n".join(f"{index}. {item}" for index, item in enumerate(implementation_steps, start=1)) + f"""

## Examples / acceptance scenarios

""" + "\n".join(f"- {item['case']}: `{item['input']}` → {item['expected']}" for item in examples) + f"""

## Dashboard preview

- artifact render: inline markdown approval packet
- visual: {visual_line}
- approval buttons: Approve records human provenance and resumes; Reject records feedback without implementation.

## Non-goals

""" + "\n".join(f"- {item}" for item in non_goals) + f"""

## Rollback / stop conditions

""" + "\n".join(f"- {item}" for item in rollback) + f"""

## Approval gates

""" + "\n".join(f"- `{gate}`" for gate in PUBLIC_APPROVAL_GATES) + f"""

## Implementation boundary

{implementation_boundary}

## Completion boundary

{completion_boundary}

## Acceptance checks

""" + "\n".join(f"- {item}" for item in acceptance) + f"""

## Verification commands

""" + "\n".join(f"- `{command}`" for command in verification) + "\n"
    plan = {
        "kind": "markdown",
        "plan_kind": "coding_plan",
        "render": "inline-markdown",
        "summary": f"Approve a concrete coding plan for: {goal}",
        "goal": goal,
        "repo": repo,
        "verification_commands": verification,
        "acceptance_checks": acceptance,
        "approval_gates": PUBLIC_APPROVAL_GATES,
        "implementation_boundary": implementation_boundary,
        "completion_boundary": completion_boundary,
        "artifact_path": str(plan_path),
        "markdown": content,
        "sections": {
            "before_after": before_after,
            "implementation_steps": implementation_steps,
            "examples": examples,
            "visuals": visuals,
            "non_goals": non_goals,
            "rollback": rollback,
        },
    }
    _write(plan_path, content)
    return plan


@step
async def coding_collect_diff(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    status = _run_git(["status", "--short"], cwd=repo)
    return {
        "kind": "coding_diff",
        "status": status,
        "changed_files": status["output"],
        "diff_stat": _run_git(["diff", "--stat"], cwd=repo),
        "diff_name_only": _run_git(["diff", "--name-only"], cwd=repo),
        "diff_summary": _run_git(["diff", "--", ":!*.sqlite", ":!*.db"], cwd=repo),
    }


@step
async def coding_run_verification(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    commands = inputs.get("verification_commands") or ["pytest -q"]
    timeout = int(inputs.get("verification_timeout", 300))
    results = [_run_shell(str(command), cwd=repo, timeout=timeout) for command in commands]
    return {"kind": "coding_verification", "ok": all(result["ok"] for result in results), "results": results}


@step
async def coding_write_evidence(
    ctx,
    inputs: Dict[str, Any],
    plan: Dict[str, Any],
    implementation_signal: Dict[str, Any],
    diff: Dict[str, Any],
    verification: Dict[str, Any],
) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    evidence_path = _artifact_path(inputs, "evidence_path", repo, "coding-evidence.md")
    content = f"""# Coding workflow evidence

## Goal

{plan['goal']}

## Plan artifact

`{plan['artifact_path']}`

## Implementation signal

- by: `{implementation_signal.get('by', 'unknown')}`
- summary: {implementation_signal.get('summary', '(none)')}

## Git status

```text
{diff['changed_files'] or '(clean)'}
```

## Diff stat

```text
{diff['diff_stat']['output'] or '(none)'}
```

## Verification

"""
    for result in verification["results"]:
        content += f"### `{result['command']}`\n\n- ok: `{result['ok']}`\n- returncode: `{result['returncode']}`\n\n```text\n{result['output']}\n```\n\n"
    _write(evidence_path, content)
    return {
        "kind": "coding_evidence",
        "artifact_path": str(evidence_path),
        "ok": verification["ok"],
        "results": verification["results"],
        "changed_files": diff["changed_files"],
        "diff_stat": diff["diff_stat"]["output"],
    }


@step
async def coding_build_review_packet(
    ctx,
    inputs: Dict[str, Any],
    plan: Dict[str, Any],
    plan_decision: Dict[str, Any],
    implementation_signal: Dict[str, Any],
    diff: Dict[str, Any],
    verification: Dict[str, Any],
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    review_path = _artifact_path(inputs, "review_packet_path", repo, "coding-review.md")
    blockers = []
    if not verification["ok"]:
        blockers.append("verification failed")
    if not diff["changed_files"]:
        blockers.append("no working-tree changes detected")
    packet = {
        "kind": "coding_review_packet",
        "goal": plan["goal"],
        "plan": plan,
        "plan_decision": plan_decision,
        "implementation_signal": implementation_signal,
        "diff": diff,
        "verification": verification,
        "evidence": evidence,
        "blockers": blockers,
        "recommendation": "approve" if not blockers else "do_not_approve",
        "commit_message": inputs.get("commit_message") or f"feat: {plan['goal'][:60]}",
        "artifact_path": str(review_path),
    }
    verification_lines = []
    for result in verification["results"]:
        verification_lines.append(f"### `{result['command']}`\n\n```text\n{result['output']}\n```")
    content = f"""# Coding workflow review packet

Goal: {packet['goal']}
Recommendation: {packet['recommendation']}

Plan approved by: {plan_decision.get('by', 'unknown')}
Plan approval source: {_format_approval_source(plan_decision)}
Implementation signaled by: {implementation_signal.get('by', 'unknown')}
Review status: waiting on `approve_coding_review`

## Approval gates

{chr(10).join(f"- `{gate}`" for gate in PUBLIC_APPROVAL_GATES)}

## Verification

- Tests/checks pass: `{verification['ok']}`

{chr(10).join(verification_lines)}

## Changed files

```text
{diff['changed_files'] or '(none)'}
```

## Diff stat

```text
{diff['diff_stat']['output'] or '(none)'}
```

## Blockers

{chr(10).join(f'- {blocker}' for blocker in blockers) if blockers else '- none'}
"""
    _write(review_path, content)
    return packet


@step
async def coding_write_review_report(
    ctx,
    inputs: Dict[str, Any],
    review_decision: Dict[str, Any],
    packet: Dict[str, Any],
) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    review_path = _artifact_path(inputs, "review_packet_path", repo, "coding-review.md")
    plan = packet["plan"]
    verification_lines = []
    for result in packet["verification"]["results"]:
        verification_lines.append(f"### `{result['command']}`\n\n```text\n{result['output']}\n```")
    content = f"""# Coding workflow review packet

Goal: {packet['goal']}
Recommendation: {packet['recommendation']}

Plan approved by: {packet['plan_decision'].get('by', 'unknown')}
Plan approval source: {_format_approval_source(packet['plan_decision'])}
Implementation signaled by: {packet['implementation_signal'].get('by', 'unknown')}
Review approved by: {review_decision.get('by', 'unknown')}
Review approval source: {_format_approval_source(review_decision)}

## Approval gates

{chr(10).join(f"- `{gate}`" for gate in PUBLIC_APPROVAL_GATES)}

## Repository

- Path: `{plan['repo']['repo_path']}`
- Branch: `{plan['repo']['branch']}`
- Baseline HEAD: `{plan['repo']['head']}`

## Verification

- Tests/checks pass: `{packet['verification']['ok']}`

{chr(10).join(verification_lines)}

## Changed files

```text
{packet['diff']['changed_files'] or '(none)'}
```

## Diff stat

```text
{packet['diff']['diff_stat']['output'] or '(none)'}
```

## Blockers

{chr(10).join(f'- {blocker}' for blocker in packet['blockers']) if packet['blockers'] else '- none'}
"""
    _write(review_path, content)
    return {"kind": "coding_review_report", "artifact_path": str(review_path), "bytes": len(content.encode("utf-8"))}


@step
async def coding_land_change(ctx, inputs: Dict[str, Any], packet: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    if not inputs.get("commit", False):
        return {"committed": False, "pushed": False, "reason": "commit disabled", **report}

    _run_git(["add", "."], cwd=repo, check=True)
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Palmer")
    env.setdefault("GIT_AUTHOR_EMAIL", "palmer@local")
    env.setdefault("GIT_COMMITTER_NAME", "Palmer")
    env.setdefault("GIT_COMMITTER_EMAIL", "palmer@local")
    commit = subprocess.run(
        ["git", "commit", "-m", packet["commit_message"]],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    if commit.returncode != 0:
        raise RuntimeError(commit.stdout.strip())

    push_result = {"ok": False, "output": "push disabled"}
    if inputs.get("push", False):
        push_result = _run_git(["push"], cwd=repo)
        if not push_result["ok"]:
            raise RuntimeError(push_result["output"])
    head = _run_git(["rev-parse", "--short", "HEAD"], cwd=repo, check=True)
    return {
        "committed": True,
        "pushed": bool(inputs.get("push", False)),
        "commit_output": commit.stdout.strip(),
        "push_output": push_result["output"],
        "head": head["output"],
        **report,
    }


@workflow
async def coding_workflow(ctx, inputs: CodingWorkflowInput) -> CodingWorkflowResult:
    repo = await coding_inspect_repo(ctx, inputs)
    plan = await coding_write_plan(ctx, inputs, repo)
    plan_decision = await ctx.approve(
        f"Approve coding plan for: {plan['goal']}?",
        key="approve_coding_plan",
        artifact=plan,
        approver=inputs.get("approver", "human:skylar"),
        allowed=["approve", "reject", "edit", "rerun"],
    )
    if not plan_decision.approved:
        return {"ready": False, "stage": "plan_rejected", "plan": plan, "decision": plan_decision.to_dict()}

    implementation_signal = await agent(
        "implement_coding_plan",
        prompt=f"Implement approved coding plan for: {plan['goal']}.",
        input={"plan": plan},
        key=INTERNAL_IMPLEMENTATION_AGENT_KEY,
        tools=["terminal", "file"],
    )
    diff = await coding_collect_diff(ctx, inputs)
    verification = await coding_run_verification(ctx, inputs)
    evidence = await coding_write_evidence(ctx, inputs, plan, implementation_signal, diff, verification)
    packet = await coding_build_review_packet(ctx, inputs, plan, plan_decision, implementation_signal, diff, verification, evidence)
    review_decision = await ctx.approve(
        f"Approve coding review for: {plan['goal']}?",
        key="approve_coding_review",
        artifact=packet,
        approver=inputs.get("approver", "human:skylar"),
        allowed=["approve", "reject", "edit", "rerun"],
    )
    if not review_decision.approved:
        return {"ready": False, "stage": "review_rejected", "packet": packet, "decision": review_decision.to_dict()}
    report = await coding_write_review_report(ctx, inputs, review_decision, packet)
    landing = await coding_land_change(ctx, inputs, packet, report)
    return cast(CodingWorkflowResult, {
        "kind": "coding_workflow_result",
        "ready": True,
        "goal": plan["goal"],
        "repo_path": repo["repo_path"],
        "approval_gates": PUBLIC_APPROVAL_GATES,
        "verification": {"ok": verification["ok"], "results": verification["results"]},
        "artifact_paths": {
            "plan": plan["artifact_path"],
            "evidence": evidence["artifact_path"],
            "review_packet": report["artifact_path"],
        },
        "packet": packet,
        **landing,
    })

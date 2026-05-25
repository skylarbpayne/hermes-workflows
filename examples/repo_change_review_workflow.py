from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from hermes_workflows import step, workflow


def _run_shell(command: str, *, cwd: Path, timeout: int = 240) -> Dict[str, Any]:
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


def _run_git(args: List[str], *, cwd: Path, check: bool = False) -> Dict[str, Any]:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )
    return {
        "command": "git " + " ".join(args),
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "output": completed.stdout.strip(),
    }


@step
async def inspect_change_repo(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repo: {repo}")
    status = _run_git(["status", "--short"], cwd=repo)
    branch = _run_git(["branch", "--show-current"], cwd=repo)
    head = _run_git(["rev-parse", "--short", "HEAD"], cwd=repo)
    remote = _run_git(["remote", "-v"], cwd=repo)
    return {
        "repo_path": str(repo),
        "branch": branch["output"],
        "head": head["output"],
        "remote": remote["output"],
        "status_short": status["output"],
        "clean": status["ok"] and status["output"] == "",
    }


@step
async def draft_change_plan(ctx, repo: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    goal = inputs["goal"]
    return {
        "goal": goal,
        "repo": repo,
        "verification_commands": inputs.get("verification_commands") or ["pytest -q"],
        "approval_gates": ["approve_change_plan", "approve_change_landing"],
        "implementation_boundary": "External/manual implementation happens after plan approval and before implementation.ready signal.",
        "risk_notes": [
            "Workflow does not bypass human landing approval.",
            "Commit/push are optional and require approve_change_landing.",
            "Verification output is captured before landing.",
        ],
    }


@step
async def collect_change_diff(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    status = _run_git(["status", "--short"], cwd=repo)
    return {
        "status": status,
        "changed_files": status["output"],
        "diff_stat": _run_git(["diff", "--stat"], cwd=repo),
        "diff_name_only": _run_git(["diff", "--name-only"], cwd=repo),
        "diff_summary": _run_git(["diff", "--", ":!*.sqlite", ":!*.db"], cwd=repo),
    }


@step
async def run_change_verification(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    commands = inputs.get("verification_commands") or ["pytest -q"]
    results = [_run_shell(command, cwd=repo) for command in commands]
    return {"ok": all(result["ok"] for result in results), "results": results}


@step
async def build_landing_packet(
    ctx,
    inputs: Dict[str, Any],
    plan: Dict[str, Any],
    implementation_signal: Dict[str, Any],
    diff: Dict[str, Any],
    verification: Dict[str, Any],
) -> Dict[str, Any]:
    blockers = []
    if not verification["ok"]:
        blockers.append("verification failed")
    if not diff["status"]["output"]:
        blockers.append("no working-tree changes detected")
    return {
        "goal": inputs["goal"],
        "plan": plan,
        "implementation_signal": implementation_signal,
        "diff": diff,
        "verification": verification,
        "blockers": blockers,
        "recommendation": "approve" if not blockers else "do_not_approve",
        "commit_message": inputs.get("commit_message") or f"feat: {inputs['goal'][:60]}",
    }


@step
async def write_change_review_report(
    ctx,
    inputs: Dict[str, Any],
    plan_decision: Dict[str, Any],
    landing_decision: Dict[str, Any],
    packet: Dict[str, Any],
) -> Dict[str, Any]:
    report_path = Path(inputs["report_path"]).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    verification_lines = []
    for result in packet["verification"]["results"]:
        verification_lines.append(f"### `{result['command']}`\n\n```text\n{result['output']}\n```")
    plan = packet["plan"]
    verification_commands = chr(10).join(f"- `{command}`" for command in plan["verification_commands"])
    risk_notes = chr(10).join(f"- {note}" for note in plan["risk_notes"])
    approval_gates = chr(10).join(f"- `{gate}`" for gate in plan["approval_gates"])
    contents = f"""# Repo change review: {packet['goal']}

Plan approved by: {plan_decision.get('by', 'unknown')}
Landing approved by: {landing_decision.get('by', 'unknown')}
Recommendation: {packet['recommendation']}

## Repo

- Path: `{packet['plan']['repo']['repo_path']}`
- Branch: `{packet['plan']['repo']['branch']}`
- Baseline HEAD: `{packet['plan']['repo']['head']}`

## Plan

Goal: {plan['goal']}

### Approval gates

{approval_gates}

### Verification commands

{verification_commands}

### Implementation boundary

{plan['implementation_boundary']}

### Risk notes

{risk_notes}

## Verification

- Tests: {'pass' if packet['verification']['ok'] else 'fail'}

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
    report_path.write_text(contents)
    return {"report_path": str(report_path), "bytes": len(contents.encode("utf-8"))}


@step
async def land_change(ctx, inputs: Dict[str, Any], packet: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
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
async def repo_change_review_workflow(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = await inspect_change_repo(ctx, inputs)
    plan = await draft_change_plan(ctx, repo, inputs)
    plan_decision = await ctx.approval.request(
        f"Approve implementation plan for: {inputs['goal']}?",
        key="approve_change_plan",
        artifact=plan,
        approver="human:skylar",
        allowed=["approve", "reject", "edit", "rerun"],
        authority=["approve_plan"],
    )
    if plan_decision.get("action") != "approve":
        return {"ready": False, "stage": "plan_rejected", "plan": plan, "decision": plan_decision}

    implementation_signal = await ctx.wait_for("implementation.ready", key="change_ready")
    diff = await collect_change_diff(ctx, inputs)
    verification = await run_change_verification(ctx, inputs)
    packet = await build_landing_packet(ctx, inputs, plan, implementation_signal, diff, verification)
    landing_decision = await ctx.approval.request(
        f"Approve landing change for: {inputs['goal']}?",
        key="approve_change_landing",
        artifact=packet,
        approver="human:skylar",
        allowed=["approve", "reject", "edit", "rerun"],
        authority=["commit", "push"] if inputs.get("push") or inputs.get("commit") else ["approve_report"],
    )
    if landing_decision.get("action") != "approve":
        return {"ready": False, "stage": "landing_rejected", "packet": packet, "decision": landing_decision}

    report = await write_change_review_report(ctx, inputs, plan_decision, landing_decision, packet)
    landing = await land_change(ctx, inputs, packet, report)
    return {"ready": True, "packet": packet, **landing}

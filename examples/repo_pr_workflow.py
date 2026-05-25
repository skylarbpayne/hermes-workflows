from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_workflows import step, workflow


DEFAULT_VERIFICATION_COMMANDS = ["pytest -q"]


def _run(command: List[str], *, cwd: Path, env: Optional[Dict[str, str]] = None, timeout: int = 300) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return {
        "command": " ".join(command),
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "output": completed.stdout.strip(),
    }


def _run_shell(command: str, *, cwd: Path, env: Optional[Dict[str, str]] = None, timeout: int = 300) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        env=env,
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


def _run_git(args: List[str], *, cwd: Path, timeout: int = 300) -> Dict[str, Any]:
    return _run(["git", *args], cwd=cwd, timeout=timeout)


def _gh_env(inputs: Dict[str, Any]) -> Dict[str, str]:
    env = os.environ.copy()
    if inputs.get("gh_home"):
        env["HOME"] = str(inputs["gh_home"])
    if inputs.get("gh_config_dir"):
        env["GH_CONFIG_DIR"] = str(inputs["gh_config_dir"])
    return env


def _approval_source_line(decision: Optional[Dict[str, Any]]) -> str:
    if not decision:
        return "not requested"
    source = decision.get("source") or {}
    provenance = source.get("message_url") or source.get("message_id") or source.get("event_id") or "missing-provenance"
    return f"{decision.get('by', 'unknown')} via {source.get('channel', 'unknown')} {provenance}"


def _owner_repo(remote_url: str) -> str:
    first_remote = remote_url.splitlines()[0] if remote_url else ""
    if "github.com:" in first_remote:
        value = first_remote.split("github.com:", 1)[1]
    elif "github.com/" in first_remote:
        value = first_remote.split("github.com/", 1)[1]
    else:
        return ""
    return value.split()[0].removesuffix(".git")


@step
async def gather_pr_evidence(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    base_branch = inputs.get("base_branch", "main")
    remote_name = inputs.get("remote_name", "origin")
    if not (repo / ".git").exists():
        raise ValueError(f"not a git repo: {repo}")

    branch = _run_git(["branch", "--show-current"], cwd=repo)
    head = _run_git(["rev-parse", "HEAD"], cwd=repo)
    status = _run_git(["status", "--short"], cwd=repo)
    remote = _run_git(["remote", "-v"], cwd=repo)
    base_ref = f"{remote_name}/{base_branch}"
    merge_base = _run_git(["merge-base", "HEAD", base_ref], cwd=repo)
    commits = _run_git(["log", "--oneline", f"{base_ref}..HEAD"], cwd=repo)
    diff_stat = _run_git(["diff", "--stat", f"{base_ref}...HEAD"], cwd=repo)
    changed_files = _run_git(["diff", "--name-only", f"{base_ref}...HEAD"], cwd=repo)
    diff_excerpt = _run_git(["diff", f"{base_ref}...HEAD", "--"], cwd=repo)
    ahead_behind = _run_git(["rev-list", "--left-right", "--count", f"{base_ref}...HEAD"], cwd=repo)

    return {
        "repo_path": str(repo),
        "base_branch": base_branch,
        "remote_name": remote_name,
        "base_ref": base_ref,
        "branch": branch["output"],
        "head": head["output"],
        "head_short": head["output"][:12] if head["ok"] else "",
        "status_short": status["output"],
        "clean": status["ok"] and status["output"] == "",
        "remote": remote["output"],
        "owner_repo": _owner_repo(remote["output"]),
        "merge_base": merge_base,
        "commits": commits["output"],
        "diff_stat": diff_stat["output"],
        "diff_excerpt": diff_excerpt["output"][: int(inputs.get("diff_excerpt_chars", 8000))],
        "changed_files": changed_files["output"],
        "ahead_behind": ahead_behind["output"],
        "ready_for_pr": bool(branch["output"] and branch["output"] != base_branch and commits["output"]),
    }


@step
async def run_pr_verification(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    commands = inputs.get("verification_commands") or DEFAULT_VERIFICATION_COMMANDS
    timeout = int(inputs.get("verification_timeout_seconds", 300))
    results = [_run_shell(command, cwd=repo, timeout=timeout) for command in commands]
    return {"ok": all(result["ok"] for result in results), "results": results}


@step
async def write_pr_body(ctx, inputs: Dict[str, Any], evidence: Dict[str, Any], verification: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    body_path = Path(inputs.get("pr_body_path") or repo / ".hermes" / "pr-workflows" / f"{ctx.workflow_id}-body.md")
    body_path = body_path.expanduser().resolve()
    body_path.parent.mkdir(parents=True, exist_ok=True)

    summary_items = inputs.get("summary") or [inputs["goal"]]
    summary = "\n".join(f"- {item}" for item in summary_items)
    verification_lines = []
    for result in verification["results"]:
        status = "PASS" if result["ok"] else "FAIL"
        verification_lines.append(
            f"- [{status}] `{result['command']}`\n\n```text\n{result['output']}\n```"
        )
    body = f"""## Summary
{summary}

## Workflow-backed PR evidence
- Workflow id: `{ctx.workflow_id}`
- Repo: `{evidence['owner_repo'] or evidence['repo_path']}`
- Branch: `{evidence['branch']}`
- Base: `{evidence['base_ref']}`
- Head: `{evidence['head_short']}`
- Working tree clean at evidence capture: `{evidence['clean']}`

### Commits
```text
{evidence['commits'] or '(none)'}
```

### Changed files
```text
{evidence['changed_files'] or '(none)'}
```

### Diff stat
```text
{evidence['diff_stat'] or '(none)'}
```

### Diff excerpt
```diff
{evidence.get('diff_excerpt') or '(none)'}
```

## Verification
{chr(10).join(verification_lines)}

## Approval / merge provenance
- PR opened by workflow step: pending
- Checks watched by workflow step: pending
- Landing/merge approval: pending human approval signal
- Merge: not performed by this workflow unless `merge=true` and approval provenance is present
"""
    body_path.write_text(body)
    return {"body_path": str(body_path), "bytes": len(body.encode("utf-8"))}


@step
async def open_pull_request(ctx, inputs: Dict[str, Any], evidence: Dict[str, Any], verification: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    if not inputs.get("create_pr", False):
        return {"opened": False, "skipped": True, "reason": "create_pr disabled", "body_path": body["body_path"]}
    if not verification["ok"]:
        raise RuntimeError("refusing to open PR because verification failed")
    if not evidence["ready_for_pr"]:
        raise RuntimeError("refusing to open PR because branch has no commits ahead of base")
    if not evidence["clean"]:
        raise RuntimeError("refusing to open PR because working tree is dirty")

    env = _gh_env(inputs)
    if inputs.get("push_branch", True):
        push = _run_git(["push", "-u", evidence["remote_name"], "HEAD"], cwd=repo, timeout=300)
        if not push["ok"]:
            raise RuntimeError(push["output"])
    else:
        push = {"ok": True, "output": "push_branch disabled"}

    existing = _run(["gh", "pr", "view", "--json", "number,url,state"], cwd=repo, env=env, timeout=120)
    if existing["ok"]:
        return {"opened": False, "existing": True, "view": existing, "push": push, "body_path": body["body_path"]}

    command = [
        "gh",
        "pr",
        "create",
        "--base",
        evidence["base_branch"],
        "--title",
        inputs.get("pr_title") or inputs["goal"],
        "--body-file",
        body["body_path"],
    ]
    if inputs.get("draft", False):
        command.append("--draft")
    created = _run(command, cwd=repo, env=env, timeout=180)
    if not created["ok"]:
        raise RuntimeError(created["output"])

    view = _run(["gh", "pr", "view", "--json", "number,url,state,headRefName,baseRefName"], cwd=repo, env=env, timeout=120)
    return {"opened": True, "created": created, "view": view, "push": push, "body_path": body["body_path"]}


@step
async def watch_pull_request_checks(ctx, inputs: Dict[str, Any], pr: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    if not inputs.get("watch_checks", False):
        return {"watched": False, "skipped": True, "reason": "watch_checks disabled"}
    if pr.get("skipped"):
        return {"watched": False, "skipped": True, "reason": "no PR was opened"}

    env = _gh_env(inputs)
    interval = str(inputs.get("check_interval_seconds", 10))
    watch = _run(["gh", "pr", "checks", "--watch", "--interval", interval], cwd=repo, env=env, timeout=int(inputs.get("check_timeout_seconds", 900)))
    final = _run(["gh", "pr", "checks"], cwd=repo, env=env, timeout=120)
    return {"watched": True, "ok": watch["ok"] and final["ok"], "watch": watch, "final": final}


@step
async def write_pr_status_report(
    ctx,
    inputs: Dict[str, Any],
    evidence: Dict[str, Any],
    verification: Dict[str, Any],
    body: Dict[str, Any],
    pr: Dict[str, Any],
    checks: Dict[str, Any],
    landing_decision: Dict[str, Any],
) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    report_path = Path(inputs.get("status_report_path") or repo / ".hermes" / "pr-workflows" / f"{ctx.workflow_id}-status.md")
    report_path = report_path.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    verification_status = "pass" if verification["ok"] else "fail"
    check_status = "pass" if checks.get("ok") else "skipped" if checks.get("skipped") else "fail"
    pr_url = ""
    if pr.get("view", {}).get("output"):
        pr_url = pr["view"]["output"]
    elif pr.get("created", {}).get("output"):
        pr_url = pr["created"]["output"]
    contents = f"""# Workflow-backed PR status: {inputs['goal']}

- Workflow id: `{ctx.workflow_id}`
- Repo: `{evidence['owner_repo'] or evidence['repo_path']}`
- Branch: `{evidence['branch']}`
- Base: `{evidence['base_ref']}`
- Head: `{evidence['head_short']}`
- Verification: {verification_status}
- PR: {pr_url or pr.get('reason', 'not available')}
- Checks: {check_status}
- Landing approval: {_approval_source_line(landing_decision)}
- Merge: not attempted by v0 status workflow

## PR body artifact

`{body['body_path']}`

## Changed files

```text
{evidence['changed_files'] or '(none)'}
```

## Diff stat

```text
{evidence['diff_stat'] or '(none)'}
```

## Checks output

```text
{checks.get('final', checks).get('output', checks.get('reason', ''))}
```
"""
    report_path.write_text(contents)
    return {
        "ready": True,
        "workflow_id": ctx.workflow_id,
        "report_path": str(report_path),
        "pr_url": pr_url,
        "verification_ok": verification["ok"],
        "checks_ok": checks.get("ok"),
        "landing_approval_source": landing_decision.get("source"),
    }


@workflow
async def repo_pr_workflow(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    evidence, verification = await ctx.gather(
        gather_pr_evidence(ctx, inputs),
        run_pr_verification(ctx, inputs),
    )
    body = await write_pr_body(ctx, inputs, evidence, verification)
    pr = await open_pull_request(ctx, inputs, evidence, verification, body)
    checks = await watch_pull_request_checks(ctx, inputs, pr)
    landing_decision = await ctx.approval.request(
        f"Approve PR landing packet for: {inputs['goal']}?",
        key="approve_pr_landing",
        artifact={
            "goal": inputs["goal"],
            "evidence": evidence,
            "verification": verification,
            "pr": pr,
            "checks": checks,
            "body": body,
        },
        approver="human:skylar",
        allowed=["approve", "reject", "edit", "rerun"],
        authority=["review_pr", "merge_pr"] if inputs.get("merge") else ["review_pr"],
    )
    if landing_decision.get("action") != "approve":
        return {"ready": False, "stage": "landing_rejected", "decision": landing_decision}
    return await write_pr_status_report(ctx, inputs, evidence, verification, body, pr, checks, landing_decision)

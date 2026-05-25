from __future__ import annotations

import json
import hashlib
import os
import subprocess
import time
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


def _source_has_provenance(source: Dict[str, Any]) -> bool:
    return bool(source.get("channel") and any(source.get(field) for field in ("message_url", "message_id", "event_id")))


def _implementation_plan_line(plan: Dict[str, Any]) -> str:
    source = plan.get("approval_source") or {}
    provenance = source.get("message_url") or source.get("message_id") or source.get("event_id") or "missing-provenance"
    return f"{plan.get('approved_by', 'unknown')} via {source.get('channel', 'unknown')} {provenance}"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _has_matching_plan_approval_event(events: List[Dict[str, Any]], durable_plan: Dict[str, Any]) -> bool:
    for event in events:
        if event.get("type") != "SignalReceived" or event.get("key") != "signal:approval.decision:approve_implementation_plan":
            continue
        payload = event.get("payload") or {}
        decision = payload.get("payload") or {}
        source = payload.get("source") or {}
        if (
            decision.get("action") == "approve"
            and decision.get("by") == durable_plan.get("approved_by") == "skylar"
            and source == durable_plan.get("approval_source")
            and source.get("kind") == "human"
            and source.get("id") == "skylar"
            and _source_has_provenance(source)
        ):
            return True
    return False


def _require_approved_implementation_plan(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    plan = inputs.get("implementation_plan")
    if not isinstance(plan, dict) or plan.get("ready_for_implementation") is not True:
        raise RuntimeError("repo_pr_workflow requires approved implementation_plan before PR work")
    source = plan.get("approval_source")
    if plan.get("approved_by") != "skylar" or not isinstance(source, dict) or source.get("kind") != "human" or source.get("id") != "skylar":
        raise RuntimeError("repo_pr_workflow requires approved implementation_plan from human:skylar")
    if not _source_has_provenance(source):
        raise RuntimeError("repo_pr_workflow requires approved implementation_plan with external approval provenance")
    if not plan.get("plan_artifact_path") or not plan.get("plan_workflow_id"):
        raise RuntimeError("repo_pr_workflow requires approved implementation_plan with plan artifact and workflow id")

    try:
        plan_status = ctx.engine.workflow_status(str(plan["plan_workflow_id"]), recent_events=100)
    except KeyError as exc:
        raise RuntimeError(f"repo_pr_workflow requires completed implementation plan workflow: {plan['plan_workflow_id']}") from exc

    if plan_status.get("workflow_name") != "repo_change_plan_workflow" or plan_status.get("status") != "completed":
        raise RuntimeError(f"repo_pr_workflow requires completed implementation plan workflow: {plan['plan_workflow_id']}")
    durable_plan = plan_status.get("result")
    if not isinstance(durable_plan, dict) or durable_plan.get("ready_for_implementation") is not True:
        raise RuntimeError("repo_pr_workflow requires implementation plan workflow result ready_for_implementation=true")
    durable_source = durable_plan.get("approval_source")
    if durable_plan.get("approved_by") != "skylar" or not isinstance(durable_source, dict) or durable_source.get("kind") != "human" or durable_source.get("id") != "skylar":
        raise RuntimeError("repo_pr_workflow requires durable implementation plan approval from human:skylar")
    if not _source_has_provenance(durable_source) or not _has_matching_plan_approval_event(plan_status.get("events") or [], durable_plan):
        raise RuntimeError("repo_pr_workflow requires durable implementation plan approval event with external provenance")

    for field in ("plan_workflow_id", "plan_artifact_path", "approved_by", "approval_source", "plan_artifact_sha256"):
        if durable_plan.get(field) != plan.get(field):
            raise RuntimeError(f"repo_pr_workflow implementation_plan does not match durable plan workflow field: {field}")

    expected_scope = {
        "repo_path": str(Path(inputs["repo_path"]).expanduser().resolve()),
        "goal": inputs["goal"],
        "base_branch": inputs.get("base_branch", "main"),
        "remote_name": inputs.get("remote_name", "origin"),
    }
    for field, expected in expected_scope.items():
        if durable_plan.get(field) != expected:
            raise RuntimeError(f"repo_pr_workflow implementation_plan does not match current PR workflow field: {field}")

    artifact_path = Path(durable_plan["plan_artifact_path"]).expanduser().resolve()
    if not artifact_path.is_file():
        raise RuntimeError(f"repo_pr_workflow plan artifact does not exist: {artifact_path}")
    artifact_bytes = artifact_path.read_bytes()
    if not artifact_bytes.strip():
        raise RuntimeError(f"repo_pr_workflow plan artifact is empty: {artifact_path}")
    if _sha256_file(artifact_path) != durable_plan.get("plan_artifact_sha256"):
        raise RuntimeError(f"repo_pr_workflow plan artifact hash does not match approved plan workflow: {artifact_path}")
    return durable_plan


def _owner_repo(remote_url: str) -> str:
    first_remote = remote_url.splitlines()[0] if remote_url else ""
    if "github.com:" in first_remote:
        value = first_remote.split("github.com:", 1)[1]
    elif "github.com/" in first_remote:
        value = first_remote.split("github.com/", 1)[1]
    else:
        return ""
    return value.split()[0].removesuffix(".git")


def _checks_not_reported(result: Dict[str, Any]) -> bool:
    return "no checks reported" in result.get("output", "").lower()


def _pr_url_from_output(output: str) -> str:
    if not output:
        return ""
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            return str(parsed.get("url") or "")
    except json.JSONDecodeError:
        pass
    for line in output.splitlines():
        if line.startswith("https://github.com/"):
            return line.strip()
    return output.strip()


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
async def write_implementation_plan(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    plan_path = Path(inputs.get("plan_artifact_path") or repo / ".hermes" / "pr-workflows" / f"{ctx.workflow_id}-implementation-plan.md")
    plan_path = plan_path.expanduser().resolve()
    plan_path.parent.mkdir(parents=True, exist_ok=True)

    def bullets(items: List[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    goal = inputs["goal"]
    non_goals = inputs.get("non_goals") or [
        "Do not implement before this plan is explicitly approved.",
        "Do not treat plan approval as merge or deployment approval.",
        "Do not broaden the slice beyond the named repo workflow change.",
    ]
    proposed_changes = inputs.get("proposed_changes") or [
        "Write or update targeted tests first.",
        "Implement the smallest code change required by the approved plan.",
        "Document approval provenance in PR/status evidence.",
    ]
    api_changes = inputs.get("api_or_event_changes") or [
        "Approval signal key: approve_implementation_plan.",
        "Approval source must be human:skylar with external provenance.",
    ]
    verification = inputs.get("verification_commands") or DEFAULT_VERIFICATION_COMMANDS
    open_questions = inputs.get("open_questions") or ["None for this slice after plan approval."]

    contents = f"""# Implementation plan: {goal}

## Goal
{goal}

## Non-goals
{bullets(non_goals)}

## Current baseline / state
- Repo: `{repo}`
- Workflow id: `{ctx.workflow_id}`
- This plan must be approved before implementation work starts.

## Proposed file/module changes
{bullets(proposed_changes)}

## API / schema / event changes
{bullets(api_changes)}

## Execution sequence
1. Create this plan artifact.
2. Wait for explicit human approval of this plan.
3. Implement with TDD only after approval.
4. Open/update PR and produce landing evidence.
5. Wait for separate merge approval.

## Approval gates
- Plan approval: `approve_implementation_plan`, approver `human:skylar`, before implementation.
- Landing approval: `approve_pr_landing`, approver `human:skylar`, before merge/landing.

## Tests / verification
{bullets([f"`{command}`" for command in verification])}

## Side effects
- After approval only: code changes, commit, branch push, PR creation/update, GitHub check watching, Kanban evidence.
- No merge/deploy without a separate landing approval.

## Risks / rollback
- Risk: plan approval is confused with merge approval. Mitigation: separate keys and report sections.
- Risk: missing provenance. Mitigation: require human source plus channel/message provenance.
- Rollback: stop before implementation, or close/supersede the PR if the approved slice proves wrong.

## Open questions / decision points
{bullets(open_questions)}
"""
    plan_path.write_text(contents)
    return {"plan_artifact_path": str(plan_path), "bytes": len(contents.encode("utf-8"))}


@step
async def write_pr_body(
    ctx,
    inputs: Dict[str, Any],
    evidence: Dict[str, Any],
    verification: Dict[str, Any],
    implementation_plan: Dict[str, Any],
) -> Dict[str, Any]:
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

## Implementation plan approval
- Plan workflow: `{implementation_plan['plan_workflow_id']}`
- Plan artifact: `{implementation_plan['plan_artifact_path']}`
- Plan artifact SHA-256: `{implementation_plan['plan_artifact_sha256']}`
- Approved by: {_implementation_plan_line(implementation_plan)}

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
        edit = {"ok": True, "output": "update_existing_pr disabled", "returncode": 0}
        if inputs.get("update_existing_pr", True):
            edit = _run(
                [
                    "gh",
                    "pr",
                    "edit",
                    "--title",
                    inputs.get("pr_title") or inputs["goal"],
                    "--body-file",
                    body["body_path"],
                ],
                cwd=repo,
                env=env,
                timeout=180,
            )
            if not edit["ok"]:
                raise RuntimeError(edit["output"])
            existing = _run(
                ["gh", "pr", "view", "--json", "number,url,state,headRefName,baseRefName"],
                cwd=repo,
                env=env,
                timeout=120,
            )
        return {
            "opened": False,
            "existing": True,
            "view": existing,
            "edit": edit,
            "push": push,
            "body_path": body["body_path"],
        }

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
    attempts = int(inputs.get("check_appearance_attempts", 6))
    watch = {"ok": False, "output": "not run", "returncode": 1}
    final = {"ok": False, "output": "not run", "returncode": 1}
    for attempt in range(1, attempts + 1):
        watch = _run(
            ["gh", "pr", "checks", "--watch", "--interval", interval],
            cwd=repo,
            env=env,
            timeout=int(inputs.get("check_timeout_seconds", 900)),
        )
        final = _run(["gh", "pr", "checks"], cwd=repo, env=env, timeout=120)
        if final["ok"] or not _checks_not_reported(final):
            return {
                "watched": True,
                "ok": final["ok"] and (watch["ok"] or _checks_not_reported(watch)),
                "watch": watch,
                "final": final,
                "attempts": attempt,
            }
        if attempt < attempts:
            time.sleep(float(interval))
    return {"watched": True, "ok": False, "watch": watch, "final": final, "attempts": attempts}


@step
async def write_pr_status_report(
    ctx,
    inputs: Dict[str, Any],
    evidence: Dict[str, Any],
    verification: Dict[str, Any],
    body: Dict[str, Any],
    pr: Dict[str, Any],
    checks: Dict[str, Any],
    implementation_plan: Dict[str, Any],
    landing_decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    report_path = Path(inputs.get("status_report_path") or repo / ".hermes" / "pr-workflows" / f"{ctx.workflow_id}-status.md")
    report_path = report_path.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    verification_status = "pass" if verification["ok"] else "fail"
    check_status = "pass" if checks.get("ok") else "skipped" if checks.get("skipped") else "fail"
    pr_url = ""
    if pr.get("view", {}).get("output"):
        pr_url = _pr_url_from_output(pr["view"]["output"])
    elif pr.get("created", {}).get("output"):
        pr_url = _pr_url_from_output(pr["created"]["output"])
    contents = f"""# Workflow-backed PR status: {inputs['goal']}

- Workflow id: `{ctx.workflow_id}`
- Repo: `{evidence['owner_repo'] or evidence['repo_path']}`
- Branch: `{evidence['branch']}`
- Base: `{evidence['base_ref']}`
- Head: `{evidence['head_short']}`
- Verification: {verification_status}
- PR: {pr_url or pr.get('reason', 'not available')}
- Checks: {check_status}
- Implementation plan: {_implementation_plan_line(implementation_plan)}
- Plan workflow: `{implementation_plan['plan_workflow_id']}`
- Plan artifact: `{implementation_plan['plan_artifact_path']}`
- Plan artifact SHA-256: `{implementation_plan['plan_artifact_sha256']}`
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
        "approval_pending": landing_decision is None,
        "landing_approval_source": (landing_decision or {}).get("source"),
    }


@workflow
async def repo_change_plan_workflow(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    plan = await write_implementation_plan(ctx, inputs)
    decision = await ctx.approval.request(
        f"Approve implementation plan for: {inputs['goal']}?",
        key="approve_implementation_plan",
        artifact={"goal": inputs["goal"], "plan": plan},
        approver="human:skylar",
        allowed=["approve", "reject", "edit", "rerun"],
        authority=["implement_pr"],
    )
    if decision.get("action") != "approve":
        return {"ready_for_implementation": False, "stage": "plan_rejected", "decision": decision, **plan}
    return {
        "ready_for_implementation": True,
        "plan_workflow_id": ctx.workflow_id,
        "repo_path": str(Path(inputs["repo_path"]).expanduser().resolve()),
        "goal": inputs["goal"],
        "base_branch": inputs.get("base_branch", "main"),
        "remote_name": inputs.get("remote_name", "origin"),
        "plan_artifact_path": plan["plan_artifact_path"],
        "plan_artifact_sha256": _sha256_file(Path(plan["plan_artifact_path"]).expanduser().resolve()),
        "approved_by": decision.get("by"),
        "approval_source": decision.get("source"),
    }


@workflow
async def repo_pr_workflow(ctx, inputs: Dict[str, Any]) -> Dict[str, Any]:
    implementation_plan = _require_approved_implementation_plan(ctx, inputs)
    evidence, verification = await ctx.gather(
        gather_pr_evidence(ctx, inputs),
        run_pr_verification(ctx, inputs),
    )
    body = await write_pr_body(ctx, inputs, evidence, verification, implementation_plan)
    pr = await open_pull_request(ctx, inputs, evidence, verification, body)
    checks = await watch_pull_request_checks(ctx, inputs, pr)
    landing_packet = await write_pr_status_report(ctx, inputs, evidence, verification, body, pr, checks, implementation_plan)
    landing_decision = await ctx.approval.request(
        f"Approve PR landing packet for: {inputs['goal']}?",
        key="approve_pr_landing",
        artifact={
            "goal": inputs["goal"],
            "implementation_plan": implementation_plan,
            "evidence": evidence,
            "verification": verification,
            "pr": pr,
            "checks": checks,
            "body": body,
            "landing_packet": landing_packet,
        },
        approver="human:skylar",
        allowed=["approve", "reject", "edit", "rerun"],
        authority=["review_pr", "merge_pr"] if inputs.get("merge") else ["review_pr"],
    )
    if landing_decision.get("action") != "approve":
        return {"ready": False, "stage": "landing_rejected", "decision": landing_decision}
    return await write_pr_status_report(ctx, inputs, evidence, verification, body, pr, checks, implementation_plan, landing_decision)

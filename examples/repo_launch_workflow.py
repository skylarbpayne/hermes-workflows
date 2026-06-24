from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict

from hermes_workflows import approve, step, workflow


def _run(command: list[str], *, cwd: Path, timeout: int = 120) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "output": completed.stdout.strip(),
        "ok": completed.returncode == 0,
    }


def _format_approval_source(decision: Dict[str, Any]) -> str:
    source = decision.get("source") or {}
    provenance = source.get("message_url") or source.get("message_id") or source.get("event_id") or "unknown"
    return f"{source.get('channel', 'unknown')} {provenance}"


@step
async def inspect_repo(inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(inputs["repo_path"]).expanduser().resolve()
    if not repo.exists():
        raise FileNotFoundError(f"repo_path does not exist: {repo}")
    if not (repo / ".git").exists():
        raise ValueError(f"repo_path is not a git repo: {repo}")

    status = _run(["git", "status", "--short"], cwd=repo)
    branch = _run(["git", "branch", "--show-current"], cwd=repo)
    head = _run(["git", "rev-parse", "--short", "HEAD"], cwd=repo)
    remote = _run(["git", "remote", "-v"], cwd=repo)
    return {
        "repo_path": str(repo),
        "project": inputs.get("project") or repo.name,
        "branch": branch["output"],
        "head": head["output"],
        "remote": remote["output"],
        "status_short": status["output"],
        "clean": status["ok"] and status["output"] == "",
    }


@step
async def run_repo_tests(repo_info: Dict[str, Any]) -> Dict[str, Any]:
    repo = Path(repo_info["repo_path"])
    result = _run(["pytest", "-q"], cwd=repo, timeout=180)
    return result


@step
async def build_launch_packet(repo_info: Dict[str, Any], tests: Dict[str, Any]) -> Dict[str, Any]:
    blockers = []
    if not repo_info["clean"]:
        blockers.append("git working tree is not clean")
    if not tests["ok"]:
        blockers.append("tests are failing")
    return {
        "project": repo_info["project"],
        "repo": repo_info,
        "git": {"clean": repo_info["clean"], "branch": repo_info["branch"], "head": repo_info["head"]},
        "tests": {"ok": tests["ok"], "output": tests["output"], "command": " ".join(tests["command"])},
        "blockers": blockers,
        "recommendation": "approve" if not blockers else "do_not_approve",
    }


@step
async def write_launch_report(packet: Dict[str, Any], decision: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    report_path = Path(inputs["report_path"]).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    contents = f"""# Repo launch packet: {packet['project']}

Approved by: {decision.get('by', 'unknown')}
Approval source: {_format_approval_source(decision)}
Decision: {decision.get('action')}

## Status

- Repo: `{packet['repo']['repo_path']}`
- Branch: `{packet['git']['branch']}`
- HEAD: `{packet['git']['head']}`
- Git clean: {'yes' if packet['git']['clean'] else 'no'}
- Tests: {'pass' if packet['tests']['ok'] else 'fail'}

## Test output

```text
{packet['tests']['output']}
```

## Blockers

{chr(10).join(f'- {blocker}' for blocker in packet['blockers']) if packet['blockers'] else '- none'}

## Next action

Use this packet as the approval checkpoint before wiring the workflow to any higher-blast-radius adapter.
"""
    report_path.write_text(contents)
    return {"report_path": str(report_path), "bytes": len(contents.encode("utf-8"))}


@workflow
async def repo_launch_workflow(inputs: Dict[str, Any]) -> Dict[str, Any]:
    repo_info = await inspect_repo(inputs)
    tests = await run_repo_tests(repo_info)
    packet = await build_launch_packet(repo_info, tests)
    decision = await approve(
        f"Approve launch packet for {packet['project']}?",
        key="approve_repo_launch",
        artifact=packet,
        allowed=["approve", "reject", "edit", "rerun"],
    )
    if decision.get("action") != "approve":
        return {"ready": False, "packet": packet, "decision": decision}
    report = await write_launch_report(packet, decision, inputs)
    return {"ready": True, "packet": packet, **report}

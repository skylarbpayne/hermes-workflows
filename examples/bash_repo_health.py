from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hermes_workflows import agent, ask, bash, workflow


@dataclass
class HealthSummary:
    status: Literal["pass", "warn", "fail"]
    headline: str
    next_step: str


@dataclass
class ReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@workflow
async def bash_repo_health_workflow(inputs: dict) -> dict:
    """Run a deterministic command, summarize it, and ask for review."""

    command = str(inputs.get("command") or "python --version")
    check = await bash(command, key="repo_health_check", timeout_seconds=30)
    summary = await agent(
        "summarize_health_check",
        prompt="Summarize this command result for a launch-readiness review.",
        input={"command": command, "exit_code": check.exit_code, "stdout": check.stdout, "stderr": check.stderr},
        returns=HealthSummary,
        mock_output={
            "status": "pass" if check.exit_code == 0 else "fail",
            "headline": f"`{command}` exited with {check.exit_code}.",
            "next_step": "Proceed" if check.exit_code == 0 else "Inspect stderr and retry",
        },
    )
    decision = await ask(
        "Review this repo-health packet.",
        key="review_repo_health",
        input={"check": check, "summary": summary},
        returns=ReviewDecision,
    )
    return {"check": check, "summary": summary, "decision": decision}


if __name__ == "__main__":
    raise SystemExit(bash_repo_health_workflow.run())

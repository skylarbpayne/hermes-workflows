from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from hermes_workflows import agent, ask, bash, workflow


@dataclass
class CodingReviewDemoInput:
    repo_path: str = "."
    base_ref: str = "HEAD"
    worktree_path: str = "/tmp/hermes-workflows-demo-worktree"
    branch_name: str = "demo/coding-review-workflow"
    task: str = "Make a small, reviewable code change."
    validation_command: str = "python -m compileall -q src/hermes_workflows"
    approver: str = "human:operator"


@dataclass
class ImplementationResult:
    summary: str
    changed_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ValidationEvidence:
    summary: str
    local_deploy_command: str | None = None
    validation_commands: list[str] = field(default_factory=list)
    curl_requests: list[str] = field(default_factory=list)
    curl_responses: list[str] = field(default_factory=list)
    screenshot_paths: list[str] = field(default_factory=list)
    request_response_pairs: list[dict[str, Any]] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    exit_code: int | None = None
    gaps: list[str] = field(default_factory=list)


@dataclass
class WorktreeChange:
    task: str
    worktree_path: str
    implementation: ImplementationResult
    git_status: str
    diff_stat: str
    changed_files: str
    untracked_files: str
    full_diff_tail: str
    validation: ValidationEvidence


@dataclass
class CodeReviewFinding:
    verdict: Literal["approve", "request_changes"]
    summary: str
    must_fix: list[str] = field(default_factory=list)
    validation_gaps: list[str] = field(default_factory=list)
    risk: str | None = None


@dataclass
class HumanReviewDecision:
    action: Literal["approve", "request_changes"]
    feedback: str | None = None


@dataclass
class PullRequestDraft:
    title: str
    body_markdown: str
    evidence_included: list[str] = field(default_factory=list)
    commands_to_run: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


@dataclass
class PullRequestDecision:
    action: Literal["create_pr", "request_changes", "stop"]
    feedback: str | None = None


@dataclass
class PullRequestResult:
    pr_url: str | None = None
    branch_name: str | None = None
    commit_sha: str | None = None
    pushed: bool = False
    opened_pr: bool = False
    evidence_summary: str = ""


@workflow
async def coding_review_demo_workflow(inputs: CodingReviewDemoInput) -> dict:
    """Demo shape: worktree -> agent implementation -> agentic local validation -> review -> PR gate."""

    request = inputs if isinstance(inputs, CodingReviewDemoInput) else CodingReviewDemoInput(**dict(inputs or {}))

    await bash(
        "set -euo pipefail\n"
        f"git worktree remove --force {request.worktree_path} 2>/dev/null || rm -rf {request.worktree_path}\n"
        f"git worktree add -B {request.branch_name} {request.worktree_path} {request.base_ref}",
        key="create_worktree",
        cwd=request.repo_path,
        timeout_seconds=60,
    )

    implementation = await agent(
        "implement_in_worktree",
        prompt=(
            "Implement the requested change in the supplied git worktree. "
            "Edit files only under worktree_path. Do not commit, push, open a PR, or merge. "
            "Return a concise summary and the files you changed."
        ),
        input={
            "task": request.task,
            "worktree_path": request.worktree_path,
            "validation_command": request.validation_command,
        },
        returns=ImplementationResult,
        tools=["terminal", "file"],
        isolation="none",
        timeout=900,
    )

    validation = await agent(
        "validate_locally_with_evidence",
        prompt=(
            "Validate the implemented change like a real local review, not a token compile step. "
            "If the change has a web/API surface, start a local deploy/server, exercise it with curl request/response "
            "and/or browser screenshots, and return the raw evidence. If there is no HTTP/UI surface, run the strongest "
            "local command-based validation and explain that gap. Do not redact personal/local-infra details. "
            "Do not commit, push, open a PR, merge, publish, or deploy externally."
        ),
        input={
            "task": request.task,
            "worktree_path": request.worktree_path,
            "suggested_validation_command": request.validation_command,
            "required_evidence": ["local deploy/server command when applicable", "curl request/response or screenshot evidence", "stdout/stderr/exit code"],
        },
        returns=ValidationEvidence,
        tools=["terminal", "file", "browser"],
        isolation="none",
        timeout=900,
        mock_output={
            "summary": "Deterministic demo validation: no provider-backed validator ran; execute the configured local validation command in a real run.",
            "local_deploy_command": None,
            "validation_commands": [request.validation_command],
            "curl_requests": [],
            "curl_responses": [],
            "screenshot_paths": [],
            "request_response_pairs": [],
            "stdout_tail": "",
            "stderr_tail": "",
            "exit_code": 0,
            "gaps": ["Mock demo output; real validation should include curl/screenshot evidence when a local surface exists."],
        },
    )

    git_status = await bash(
        "git status --short",
        key="collect_git_status",
        cwd=request.worktree_path,
        timeout_seconds=30,
    )
    diff_stat = await bash(
        "git diff --stat HEAD",
        key="collect_diff_stat",
        cwd=request.worktree_path,
        timeout_seconds=30,
    )
    changed_files = await bash(
        "{ git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u",
        key="collect_changed_files",
        cwd=request.worktree_path,
        timeout_seconds=30,
    )
    untracked_files = await bash(
        "git ls-files --others --exclude-standard",
        key="collect_untracked_files",
        cwd=request.worktree_path,
        timeout_seconds=30,
    )
    full_diff = await bash(
        "git diff -- . | tail -n 400",
        key="collect_full_diff_tail",
        cwd=request.worktree_path,
        timeout_seconds=30,
        max_stdout_bytes=64_000,
    )

    change = WorktreeChange(
        task=request.task,
        worktree_path=request.worktree_path,
        implementation=implementation,
        git_status=git_status.stdout or "(clean)",
        diff_stat=diff_stat.stdout or "(no tracked diff)",
        changed_files=changed_files.stdout or "(none)",
        untracked_files=untracked_files.stdout or "(none)",
        full_diff_tail=full_diff.stdout or "(no tracked diff)",
        validation=validation,
    )

    code_review = await agent(
        "code_review",
        prompt=(
            "Review this implemented worktree diff. Be strict. Compare the implementer's claimed files "
            "against git status/diff output, check scope, evaluate the local validation evidence, and name required fixes. "
            "Treat missing curl/screenshot evidence as a validation gap when the task has an HTTP/UI surface."
        ),
        input=change,
        returns=CodeReviewFinding,
        tools=["terminal", "file"],
        isolation="none",
        timeout=600,
    )

    human_decision = await ask(
        "Review this worktree change before any commit, push, PR, or merge.",
        key="review_worktree_change",
        input={"change": change, "code_review": code_review},
        returns=HumanReviewDecision,
        approver=request.approver,
    )
    if human_decision.action != "approve":
        return {
            "status": "needs_worktree_changes",
            "worktree_path": request.worktree_path,
            "change": change,
            "code_review": code_review,
            "human_decision": human_decision,
            "side_effects": {"committed": False, "pushed": False, "opened_pr": False, "merged": False},
        }

    pr_draft = await agent(
        "draft_pull_request_packet",
        prompt=(
            "Draft the PR title/body for this approved worktree change. Carry forward the actual validation evidence: "
            "local deploy commands, curl request/response snippets, screenshot paths, tests, changed files, and risks. "
            "Do not fabricate evidence and do not create the PR."
        ),
        input={"change": change, "code_review": code_review, "review_decision": human_decision},
        returns=PullRequestDraft,
        tools=["terminal", "file"],
        isolation="none",
        timeout=600,
        mock_output={
            "title": "Demo PR title from approved worktree change",
            "body_markdown": "Includes changed files, validation evidence, risks, and non-actions. Real runs should include curl/screenshot receipts when applicable.",
            "evidence_included": ["git status", "changed files", "diff stat", "local validation evidence"],
            "commands_to_run": ["git add ...", "git commit ...", "git push ...", "gh pr create ..."],
            "risks": [],
        },
    )

    pr_decision = await ask(
        "Create the PR now? This gate authorizes commit, push, and PR creation only; it does not authorize merge or deploy.",
        key="approve_create_pr",
        input={"pr_draft": pr_draft, "change": change, "code_review": code_review},
        returns=PullRequestDecision,
        approver=request.approver,
    )
    if pr_decision.action != "create_pr":
        return {
            "status": "pr_creation_not_approved",
            "worktree_path": request.worktree_path,
            "change": change,
            "code_review": code_review,
            "human_decision": human_decision,
            "pr_draft": pr_draft,
            "pr_decision": pr_decision,
            "side_effects": {"committed": False, "pushed": False, "opened_pr": False, "merged": False},
        }

    pr_result = await agent(
        "create_pull_request",
        prompt=(
            "Commit the approved worktree change, push the branch, and create a GitHub PR. Use the supplied PR draft. "
            "Carry forward validation evidence in the PR body. Do not merge, deploy, publish, or perform unrelated cleanup. "
            "Return the PR URL, branch, commit SHA, and evidence summary."
        ),
        input={"worktree_path": request.worktree_path, "branch_name": request.branch_name, "pr_draft": pr_draft, "change": change},
        returns=PullRequestResult,
        tools=["terminal", "file"],
        isolation="none",
        timeout=900,
        mock_output={
            "pr_url": None,
            "branch_name": request.branch_name,
            "commit_sha": None,
            "pushed": False,
            "opened_pr": False,
            "evidence_summary": "Mock demo output; real run creates PR only after approve_create_pr.",
        },
    )

    return {
        "status": "pr_created" if pr_result.opened_pr else "pr_creation_attempt_recorded",
        "worktree_path": request.worktree_path,
        "change": change,
        "code_review": code_review,
        "human_decision": human_decision,
        "pr_draft": pr_draft,
        "pr_decision": pr_decision,
        "pr_result": pr_result,
        "side_effects": {"committed": bool(pr_result.commit_sha), "pushed": pr_result.pushed, "opened_pr": pr_result.opened_pr, "merged": False},
    }


if __name__ == "__main__":
    raise SystemExit(coding_review_demo_workflow.run())  # type: ignore[attr-defined]

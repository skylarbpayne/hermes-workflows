from __future__ import annotations

from dataclasses import dataclass, field
import shlex
from typing import Any, Literal, Mapping

from hermes_workflows import agent, ask, bash, workflow


@dataclass
class PersonalPRInput:
    intent: str
    repo_hints: list[str] = field(default_factory=list)
    approver: str = "skylar"
    default_branch: str = "main"
    worktree_root: str = "/tmp/personal-pr-worktrees"
    dry_run: bool = True
    mock_agents: bool = True

    @classmethod
    def from_value(cls, value: object) -> "PersonalPRInput":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                intent=str(value.get("intent") or "Prepare a personal PR"),
                repo_hints=[str(item) for item in value.get("repo_hints", [])],
                approver=str(value.get("approver") or "skylar"),
                default_branch=str(value.get("default_branch") or "main"),
                worktree_root=str(value.get("worktree_root") or "/tmp/personal-pr-worktrees"),
                dry_run=bool(value.get("dry_run", True)),
                mock_agents=bool(value.get("mock_agents", True)),
            )
        raise TypeError(f"cannot coerce {type(value).__name__} to PersonalPRInput")


@dataclass
class RepoTarget:
    path: str
    role: Literal["primary", "secondary", "validation_only"]
    reason: str


@dataclass
class RelatedItem:
    source: Literal["github", "kanban", "repo", "other"]
    id: str
    title: str
    url: str | None = None
    relevance: Literal["direct", "related", "background"] = "related"


@dataclass
class PhasePlan:
    name: str
    implement: str
    review: str
    validate: list[str]
    expected_artifacts: list[str] = field(default_factory=list)


@dataclass
class PRPlan:
    goal: str
    repos: list[RepoTarget]
    related: list[RelatedItem]
    phases: list[PhasePlan]
    final_pr_summary: str
    deploy_meaning: str
    non_goals: list[str] = field(default_factory=list)


@dataclass
class HumanDecision:
    action: Literal["approve", "request_changes", "cancel"]
    feedback: str | None = None


@dataclass
class WorktreeContext:
    base_repo: str
    worktree_path: str
    branch: str
    default_branch: str


@dataclass
class CommandReceipt:
    command: str
    cwd: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


@dataclass
class PhaseReceipt:
    phase: str
    implementation_summary: str
    review_summary: str
    validation: list[CommandReceipt]
    artifacts: list[str]
    status: Literal["passed", "failed"]


@dataclass
class FinalPRPacket:
    title: str
    body: str
    artifacts: list[str] = field(default_factory=list)


@dataclass
class PRReceipt:
    url: str | None
    branch: str
    commit: CommandReceipt | None = None
    push: CommandReceipt | None = None
    create_pr: CommandReceipt | None = None
    dry_run: bool = False


@dataclass
class MergeDeployReceipt:
    merged: bool
    deployed: bool
    merge: CommandReceipt | None = None
    deploy_summary: str | None = None
    post_merge_validation: CommandReceipt | None = None


@dataclass
class PersonalPRResult:
    plan: PRPlan
    worktree: WorktreeContext | None
    phase_receipts: list[PhaseReceipt]
    pr: PRReceipt | None
    merge_receipt: MergeDeployReceipt | None
    status: Literal["blocked", "pr_open", "merged_deployed"]


@workflow
async def personal_pr_delivery_workflow(inputs: PersonalPRInput | dict[str, Any]) -> PersonalPRResult:
    """Drive a personal engineering request to a reviewable PR, then merge/deploy after approval."""

    request = PersonalPRInput.from_value(inputs)

    plan = await draft_plan_with_feedback(request)
    worktree = await create_isolated_worktree(plan=plan, request=request)

    phase_receipts: list[PhaseReceipt] = []
    for phase in plan.phases:
        receipt = await run_phase_with_feedback(
            phase=phase,
            plan=plan,
            worktree=worktree,
            request=request,
        )
        phase_receipts.append(receipt)

    packet = await build_pr_packet_with_feedback(
        plan=plan,
        worktree=worktree,
        phase_receipts=phase_receipts,
        request=request,
    )
    pr = await create_pr(worktree=worktree, packet=packet, dry_run=request.dry_run)

    merge_receipt = await merge_and_deploy_with_approval(
        plan=plan,
        worktree=worktree,
        pr=pr,
        phase_receipts=phase_receipts,
        request=request,
    )
    status: Literal["blocked", "pr_open", "merged_deployed"] = "merged_deployed" if merge_receipt else "pr_open"
    return PersonalPRResult(plan, worktree, phase_receipts, pr, merge_receipt, status)


async def draft_plan_with_feedback(request: PersonalPRInput) -> PRPlan:
    feedback: str | None = None
    for attempt in range(1, 4):
        plan = await draft_plan(request=request, feedback=feedback, attempt=attempt)
        decision = await ask(
            "Review the PR plan before implementation starts.",
            key=f"review_pr_plan_{attempt}",
            input={
                "plan": plan,
                "approve_means": "create an isolated worktree and begin phase implementation",
                "feedback_examples": [
                    "wrong repo",
                    "missing related issue",
                    "split or merge phases",
                    "validation is too weak",
                    "deploy meaning is wrong",
                ],
            },
            returns=HumanDecision,
            approver=request.approver,
        )
        if decision.action == "approve":
            return plan
        if decision.action == "cancel":
            raise RuntimeError(f"personal PR workflow cancelled during planning: {decision.feedback or ''}")
        feedback = decision.feedback or "Revise the plan based on reviewer feedback."
    raise RuntimeError("PR plan was not approved after 3 attempts")


async def draft_plan(*, request: PersonalPRInput, feedback: str | None, attempt: int) -> PRPlan:
    return coerce_plan(await agent(
        "draft_personal_pr_plan",
        prompt=(
            "Draft a simple personal PR plan. Identify touched repo(s), related GitHub/Kanban/repo "
            "context, phases, implement/review/validate receipts for each phase, final PR summary, "
            "and deploy meaning. Keep it to the user's requested workflow; do not add ceremony. "
            "If feedback is present, revise the previous plan accordingly."
        ),
        input={
            "intent": request.intent,
            "repo_hints": request.repo_hints,
            "default_branch": request.default_branch,
            "feedback": feedback,
            "attempt": attempt,
        },
        returns=PRPlan,
        mock_output=_mock_plan_output(request, feedback) if request.mock_agents else None,
    ))


async def create_isolated_worktree(*, plan: PRPlan, request: PersonalPRInput) -> WorktreeContext:
    primary_repo = get_primary_repo(plan)
    branch = f"personal-pr/{safe_key(plan.goal)[:48]}"
    worktree_path = f"{request.worktree_root}/{safe_key(plan.goal)[:64]}"

    status = await checked_bash(
        "git status --short --branch",
        key="base_repo_status",
        cwd=primary_repo.path,
        timeout_seconds=30,
    )
    if status.exit_code != 0:
        raise RuntimeError(f"could not inspect base repo: {status.stdout}\n{status.stderr}")

    if has_dirty_worktree(status):
        decision = await ask(
            "Base repo is dirty before creating the worktree. Continue?",
            key="review_dirty_base_repo",
            input={"repo": primary_repo.path, "status": status, "recommendation": "usually cancel or choose a clean base"},
            returns=HumanDecision,
            approver=request.approver,
        )
        if decision.action != "approve":
            raise RuntimeError("cancelled because base repo was dirty")

    create = await checked_bash(
        "mkdir -p "
        + shlex.quote(request.worktree_root)
        + " && (git fetch origin "
        + shlex.quote(request.default_branch)
        + " >/dev/null 2>&1 || true)"
        + " && if git rev-parse --verify origin/"
        + shlex.quote(request.default_branch)
        + " >/dev/null 2>&1; then git worktree add -b "
        + shlex.quote(branch)
        + " "
        + shlex.quote(worktree_path)
        + " origin/"
        + shlex.quote(request.default_branch)
        + "; else git worktree add -b "
        + shlex.quote(branch)
        + " "
        + shlex.quote(worktree_path)
        + " "
        + shlex.quote(request.default_branch)
        + "; fi",
        key="create_worktree",
        cwd=primary_repo.path,
        timeout_seconds=300,
    )
    if create.exit_code != 0:
        raise RuntimeError(f"failed to create worktree: {create.stdout}\n{create.stderr}")

    return WorktreeContext(
        base_repo=primary_repo.path,
        worktree_path=worktree_path,
        branch=branch,
        default_branch=request.default_branch,
    )


async def run_phase_with_feedback(
    *,
    phase: PhasePlan,
    plan: PRPlan,
    worktree: WorktreeContext,
    request: PersonalPRInput,
) -> PhaseReceipt:
    feedback: str | None = None
    for attempt in range(1, 4):
        receipt = await run_phase_once(
            phase=phase,
            plan=plan,
            worktree=worktree,
            feedback=feedback,
            attempt=attempt,
            mock_agents=request.mock_agents,
        )
        decision = await ask(
            f"Review phase receipt: {phase.name}",
            key=f"review_phase_{safe_key(phase.name)}_{attempt}",
            input={
                "phase": phase,
                "receipt": receipt,
                "approve_means": "accept this phase and continue to the next phase or PR packet",
            },
            returns=HumanDecision,
            approver=request.approver,
        )
        if decision.action == "approve":
            return receipt
        if decision.action == "cancel":
            raise RuntimeError(f"cancelled during phase {phase.name}: {decision.feedback or ''}")
        feedback = decision.feedback or "Revise this phase based on reviewer feedback."
    raise RuntimeError(f"phase {phase.name} was not approved after 3 attempts")


async def run_phase_once(
    *,
    phase: PhasePlan,
    plan: PRPlan,
    worktree: WorktreeContext,
    feedback: str | None,
    attempt: int,
    mock_agents: bool,
) -> PhaseReceipt:
    implementation = await implement_phase(
        phase=phase,
        plan=plan,
        worktree=worktree,
        feedback=feedback,
        attempt=attempt,
        mock_agents=mock_agents,
    )
    review = await review_phase(
        phase=phase,
        plan=plan,
        worktree=worktree,
        implementation=implementation,
        mock_agents=mock_agents,
    )
    validation = await validate_phase(phase=phase, worktree=worktree)
    passed = all(result.exit_code == 0 for result in validation)
    return PhaseReceipt(
        phase=phase.name,
        implementation_summary=str(implementation.get("summary", implementation)),
        review_summary=str(review.get("summary", review)),
        validation=validation,
        artifacts=list(implementation.get("artifacts", [])) + phase.expected_artifacts,
        status="passed" if passed else "failed",
    )


async def implement_phase(
    *,
    phase: PhasePlan,
    plan: PRPlan,
    worktree: WorktreeContext,
    feedback: str | None,
    attempt: int,
    mock_agents: bool,
) -> dict[str, Any]:
    return await agent(
        f"implement_{safe_key(phase.name)}",
        prompt=(
            "Implement this phase only in the supplied worktree. Stay inside the approved phase scope. "
            "Do not opportunistically refactor. If the phase plan is wrong, stop and report it. "
            "If reviewer feedback is provided, address it directly. Return summary and artifacts."
        ),
        input={"plan": plan, "phase": phase, "worktree": worktree, "feedback": feedback, "attempt": attempt},
        returns=dict,
        tools=["terminal", "file"],
        mock_output={"summary": f"Dry-run implementation for {phase.name}.", "artifacts": []} if mock_agents else None,
    )


async def review_phase(
    *,
    phase: PhasePlan,
    plan: PRPlan,
    worktree: WorktreeContext,
    implementation: dict[str, Any],
    mock_agents: bool,
) -> dict[str, Any]:
    diff = await checked_bash(
        "git diff --stat && git diff --check",
        key=f"review_diff_{safe_key(phase.name)}",
        cwd=worktree.worktree_path,
        timeout_seconds=120,
    )
    return await agent(
        f"review_{safe_key(phase.name)}",
        prompt=(
            "Review the implementation against the approved phase plan. Check scope, unexpected files, "
            "understandability, validation strength, and git diff --check. Return a concise review summary."
        ),
        input={"plan": plan, "phase": phase, "implementation": implementation, "diff_check": diff, "worktree": worktree},
        returns=dict,
        tools=["terminal", "file"],
        mock_output={"summary": f"Dry-run review for {phase.name}: scope matches the plan."} if mock_agents else None,
    )


async def validate_phase(*, phase: PhasePlan, worktree: WorktreeContext) -> list[CommandReceipt]:
    receipts: list[CommandReceipt] = []
    for index, command in enumerate(phase.validate, start=1):
        receipts.append(
            await checked_bash(
                command,
                key=f"validate_{safe_key(phase.name)}_{index}",
                cwd=worktree.worktree_path,
                timeout_seconds=300,
            )
        )
    return receipts


async def build_pr_packet_with_feedback(
    *,
    plan: PRPlan,
    worktree: WorktreeContext,
    phase_receipts: list[PhaseReceipt],
    request: PersonalPRInput,
) -> FinalPRPacket:
    feedback: str | None = None
    for attempt in range(1, 4):
        packet = await build_pr_packet(
            plan=plan,
            worktree=worktree,
            phase_receipts=phase_receipts,
            feedback=feedback,
            attempt=attempt,
            mock_agents=request.mock_agents,
        )
        decision = await ask(
            "Review the final PR packet before creating the PR.",
            key=f"review_pr_packet_{attempt}",
            input={"packet": packet, "plan": plan, "phase_receipts": phase_receipts},
            returns=HumanDecision,
            approver=request.approver,
        )
        if decision.action == "approve":
            return packet
        if decision.action == "cancel":
            raise RuntimeError(f"cancelled before PR creation: {decision.feedback or ''}")
        feedback = decision.feedback or "Revise the PR packet."
    raise RuntimeError("PR packet was not approved after 3 attempts")


async def build_pr_packet(
    *,
    plan: PRPlan,
    worktree: WorktreeContext,
    phase_receipts: list[PhaseReceipt],
    feedback: str | None,
    attempt: int,
    mock_agents: bool,
) -> FinalPRPacket:
    return await agent(
        "build_final_pr_packet",
        prompt=(
            "Build a concise PR packet: why this change is needed, what changed, related issues, "
            "validation grouped by phase, artifacts, and deploy meaning. Do not dump giant logs. "
            "If feedback is provided, revise the packet accordingly."
        ),
        input={
            "plan": plan,
            "worktree": worktree,
            "phase_receipts": phase_receipts,
            "feedback": feedback,
            "attempt": attempt,
        },
        returns=FinalPRPacket,
        mock_output=_mock_pr_packet_output(plan, phase_receipts) if mock_agents else None,
    )


async def create_pr(*, worktree: WorktreeContext, packet: FinalPRPacket, dry_run: bool) -> PRReceipt:
    if dry_run:
        return PRReceipt(url=None, branch=worktree.branch, dry_run=True)

    body_file = ".hermes/pr-body.md"
    write_body = await checked_bash(
        "mkdir -p .hermes && python - <<'PY'\n"
        "from pathlib import Path\n"
        f"Path({body_file!r}).write_text({packet.body!r}, encoding='utf-8')\n"
        "PY",
        key="write_pr_body",
        cwd=worktree.worktree_path,
        timeout_seconds=30,
    )
    if write_body.exit_code != 0:
        raise RuntimeError(f"failed to write PR body: {write_body.stdout}\n{write_body.stderr}")

    commit = await checked_bash(
        "git add -A && git commit -m " + shlex.quote(packet.title),
        key="commit_changes",
        cwd=worktree.worktree_path,
        timeout_seconds=300,
    )
    if commit.exit_code != 0:
        raise RuntimeError(f"commit failed: {commit.stdout}\n{commit.stderr}")

    push = await checked_bash(
        "git push -u origin " + shlex.quote(worktree.branch),
        key="push_branch",
        cwd=worktree.worktree_path,
        timeout_seconds=300,
    )
    if push.exit_code != 0:
        raise RuntimeError(f"push failed: {push.stdout}\n{push.stderr}")

    create = await checked_bash(
        "HOME=/Users/skylarpayne gh pr create --title "
        + shlex.quote(packet.title)
        + " --body-file "
        + shlex.quote(body_file),
        key="create_pr",
        cwd=worktree.worktree_path,
        timeout_seconds=300,
    )
    if create.exit_code != 0:
        raise RuntimeError(f"PR creation failed: {create.stdout}\n{create.stderr}")

    return PRReceipt(url=create.stdout.strip(), branch=worktree.branch, commit=commit, push=push, create_pr=create)


async def merge_and_deploy_with_approval(
    *,
    plan: PRPlan,
    worktree: WorktreeContext,
    pr: PRReceipt,
    phase_receipts: list[PhaseReceipt],
    request: PersonalPRInput,
) -> MergeDeployReceipt | None:
    decision = await ask(
        "Approve merge and deploy for this PR.",
        key="approve_merge_and_deploy",
        input={
            "pr": pr,
            "plan": plan,
            "phase_receipts": phase_receipts,
            "approval_means": [
                "merge this PR",
                "run or confirm the deploy path described by the plan",
                "perform post-merge validation",
            ],
            "approval_does_not_mean": [
                "publish a package/release unless deploy_meaning explicitly says so",
                "change credentials",
                "make unrelated repo/config changes",
            ],
        },
        returns=HumanDecision,
        approver=request.approver,
    )
    if decision.action != "approve":
        return None
    if request.dry_run:
        return MergeDeployReceipt(merged=False, deployed=False, deploy_summary="Dry run: merge/deploy not executed.")

    merge = await checked_bash(
        "HOME=/Users/skylarpayne gh pr merge --squash --delete-branch",
        key="merge_pr",
        cwd=worktree.worktree_path,
        timeout_seconds=300,
    )
    if merge.exit_code != 0:
        raise RuntimeError(f"merge failed: {merge.stdout}\n{merge.stderr}")

    deploy = await agent(
        "deploy_or_confirm",
        prompt=(
            "Based on the plan's deploy meaning, run the explicit deploy command if one exists and is safe, "
            "confirm automatic deploy on merge, or confirm that merge is the endpoint. Return a short summary."
        ),
        input={"plan": plan, "worktree": worktree, "pr": pr, "merge": merge},
        returns=dict,
        tools=["terminal"],
        mock_output={"summary": "Dry-run deploy confirmation."} if request.mock_agents else None,
    )
    post_merge = await checked_bash(
        "git checkout "
        + shlex.quote(worktree.default_branch)
        + " && git pull --ff-only && git status --short --branch",
        key="post_merge_base_repo_status",
        cwd=worktree.base_repo,
        timeout_seconds=120,
    )
    return MergeDeployReceipt(
        merged=True,
        deployed=post_merge.exit_code == 0,
        merge=merge,
        deploy_summary=str(deploy),
        post_merge_validation=post_merge,
    )


async def checked_bash(command: str, *, key: str, cwd: str, timeout_seconds: int) -> CommandReceipt:
    result = await bash(command, key=key, cwd=cwd, timeout_seconds=timeout_seconds)
    return CommandReceipt(
        command=result.command,
        cwd=result.cwd,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
    )


def get_primary_repo(plan: PRPlan) -> RepoTarget:
    plan = coerce_plan(plan)
    for repo in plan.repos:
        if repo.role == "primary":
            return repo
    raise ValueError("plan must include a primary repo")


def coerce_plan(value: PRPlan | Mapping[str, Any]) -> PRPlan:
    if isinstance(value, PRPlan):
        return PRPlan(
            goal=value.goal,
            repos=[coerce_repo(repo) for repo in value.repos],
            related=[coerce_related(item) for item in value.related],
            phases=[coerce_phase(phase) for phase in value.phases],
            final_pr_summary=value.final_pr_summary,
            deploy_meaning=value.deploy_meaning,
            non_goals=[str(item) for item in value.non_goals],
        )
    if isinstance(value, Mapping):
        return PRPlan(
            goal=str(value.get("goal") or "Personal PR"),
            repos=[coerce_repo(repo) for repo in value.get("repos", [])],
            related=[coerce_related(item) for item in value.get("related", [])],
            phases=[coerce_phase(phase) for phase in value.get("phases", [])],
            final_pr_summary=str(value.get("final_pr_summary") or "Personal PR"),
            deploy_meaning=str(value.get("deploy_meaning") or "Merge is the endpoint."),
            non_goals=[str(item) for item in value.get("non_goals", [])],
        )
    raise TypeError(f"cannot coerce {type(value).__name__} to PRPlan")


def coerce_repo(value: RepoTarget | Mapping[str, Any]) -> RepoTarget:
    if isinstance(value, RepoTarget):
        return value
    if isinstance(value, Mapping):
        role = str(value.get("role") or "secondary")
        if role not in {"primary", "secondary", "validation_only"}:
            role = "secondary"
        return RepoTarget(path=str(value.get("path") or ""), role=role, reason=str(value.get("reason") or ""))  # type: ignore[arg-type]
    raise TypeError(f"cannot coerce {type(value).__name__} to RepoTarget")


def coerce_related(value: RelatedItem | Mapping[str, Any]) -> RelatedItem:
    if isinstance(value, RelatedItem):
        return value
    if isinstance(value, Mapping):
        source = str(value.get("source") or "other")
        if source not in {"github", "kanban", "repo", "other"}:
            source = "other"
        relevance = str(value.get("relevance") or "related")
        if relevance not in {"direct", "related", "background"}:
            relevance = "related"
        return RelatedItem(
            source=source,  # type: ignore[arg-type]
            id=str(value.get("id") or ""),
            title=str(value.get("title") or ""),
            url=str(value["url"]) if value.get("url") is not None else None,
            relevance=relevance,  # type: ignore[arg-type]
        )
    raise TypeError(f"cannot coerce {type(value).__name__} to RelatedItem")


def coerce_phase(value: PhasePlan | Mapping[str, Any]) -> PhasePlan:
    if isinstance(value, PhasePlan):
        return value
    if isinstance(value, Mapping):
        return PhasePlan(
            name=str(value.get("name") or "Phase"),
            implement=str(value.get("implement") or "Implement scoped change."),
            review=str(value.get("review") or "Review scoped change."),
            validate=[str(item) for item in value.get("validate", [])],
            expected_artifacts=[str(item) for item in value.get("expected_artifacts", [])],
        )
    raise TypeError(f"cannot coerce {type(value).__name__} to PhasePlan")


def has_dirty_worktree(status: CommandReceipt) -> bool:
    return any(line.strip() and not line.startswith("## ") for line in status.stdout.splitlines())


def safe_key(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    return "-".join(part for part in cleaned.split("-") if part)


def _mock_plan_output(request: PersonalPRInput, feedback: str | None) -> dict[str, Any]:
    repo_path = request.repo_hints[0] if request.repo_hints else "/tmp/example-repo"
    suffix = " Revised from reviewer feedback." if feedback else ""
    return {
        "goal": request.intent + suffix,
        "repos": [{"path": repo_path, "role": "primary", "reason": "Primary code change surface."}],
        "related": [
            {
                "source": "repo",
                "id": "local-context",
                "title": "Repo files and tests discovered during planning",
                "url": None,
                "relevance": "direct",
            }
        ],
        "phases": [
            {
                "name": "Implement scoped change",
                "implement": "Make the smallest code change that satisfies the goal.",
                "review": "Check scope, readability, and unexpected files.",
                "validate": ["python --version"],
                "expected_artifacts": ["command output"],
            }
        ],
        "final_pr_summary": "Open a concise PR with phase validation receipts.",
        "deploy_meaning": "Dry-run example: merge is the endpoint unless the repo documents a deployment path.",
        "non_goals": ["No unrelated cleanup", "No deploy or publish without approval"],
    }


def _mock_pr_packet_output(plan: PRPlan, phase_receipts: list[PhaseReceipt]) -> dict[str, Any]:
    validation = "\n".join(f"- {receipt.phase}: {receipt.status}" for receipt in phase_receipts)
    return {
        "title": plan.final_pr_summary[:72] or "Personal PR delivery workflow change",
        "body": (
            "## Why\n\n"
            + plan.goal
            + "\n\n## What changed\n\nSee phase receipts.\n\n## Validation\n\n"
            + validation
            + "\n\n## Deploy\n\n"
            + plan.deploy_meaning
        ),
        "artifacts": [],
    }


if __name__ == "__main__":
    raise SystemExit(personal_pr_delivery_workflow.run())

from __future__ import annotations

from dataclasses import dataclass

from hermes_workflows import agent, workflow


@dataclass(frozen=True)
class WorkspaceInput:
    workspace_dir: str
    task: str = "Inspect the repository status."


@workflow
async def agent_workspace_workflow(inputs: WorkspaceInput) -> dict:
    return await agent(
        "inspect_workspace",
        prompt="Inspect the workspace and return a short JSON summary.",
        input={"task": inputs.task},
        workspace_dir=inputs.workspace_dir,
        isolation="worktree",
        mock_output={"status": "ready", "workspace_dir": inputs.workspace_dir},
    )


if __name__ == "__main__":
    raise SystemExit(agent_workspace_workflow.run())  # type: ignore[attr-defined]

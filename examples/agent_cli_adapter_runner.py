from __future__ import annotations

import sys
from pathlib import Path

from hermes_workflows import AgentStep, SubprocessAgentRunner, WorkflowEngine, workflow


@workflow
async def cli_agent_adapter_example(ctx, inputs):
    return await AgentStep(
        "summarize_item",
        prompt="Summarize {{item}} as JSON.",
        variables={"item": inputs["item"]},
    )(ctx)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    db_path = Path("/tmp/hermes-agent-cli-adapter-example.sqlite")
    if db_path.exists():
        db_path.unlink()
    runner = SubprocessAgentRunner(
        [
            sys.executable,
            "-m",
            "hermes_workflows.agent_cli_adapter",
            "--agent-command",
            sys.executable,
            "--agent-arg",
            str(repo_root / "examples" / "runners" / "fake_json_cli_agent.py"),
        ],
        timeout_seconds=120,
        max_stdout_bytes=1_000_000,
    )
    engine = WorkflowEngine(db_path, agent_runner=runner)
    result = engine.run_until_idle(
        cli_agent_adapter_example,
        {"item": "alpha"},
        workflow_id="wf_agent_cli_adapter_example",
    )
    print(result)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

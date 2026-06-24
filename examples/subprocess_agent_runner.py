from __future__ import annotations

import sys
from pathlib import Path

from hermes_workflows import SubprocessAgentRunner, WorkflowEngine, agent, workflow


@workflow
async def subprocess_agent_runner_example(inputs):
    return await agent(
        "summarize_item",
        prompt=f"Summarize {inputs['item']}",
        input={"item": inputs["item"]},
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    db_path = Path("/tmp/hermes-subprocess-runner-example.sqlite")
    if db_path.exists():
        db_path.unlink()
    runner = SubprocessAgentRunner([sys.executable, str(repo_root / "examples" / "runners" / "static_json_agent.py")])
    engine = WorkflowEngine(db_path, agent_runner=runner)
    result = engine.run_until_idle(
        subprocess_agent_runner_example,
        {"item": "alpha"},
        workflow_id="wf_subprocess_runner_example",
    )
    print(result)
    if result.status != "completed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

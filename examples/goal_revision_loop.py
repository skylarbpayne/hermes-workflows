from __future__ import annotations

from dataclasses import dataclass

from hermes_workflows import agent, goal, workflow


@dataclass
class Draft:
    text: str
    score: int


@workflow
async def goal_revision_loop_workflow(inputs: dict) -> dict:
    """Use goal(do, check) for a bounded improve-until-accepted loop."""

    topic = str(inputs.get("topic") or "launch docs")
    target_score = int(inputs.get("target_score") or 2)

    def draft(previous: Draft | None = None) -> object:
        attempt = 1 if previous is None else previous.score + 1
        return agent(
            "revise_draft",
            prompt="Improve the draft until it is ready for launch review.",
            input={"topic": topic, "previous": previous, "attempt": attempt},
            key_by=attempt,
            returns=Draft,
            mock_output={"text": f"Attempt {attempt}: concise draft about {topic}.", "score": min(attempt, 2)},
        )

    def check(candidate: Draft) -> bool:
        return candidate.score >= target_score

    final = await goal(draft, check, max_iters=3)
    return {"final": final, "accepted": final.score >= target_score}


if __name__ == "__main__":
    raise SystemExit(goal_revision_loop_workflow.run())

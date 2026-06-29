from __future__ import annotations

from dataclasses import dataclass

from hermes_workflows import Artifact, MarkdownArtifact, WorkflowEngine, step, workflow
from hermes_workflows.artifacts import artifact_descriptor


@dataclass
class ArtifactWorkflowResult:
    draft: Artifact
    artifacts: list[Artifact]


@step
async def build_markdown_artifact() -> Artifact:
    return MarkdownArtifact("Draft", "# Hello from a workflow artifact")


@workflow
async def artifact_roundtrip_workflow(inputs: dict) -> ArtifactWorkflowResult:
    draft = await build_markdown_artifact()
    assert isinstance(draft, Artifact)
    return ArtifactWorkflowResult(draft=draft, artifacts=[draft])


def test_artifact_step_outputs_round_trip_as_typed_artifacts(tmp_path):
    db_path = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db_path)

    result = engine.run_until_idle(artifact_roundtrip_workflow, {}, workflow_id="wf_artifacts")

    assert result.status == "completed"
    assert isinstance(result.result.draft, Artifact)
    assert result.result.draft.metadata.title == "Draft"
    assert artifact_descriptor(result.result.draft)["render"] == "inline-markdown"

    loaded = WorkflowEngine(db_path, read_only=True)._result_from_instance("wf_artifacts")
    assert isinstance(loaded.result["draft"], Artifact)
    assert isinstance(loaded.result["artifacts"][0], Artifact)
    assert loaded.result["draft"].value == "# Hello from a workflow artifact"


def test_artifact_json_coerces_to_dataclass_fields():
    from hermes_workflows.input_parsing import coerce_workflow_input

    payload = MarkdownArtifact("Typed", "# Typed").to_json()

    hydrated = coerce_workflow_input({"draft": payload, "artifacts": [payload]}, ArtifactWorkflowResult)

    assert isinstance(hydrated.draft, Artifact)
    assert isinstance(hydrated.artifacts[0], Artifact)

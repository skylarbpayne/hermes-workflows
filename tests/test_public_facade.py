from __future__ import annotations

import hermes_workflows


def test_top_level_public_facade_teaches_authoring_primitives_and_artifacts_only() -> None:
    assert set(hermes_workflows.__all__) == {
        "Artifact",
        "ArtifactMetadata",
        "ArtifactRender",
        "FileArtifact",
        "JsonArtifact",
        "LinkArtifact",
        "MarkdownArtifact",
        "PythonSourceArtifact",
        "TextArtifact",
        "Workflow",
        "agent",
        "approve",
        "approve_many",
        "ask",
        "bash",
        "cancel_workflow",
        "gather",
        "goal",
        "map_workflow",
        "parallel",
        "pipeline",
        "PromptFile",
        "prompt_file",
        "RenderedPrompt",
        "start_child",
        "wait_for",
        "workflow",
        "workflow_id",
        "workflow_status",
    }
    assert "WorkflowEngine" not in hermes_workflows.__all__
    assert "WorkflowWorkerService" not in hermes_workflows.__all__
    assert "ApprovalDecisionInput" not in hermes_workflows.__all__
    assert "OperatorStepView" not in hermes_workflows.__all__
    assert "step" not in hermes_workflows.__all__


def test_top_level_dir_hides_advanced_compatibility_shims() -> None:
    visible = set(dir(hermes_workflows))

    assert set(hermes_workflows.__all__).issubset(visible)
    assert "WorkflowEngine" not in visible
    assert "WorkflowWorkerService" not in visible
    assert "ApprovalDecisionInput" not in visible
    assert "OperatorStepView" not in visible
    assert "step" not in visible


def test_advanced_top_level_imports_remain_compatibility_shims() -> None:
    from hermes_workflows import ApprovalDecisionInput, WorkflowEngine, bash, current_context, current_step_context, step
    from hermes_workflows.approvals import ApprovalDecisionInput as SubmoduleApprovalDecisionInput
    from hermes_workflows.authoring import current_context as submodule_current_context
    from hermes_workflows.authoring import current_step_context as submodule_current_step_context
    from hermes_workflows.bash import bash as submodule_bash
    from hermes_workflows.decorators import step as submodule_step
    from hermes_workflows.engine import WorkflowEngine as SubmoduleWorkflowEngine

    assert WorkflowEngine is SubmoduleWorkflowEngine
    assert ApprovalDecisionInput is SubmoduleApprovalDecisionInput
    assert current_context is submodule_current_context
    assert current_step_context is submodule_current_step_context
    assert step is submodule_step
    assert bash is submodule_bash


def test_all_advanced_compatibility_shims_resolve() -> None:
    for name, (module_name, attr_name) in hermes_workflows._ADVANCED_EXPORTS.items():
        module = __import__(module_name, fromlist=[attr_name])

        assert getattr(hermes_workflows, name) is getattr(module, attr_name)

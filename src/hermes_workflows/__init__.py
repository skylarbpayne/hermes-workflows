"""Public authoring facade for ``hermes-workflows``.

The launch-facing SDK is intentionally small: workflow authors should start
with ``workflow``, ``agent(...)``, ``ask(...)``, ``bash(...)``, artifacts,
``goal(...)``, ``parallel(...)``, and ``pipeline(...)``. Runtime services,
approval adapter DTOs, low-level steps, and direct engine control remain
available from their submodules for advanced integrations, but they are not
part of the default top-level teaching surface.
"""

from __future__ import annotations

from typing import Any

from .artifacts import (
    Artifact,
    ArtifactMetadata,
    ArtifactRender,
    FileArtifact,
    JsonArtifact,
    LinkArtifact,
    MarkdownArtifact,
    PythonSourceArtifact,
    TextArtifact,
)
from .authoring import agent, approve, approve_many, ask, cancel_workflow, gather, goal, map_workflow, parallel, pipeline, start_child, wait_for, workflow_id, workflow_status
from .bash import bash
from .decorators import workflow
from .prompts import PromptFile, RenderedPrompt, prompt_file
from .workflow_values import Workflow

# Import the built-in prompt/agent step module for its @step registration side
# effect. It remains an advanced submodule export, not part of __all__.
from . import prompts as _prompts  # noqa: F401

__all__ = [
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
]

_ADVANCED_EXPORTS: dict[str, tuple[str, str]] = {
    "artifact_descriptor": ("hermes_workflows.artifacts", "artifact_descriptor"),
    "normalize_artifact": ("hermes_workflows.artifacts", "normalize_artifact"),
    "workflow_source_preview": ("hermes_workflows.artifacts", "workflow_source_preview"),
    "AgentCall": ("hermes_workflows.authoring", "AgentCall"),
    "AskCall": ("hermes_workflows.authoring", "AskCall"),
    "approve": ("hermes_workflows.authoring", "approve"),
    "step": ("hermes_workflows.decorators", "step"),
    "ApprovalDecision": ("hermes_workflows.approvals", "ApprovalDecision"),
    "ApprovalDecisionInput": ("hermes_workflows.approvals", "ApprovalDecisionInput"),
    "ApprovalReceipt": ("hermes_workflows.approvals", "ApprovalReceipt"),
    "ApprovalView": ("hermes_workflows.approvals", "ApprovalView"),
    "OperatorDecision": ("hermes_workflows.approvals", "OperatorDecision"),
    "OperatorResponseInput": ("hermes_workflows.approvals", "OperatorResponseInput"),
    "OperatorResponseReceipt": ("hermes_workflows.approvals", "OperatorResponseReceipt"),
    "OperatorStepView": ("hermes_workflows.approvals", "OperatorStepView"),
    "WorkflowEngine": ("hermes_workflows.engine", "WorkflowEngine"),
    "RunResult": ("hermes_workflows.engine", "RunResult"),
    "InvocationService": ("hermes_workflows.invocation", "InvocationService"),
    "TrustedResumer": ("hermes_workflows.invocation", "TrustedResumer"),
    "render_prompt": ("hermes_workflows.prompts", "render_prompt"),
    "build_workflow_receipt": ("hermes_workflows.receipts", "build_workflow_receipt"),
    "redact_secrets": ("hermes_workflows.receipts", "redact_secrets"),
    "WorkflowDbConfig": ("hermes_workflows.registry", "WorkflowDbConfig"),
    "WorkflowRefConfig": ("hermes_workflows.registry", "WorkflowRefConfig"),
    "WorkflowRegistry": ("hermes_workflows.registry", "WorkflowRegistry"),
    "AgentRunnerError": ("hermes_workflows.runners", "AgentRunnerError"),
    "SubprocessAgentRunner": ("hermes_workflows.runners", "SubprocessAgentRunner"),
    "WorkflowWorkerService": ("hermes_workflows.worker_service", "WorkflowWorkerService"),
    "JsonObject": ("hermes_workflows.types", "JsonObject"),
    "JsonScalar": ("hermes_workflows.types", "JsonScalar"),
    "JsonValue": ("hermes_workflows.types", "JsonValue"),
    "to_json_object": ("hermes_workflows.types", "to_json_object"),
    "to_json_value": ("hermes_workflows.types", "to_json_value"),
}


def __getattr__(name: str) -> Any:
    """Resolve advanced compatibility exports lazily.

    ``from hermes_workflows import WorkflowEngine`` still works for existing
    tests, examples, and adapters, but ``__all__`` and generated public docs now
    point new users at the authoring facade instead of internals.
    """

    try:
        module_name, attr_name = _ADVANCED_EXPORTS[name]
    except KeyError as exc:  # pragma: no cover - Python owns the exact wording.
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)

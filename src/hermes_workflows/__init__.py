"""Code-first durable workflow runtime spike.

v0 intentionally tiny:
- @workflow marks an async decider function.
- @step calls become durable awaits keyed by deterministic call order.
- WorkflowEngine stores append-only events in SQLite.
- Pending work is emitted as outbox commands, then the decider exits.
- External signals append events and wake/replay the decider.
"""

from .approvals import (
    ApprovalDecision,
    ApprovalDecisionInput,
    ApprovalReceipt,
    ApprovalView,
    OperatorDecision,
    OperatorResponseInput,
    OperatorResponseReceipt,
    OperatorStepView,
)
from .authoring import AgentCall, AskCall, ContextBundle, agent, ask, approve, parallel, pipeline
from .decorators import step, workflow
from .engine import RunResult, WorkflowEngine
from .invocation import InvocationService, TrustedResumer
from .prompts import render_prompt
from .receipts import build_workflow_receipt, redact_secrets
from .registry import WorkflowDbConfig, WorkflowRefConfig, WorkflowRegistry
from .runners import AgentRunnerError, SubprocessAgentRunner
from .worker_service import WorkflowWorkerService
from .workflow_values import Workflow

__all__ = [
    "AgentCall",
    "AskCall",
    "ApprovalDecision",
    "ApprovalDecisionInput",
    "ApprovalReceipt",
    "ApprovalView",
    "OperatorDecision",
    "OperatorResponseInput",
    "OperatorResponseReceipt",
    "OperatorStepView",
    "ContextBundle",
    "AgentRunnerError",
    "RunResult",
    "InvocationService",
    "SubprocessAgentRunner",
    "TrustedResumer",
    "Workflow",
    "WorkflowDbConfig",
    "WorkflowEngine",
    "WorkflowRefConfig",
    "WorkflowRegistry",
    "WorkflowWorkerService",
    "agent",
    "ask",
    "approve",
    "build_workflow_receipt",
    "redact_secrets",
    "render_prompt",
    "parallel",
    "pipeline",
    "step",
    "workflow",
]

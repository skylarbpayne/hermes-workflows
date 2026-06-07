"""Packaged example workflows for installed quickstarts and dogfood pilots.

Keep workflows here when they should be importable from an installed wheel via a
stable ``hermes_workflows.examples.<module>:<workflow>`` reference. Repo-local
``examples/`` scripts are better for throwaway demos and smoke runners.
"""

from .email_triage import REGISTRY_NAME as EMAIL_TRIAGE_REGISTRY_NAME
from .email_triage import WORKFLOW_REF as EMAIL_TRIAGE_WORKFLOW_REF
from .email_triage import email_triage_workflow
from .trip import trip_planning_workflow

__all__ = [
    "EMAIL_TRIAGE_REGISTRY_NAME",
    "EMAIL_TRIAGE_WORKFLOW_REF",
    "email_triage_workflow",
    "trip_planning_workflow",
]

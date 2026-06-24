"""Fixture-only email triage workflow example.

This packaged example is intentionally safe to run from an installed wheel:
it reads only synthetic/provided fixtures, waits for human approval, and then
writes local proposal files. It never sends, archives, deletes, marks, drafts,
mutates calendar/account state, changes credentials, or touches live cron jobs.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from hermes_workflows import approve, step, workflow, workflow_id

APPROVAL_KEY = "approve_email_triage_packet"
APPROVER = "human:operator"
DEFAULT_DB_ALIAS = "email-triage-demo"
REGISTRY_NAME = "email-triage-demo"
WORKFLOW_REF = "hermes_workflows.examples.email_triage:email_triage_workflow"
MAX_PROVIDED_THREADS = 10

_LOCAL_PROPOSAL_FILES = {
    "triage_packet": "triage-packet.json",
    "kanban_proposal": "kanban-proposal.md",
    "skyvault_proposal": "skyvault-proposal.md",
    "side_effect_ledger": "side-effect-ledger.json",
}

_FORBIDDEN_LIVE_ACTIONS = [
    "send/archive/delete/mark email",
    "create/delete Gmail drafts",
    "calendar/account/security/payment/credential changes",
    "external HTTP requests or cron mutations",
]
_ALLOWED_SIGNALS = frozenset(
    {
        "asks_for_response",
        "has_clear_next_step",
        "mentions_active_work",
        "belongs_in_kanban_context",
        "no_action_needed",
        "archive_candidate",
        "ignore",
        "auth_blocked",
        "human_decision",
    }
)

_POLICY = {
    "mode": "fixture_only_demo",
    "allowed_after_approval": [
        "write local triage packet artifact",
        "write local Kanban proposal artifact",
        "write local Skyvault proposal artifact",
        "write local side-effect ledger artifact",
    ],
    "forbidden_live_actions": _FORBIDDEN_LIVE_ACTIONS,
}

_SYNTHETIC_THREADS = [
    {
        "handle": "fixture:gmail:synthetic:001",
        "account": "demo-fixture",
        "sender_label": "known collaborator",
        "subject_label": "reply request",
        "signals": ["asks_for_response", "has_clear_next_step"],
    },
    {
        "handle": "fixture:gmail:synthetic:002",
        "account": "demo-fixture",
        "sender_label": "ops service",
        "subject_label": "status update for active project",
        "signals": ["mentions_active_work", "belongs_in_kanban_context"],
    },
    {
        "handle": "fixture:gmail:synthetic:003",
        "account": "demo-fixture",
        "sender_label": "newsletter/vendor",
        "subject_label": "low-attention notification",
        "signals": ["no_action_needed", "archive_candidate"],
    },
    {
        "handle": "fixture:gmail:synthetic:004",
        "account": "demo-fixture",
        "sender_label": "automated receipt",
        "subject_label": "FYI receipt",
        "signals": ["ignore", "no_action_needed"],
    },
]


def empty_side_effect_ledger() -> dict[str, int]:
    return {
        "email_mutations": 0,
        "gmail_draft_mutations": 0,
        "calendar_mutations": 0,
        "account_payment_or_auth_mutations": 0,
        "external_http_requests": 0,
        "local_artifacts_written": 0,
    }


def dangerous_side_effects_zero(ledger: dict[str, int]) -> bool:
    return all(value == 0 for key, value in ledger.items() if key != "local_artifacts_written")


@step
async def fetch_email_triage_candidates(inputs: dict[str, Any]) -> dict[str, Any]:
    """Return bounded redacted candidate handles; never fetch live mail by default."""

    fixture = inputs.get("fixture", "synthetic")
    if fixture == "synthetic":
        threads = list(_SYNTHETIC_THREADS)
        source = {
            "kind": "synthetic_fixture",
            "fixture": "email-triage-demo-v1",
            "raw_private_email_included": False,
            "handles_redacted": True,
        }
    elif fixture == "provided":
        threads, source = _coerce_provided_threads(inputs.get("threads"), provided_count=inputs.get("_provided_count"))
    else:
        raise ValueError("email triage demo only accepts fixture='synthetic' or fixture='provided'")

    return {
        "source": source,
        "candidate_threads": [_redacted_thread(thread) for thread in threads],
        "candidate_count": len(threads),
        "local_writeback_paths": _local_writeback_paths(inputs["output_dir"]),
        "account_health": {
            "mode": "fixture",
            "gmail_auth_checked": False,
            "safe_for_live_mail": False,
            "note": "Fixture demo run; no Gmail reads or auth changes performed.",
        },
    }


@step
async def classify_email_triage_candidates(candidate_packet: dict[str, Any]) -> dict[str, Any]:
    classifications = []
    for thread in candidate_packet["candidate_threads"]:
        classification = _classify(thread)
        classifications.append(
            {
                "handle": thread["handle"],
                "classification": classification,
                "reason": _classification_reason(classification),
                "proposed_local_writeback": _proposed_writeback(classification),
                "dangerous_side_effects_required": [],
            }
        )

    counts = Counter(item["classification"] for item in classifications)
    ordered_counts = {
        "total": len(classifications),
        "ignore": counts.get("ignore", 0),
        "archive_candidate": counts.get("archive_candidate", 0),
        "draft_reply": counts.get("draft_reply", 0),
        "kanban_update": counts.get("kanban_update", 0),
        "human_decision": counts.get("human_decision", 0),
        "auth_blocked": counts.get("auth_blocked", 0),
    }
    return {
        **candidate_packet,
        "candidate_counts": ordered_counts,
        "classifications": classifications,
        "side_effect_ledger": empty_side_effect_ledger(),
        "policy": dict(_POLICY),
    }


@step
async def render_email_triage_approval_packet(classified_packet: dict[str, Any]) -> dict[str, Any]:
    counts = classified_packet["candidate_counts"]
    return {
        "title": "Email triage demo approval packet",
        "summary": _counts_summary(counts),
        "approval_key": APPROVAL_KEY,
        "candidate_counts": counts,
        "classifications": classified_packet["classifications"],
        "source_handles": [thread["handle"] for thread in classified_packet["candidate_threads"]],
        "source": classified_packet["source"],
        "local_writeback_paths": classified_packet["local_writeback_paths"],
        "account_health": classified_packet["account_health"],
        "policy": classified_packet["policy"],
        "side_effect_ledger": classified_packet["side_effect_ledger"],
        "dangerous_side_effects_zero": dangerous_side_effects_zero(classified_packet["side_effect_ledger"]),
        "raw_private_email_included": False,
        "requested_decision": "Approve local proposal artifacts only; no email/archive/delete/draft/calendar/account/payment/credential actions.",
    }


@step
async def perform_email_triage_demo_writebacks(
    approval_packet: dict[str, Any],
    decision: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    if decision.get("action") != "approve":
        return {
            "approved": False,
            "approval_key": APPROVAL_KEY,
            "decision": decision,
            "side_effect_ledger": approval_packet["side_effect_ledger"],
            "created_or_updated_paths": {},
        }

    output_dir = Path(inputs["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    ledger = empty_side_effect_ledger()
    paths = {name: Path(path) for name, path in _local_writeback_paths(output_dir).items()}
    ledger["local_artifacts_written"] = len(paths)

    receipt_packet = {
        "workflow_id": workflow_id(),
        "workflow_ref": WORKFLOW_REF,
        "registry_name": inputs.get("_registry_name") or REGISTRY_NAME,
        "db_alias": inputs.get("db_alias") or DEFAULT_DB_ALIAS,
        "source": approval_packet["source"],
        "source_handles": approval_packet["source_handles"],
        "candidate_counts": approval_packet["candidate_counts"],
        "classifications": approval_packet["classifications"],
        "approval": {
            "key": APPROVAL_KEY,
            "action": decision.get("action"),
            "by": decision.get("by"),
            "source": decision.get("source"),
            "note": decision.get("note"),
        },
        "policy": approval_packet["policy"],
        "side_effect_ledger": ledger,
        "dangerous_side_effects_zero": dangerous_side_effects_zero(ledger),
        "created_or_updated_paths": {name: str(path) for name, path in paths.items()},
    }

    _write_json(paths["triage_packet"], receipt_packet)
    paths["kanban_proposal"].write_text(_kanban_proposal(receipt_packet), encoding="utf-8")
    paths["skyvault_proposal"].write_text(_skyvault_proposal(receipt_packet), encoding="utf-8")
    _write_json(paths["side_effect_ledger"], ledger)

    return {
        "approved": True,
        "approved_by": decision.get("by"),
        "approval_key": APPROVAL_KEY,
        "approval_source": decision.get("source"),
        "workflow_id": workflow_id(),
        "workflow_ref": WORKFLOW_REF,
        "registry_name": receipt_packet["registry_name"],
        "db_alias": receipt_packet["db_alias"],
        "source_handles": receipt_packet["source_handles"],
        "candidate_counts": receipt_packet["candidate_counts"],
        "classifications": receipt_packet["classifications"],
        "side_effect_ledger": ledger,
        "created_or_updated_paths": receipt_packet["created_or_updated_paths"],
        "dangerous_side_effects_zero": dangerous_side_effects_zero(ledger),
    }


@workflow
async def email_triage_workflow(inputs: dict[str, Any]) -> dict[str, Any]:
    candidates = await fetch_email_triage_candidates(inputs)
    classified = await classify_email_triage_candidates(candidates)
    packet = await render_email_triage_approval_packet(classified)
    decision = await approve(
        (
            "Approve email triage demo local proposal files? "
            f"{_counts_summary(packet['candidate_counts'])}; "
            f"classifications={_classification_summary(packet['classifications'])}"
        ),
        key=APPROVAL_KEY,
        artifact=packet,
        approver=APPROVER,
        allowed=["approve", "reject"],
        authority=["local_email_triage_proposal_writebacks_only"],
    )
    return await perform_email_triage_demo_writebacks(packet, decision, inputs)


setattr(
    email_triage_workflow,
    "__workflow_input_sanitizer__",
    lambda inputs, *, workflow_id=None: sanitize_email_triage_inputs(inputs, workflow_id=workflow_id),
)


def sanitize_email_triage_inputs(inputs: Any, *, workflow_id: str | None = None) -> dict[str, Any]:
    if not isinstance(inputs, dict):
        raise ValueError("email triage demo inputs must be a mapping")
    requested_fixture = inputs.get("fixture", "synthetic")
    fixture = requested_fixture if requested_fixture in {"synthetic", "provided"} else "synthetic"
    sanitized: dict[str, Any] = {"fixture": fixture, "approver": APPROVER}
    sanitized["output_dir"] = _resolve_output_dir(inputs.get("output_dir"), workflow_id=workflow_id)
    db_alias = _safe_optional_text(inputs.get("db_alias"))
    if db_alias:
        sanitized["db_alias"] = db_alias
    registry_name = _safe_optional_text(inputs.get("_registry_name"))
    sanitized["_registry_name"] = registry_name or REGISTRY_NAME
    if isinstance(inputs.get("_source"), dict):
        sanitized["_source"] = _sanitize_source_provenance(inputs["_source"])
    if fixture == "provided":
        threads, _source = _coerce_provided_threads(inputs.get("threads"))
        sanitized["threads"] = threads
        sanitized["_provided_count"] = len(inputs.get("threads") or []) if isinstance(inputs.get("threads"), list) else 0
    return sanitized


def _safe_optional_text(value: Any) -> str | None:
    if not isinstance(value, str) or _looks_private(value):
        return None
    return value


def _workflow_slug(workflow_id: str | None) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(workflow_id or "workflow").strip().lower()).strip("-")
    return slug or "workflow"


def _safe_relative_output_root(value: Any) -> Path | None:
    if not isinstance(value, str) or _looks_private(value):
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or "~" in candidate.parts:
        return None
    if not candidate.parts or candidate.parts[0] != "dist" or any(part.startswith(".") for part in candidate.parts):
        return None
    if not candidate.parts:
        return None
    return candidate


def _resolve_output_dir(value: Any, *, workflow_id: str | None) -> str:
    root = _safe_relative_output_root(value) or Path(_default_output_root())
    return str(root / _workflow_slug(workflow_id))


def _local_writeback_paths(output_dir: str | Path) -> dict[str, str]:
    root = Path(output_dir)
    return {name: str(root / filename) for name, filename in _LOCAL_PROPOSAL_FILES.items()}


_SAFE_SOURCE_PROVENANCE_KEYS = frozenset({"kind", "id", "task_id", "message_id", "event_id", "channel", "message_url"})


def _sanitize_source_provenance(source: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key in sorted(_SAFE_SOURCE_PROVENANCE_KEYS):
        value = _safe_optional_text(source.get(key))
        if value:
            sanitized[key] = value
    return sanitized


def _redact_private_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_private_values(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_redact_private_values(item) for item in value]
    if isinstance(value, str) and _looks_private(value):
        return "[REDACTED]"
    return value


def _looks_private(value: str) -> bool:
    lowered = value.lower()
    return "@" in value or any(marker in lowered for marker in ("secret", "password", "token", "credential", "raw_"))


def _default_output_root() -> str:
    return f"dist/email-triage-demo-{date.today().isoformat()}"


def _coerce_provided_threads(value: Any, *, provided_count: Any = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("provided email triage fixture must include a threads list")
    bounded_items = [item for item in value if isinstance(item, dict)][:MAX_PROVIDED_THREADS]
    threads = [_redacted_thread(item, index=index + 1, preserve_labels=False) for index, item in enumerate(bounded_items)]
    raw_provided_count = provided_count if isinstance(provided_count, int) else len(value)
    source = {
        "kind": "provided_fixture",
        "raw_private_email_included": False,
        "handles_redacted": True,
        "max_candidate_count": MAX_PROVIDED_THREADS,
        "provided_count": raw_provided_count,
        "bounded_count": len(threads),
    }
    return threads, source


def _redacted_thread(thread: dict[str, Any], *, index: int | None = None, preserve_labels: bool = True) -> dict[str, Any]:
    if preserve_labels:
        handle = str(thread.get("handle") or "fixture:gmail:synthetic:unknown")
        account = str(thread.get("account") or "redacted-account")
        sender_label = str(thread.get("sender_label") or "redacted-sender")
        subject_label = str(thread.get("subject_label") or "redacted-subject")
    else:
        safe_index = index or 0
        handle = f"fixture:gmail:provided:{safe_index:03d}"
        account = "redacted-account"
        sender_label = f"provided-sender-{safe_index:03d}"
        subject_label = f"provided-subject-{safe_index:03d}"
    signals = [str(item) for item in thread.get("signals", []) if str(item) in _ALLOWED_SIGNALS]
    return {
        "handle": handle,
        "account": account,
        "sender_label": sender_label,
        "subject_label": subject_label,
        "signals": signals,
    }


def _classify(thread: dict[str, Any]) -> str:
    signals = set(thread.get("signals") or [])
    if "asks_for_response" in signals:
        return "draft_reply"
    if "belongs_in_kanban_context" in signals or "mentions_active_work" in signals:
        return "kanban_update"
    if "archive_candidate" in signals:
        return "archive_candidate"
    if "auth_blocked" in signals:
        return "auth_blocked"
    if "human_decision" in signals:
        return "human_decision"
    return "ignore"


def _classification_reason(classification: str) -> str:
    return {
        "draft_reply": "Candidate appears to need a response; demo proposes a local draft outline only.",
        "kanban_update": "Candidate contains active-work context; demo proposes a local Kanban comment/task note only.",
        "archive_candidate": "Candidate appears low-attention, but demo records only the candidate and performs no archive.",
        "ignore": "Candidate does not need attention; demo records suppression only.",
        "human_decision": "Candidate needs operator judgment; demo records the ask without external action.",
        "auth_blocked": "Candidate cannot be processed safely without auth/context; demo records blocker only.",
    }[classification]


def _proposed_writeback(classification: str) -> str:
    return {
        "draft_reply": "local_draft_reply_outline",
        "kanban_update": "local_kanban_update_proposal",
        "archive_candidate": "local_archive_candidate_note_no_archive",
        "ignore": "local_suppression_note",
        "human_decision": "local_decision_prompt",
        "auth_blocked": "local_auth_blocker_note_no_auth_change",
    }[classification]


def _counts_summary(counts: dict[str, int]) -> str:
    ordered = ["total", "draft_reply", "kanban_update", "archive_candidate", "ignore", "human_decision", "auth_blocked"]
    return ", ".join(f"{key}={counts.get(key, 0)}" for key in ordered)


def _classification_summary(classifications: list[dict[str, Any]]) -> str:
    return ",".join(str(item.get("classification") or "unknown") for item in classifications)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _kanban_proposal(receipt: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Kanban proposal — email triage demo",
            "",
            f"Workflow: {receipt['workflow_id']}",
            f"Approval: {receipt['approval']['key']} by {receipt['approval']['by']}",
            f"Counts: {_counts_summary(receipt['candidate_counts'])}",
            "",
            "No Kanban rows were mutated by this demo run.",
            "Proposed writebacks:",
            *[
                f"- {item['handle']}: {item['classification']} — {item['proposed_local_writeback']}"
                for item in receipt["classifications"]
            ],
            "",
        ]
    )


def _skyvault_proposal(receipt: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Skyvault proposal — email triage demo",
            "",
            "No Skyvault files were changed by this demo run.",
            f"Workflow: {receipt['workflow_id']}",
            f"Source handles: {', '.join(receipt['source_handles'])}",
            f"Dangerous side effects zero: {receipt['dangerous_side_effects_zero']}",
            "",
        ]
    )

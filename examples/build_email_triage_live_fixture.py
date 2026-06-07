from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from hermes_workflows.examples.email_triage import MAX_PROVIDED_THREADS

RunFn = Callable[..., subprocess.CompletedProcess[str]]

_RESPONSE_KEYWORDS = (
    "confirm",
    "respond",
    "reply",
    "question",
    "can you",
    "could you",
    "please review",
    "approval",
    "approve",
    "rsvp",
    "schedule",
    "available",
    "decision",
)
_PROJECT_KEYWORDS = (
    "receipt",
    "invoice",
    "order",
    "shipment",
    "delivery",
    "payment",
    "refund",
    "statement",
    "waiver",
    "submission",
    "application",
    "deadline",
)
_LOW_ATTENTION_KEYWORDS = (
    "newsletter",
    "digest",
    "promotion",
    "sale",
    "unsubscribe",
    "weekly update",
    "marketing",
)
_LOW_ATTENTION_SENDERS = ("newsletter", "noreply", "no-reply", "notifications")


def infer_triage_signals(thread: dict[str, Any]) -> list[str]:
    """Infer symbolic triage signals from Gmail search metadata.

    The returned signals are intentionally coarse. The live snapshot builder reads
    only Gmail search results and never persists raw sender, subject, snippet, or
    body content into the workflow input.
    """

    subject = str(thread.get("subject") or "").lower()
    sender = str(thread.get("from") or "").lower()
    labels = {str(label).upper() for label in thread.get("labels") or []}
    text = f"{subject} {sender}"

    if any(keyword in text for keyword in _RESPONSE_KEYWORDS):
        return ["asks_for_response", "has_clear_next_step"]
    if any(keyword in text for keyword in _PROJECT_KEYWORDS):
        return ["mentions_active_work", "belongs_in_kanban_context"]
    if "CATEGORY_PROMOTIONS" in labels or any(keyword in text for keyword in _LOW_ATTENTION_KEYWORDS):
        return ["no_action_needed", "archive_candidate"]
    if any(marker in sender for marker in _LOW_ATTENTION_SENDERS):
        return ["no_action_needed", "archive_candidate"]
    return ["no_action_needed"]


def build_live_fixture(
    *,
    accounts: Sequence[str],
    query: str,
    max_per_account: int,
    run: RunFn = subprocess.run,
    gog_command: str = "palmer-gog",
    timeout: int = 90,
) -> dict[str, Any]:
    if not accounts:
        raise ValueError("at least one --account is required")
    if max_per_account < 1:
        raise ValueError("--max-per-account must be positive")

    redacted_threads: list[dict[str, Any]] = []
    returned_threads = 0
    account_count = 0
    for account_index, account in enumerate(accounts, start=1):
        account_count += 1
        raw_threads = _search_account(
            account=account,
            query=query,
            max_per_account=max_per_account,
            run=run,
            gog_command=gog_command,
            timeout=timeout,
        )
        returned_threads += len(raw_threads)
        for thread in raw_threads:
            if len(redacted_threads) >= MAX_PROVIDED_THREADS:
                continue
            item_index = len(redacted_threads) + 1
            redacted_threads.append(
                {
                    "handle": f"fixture:gmail:live:{item_index:03d}",
                    "account": f"gmail-account-{account_index:03d}",
                    "sender_label": f"live-sender-{item_index:03d}",
                    "subject_label": f"live-subject-{item_index:03d}",
                    "signals": infer_triage_signals(thread),
                }
            )

    return {
        "fixture": "provided",
        "threads": redacted_threads,
        "_source": {
            "kind": "gmail_live_snapshot",
            "id": "bounded-redacted-gmail-search",
        },
        "summary": {
            "account_count": account_count,
            "query": query,
            "requested_max_per_account": max_per_account,
            "returned_threads": returned_threads,
            "bounded_threads": len(redacted_threads),
            "raw_private_email_included": False,
            "email_mutations": 0,
            "gmail_draft_mutations": 0,
        },
    }


def _search_account(
    *,
    account: str,
    query: str,
    max_per_account: int,
    run: RunFn,
    gog_command: str,
    timeout: int,
) -> list[dict[str, Any]]:
    cmd = [
        gog_command,
        "gmail",
        "search",
        query,
        "--account",
        account,
        "--json",
        "--max",
        str(max_per_account),
        "--no-input",
    ]
    result = run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = _first_line(result.stderr or result.stdout) or "gog gmail search failed"
        raise RuntimeError(f"gog gmail search failed for account #{_safe_account_number(account)}: {detail}")
    payload = json.loads(result.stdout or "{}")
    threads = _threads_from_payload(payload)
    return [thread for thread in threads if isinstance(thread, dict)]


def _threads_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        if isinstance(payload.get("threads"), list):
            return payload["threads"]
        if isinstance(payload.get("messages"), list):
            return payload["messages"]
        if isinstance(payload.get("items"), list):
            return payload["items"]
        return []
    if isinstance(payload, list):
        return payload
    return []


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


def _safe_account_number(account: str) -> str:
    # Avoid reflecting raw email addresses in errors or CLI output.
    return str(abs(hash(account)) % 10000).zfill(4)


def main(argv: list[str] | None = None, *, run: RunFn = subprocess.run) -> int:
    parser = argparse.ArgumentParser(
        description="Build a bounded redacted live-Gmail fixture for hermes_workflows.examples.email_triage."
    )
    parser.add_argument("--account", action="append", required=True, help="Gmail account to read via gog; may be repeated.")
    parser.add_argument("--query", default="newer_than:2d in:inbox", help="Gmail search query; default is bounded recent Inbox mail.")
    parser.add_argument("--max-per-account", type=int, default=5, help="Maximum Gmail search results to inspect per account.")
    parser.add_argument("--out", type=Path, required=True, help="Path to write workflow input JSON.")
    parser.add_argument("--gog-command", default="palmer-gog", help="Profile-aware gog wrapper/command to execute.")
    args = parser.parse_args(argv)

    fixture = build_live_fixture(
        accounts=args.account,
        query=args.query,
        max_per_account=args.max_per_account,
        run=run,
        gog_command=args.gog_command,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "summary": fixture["summary"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

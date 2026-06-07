from __future__ import annotations

import json
import subprocess
from pathlib import Path

from examples.build_email_triage_live_fixture import (
    build_live_fixture,
    infer_triage_signals,
    main,
)
from hermes_workflows.examples.email_triage import MAX_PROVIDED_THREADS


GOG_SEARCH_PAYLOAD = {
    "threads": [
        {
            "id": "thread-private-123",
            "date": "2026-06-07T10:00:00Z",
            "from": "Real Person <private.person@example.com>",
            "subject": "Can you confirm the revised schedule?",
            "labels": ["INBOX", "IMPORTANT"],
        },
        {
            "id": "thread-private-456",
            "date": "2026-06-07T11:00:00Z",
            "from": "newsletter@example.com",
            "subject": "Weekly newsletter with secret project name",
            "labels": ["CATEGORY_PROMOTIONS", "INBOX"],
        },
    ]
}


def test_infer_triage_signals_prefers_response_and_low_attention_paths():
    assert infer_triage_signals(
        {"subject": "Can you confirm this works?", "from": "person@example.com", "labels": ["INBOX"]}
    ) == ["asks_for_response", "has_clear_next_step"]
    assert infer_triage_signals(
        {"subject": "Weekly digest", "from": "newsletter@example.com", "labels": ["CATEGORY_PROMOTIONS"]}
    ) == ["no_action_needed", "archive_candidate"]


def test_build_live_fixture_uses_gog_search_only_and_redacts_raw_gmail_values(tmp_path: Path):
    calls: list[list[str]] = []

    def fake_run(cmd, *, capture_output, text, timeout, check):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(GOG_SEARCH_PAYLOAD), stderr="")

    fixture = build_live_fixture(
        accounts=["private.person@example.com"],
        query="newer_than:2d in:inbox",
        max_per_account=2,
        run=fake_run,
        gog_command="palmer-gog",
    )

    assert calls == [
        [
            "palmer-gog",
            "gmail",
            "search",
            "newer_than:2d in:inbox",
            "--account",
            "private.person@example.com",
            "--json",
            "--max",
            "2",
            "--no-input",
        ]
    ]
    rendered = json.dumps(fixture, sort_keys=True)
    assert "private.person@example.com" not in rendered
    assert "Real Person" not in rendered
    assert "Can you confirm" not in rendered
    assert "thread-private" not in rendered
    assert fixture["fixture"] == "provided"
    assert fixture["_source"] == {
        "kind": "gmail_live_snapshot",
        "id": "bounded-redacted-gmail-search",
    }
    assert fixture["summary"] == {
        "account_count": 1,
        "query": "newer_than:2d in:inbox",
        "requested_max_per_account": 2,
        "returned_threads": 2,
        "bounded_threads": 2,
        "raw_private_email_included": False,
        "email_mutations": 0,
        "gmail_draft_mutations": 0,
    }
    assert fixture["threads"] == [
        {
            "handle": "fixture:gmail:live:001",
            "account": "gmail-account-001",
            "sender_label": "live-sender-001",
            "subject_label": "live-subject-001",
            "signals": ["asks_for_response", "has_clear_next_step"],
        },
        {
            "handle": "fixture:gmail:live:002",
            "account": "gmail-account-001",
            "sender_label": "live-sender-002",
            "subject_label": "live-subject-002",
            "signals": ["no_action_needed", "archive_candidate"],
        },
    ]


def test_build_live_fixture_bounds_threads_to_packaged_workflow_limit():
    payload = {"threads": [{"id": f"raw-{i}", "subject": "FYI", "from": "noreply@example.com", "labels": []} for i in range(25)]}

    def fake_run(cmd, *, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    fixture = build_live_fixture(
        accounts=["one@example.com", "two@example.com"],
        query="newer_than:2d",
        max_per_account=25,
        run=fake_run,
        gog_command="palmer-gog",
    )

    assert len(fixture["threads"]) == MAX_PROVIDED_THREADS
    assert fixture["summary"]["returned_threads"] == 50
    assert fixture["summary"]["bounded_threads"] == MAX_PROVIDED_THREADS


def test_cli_writes_workflow_input_json_without_raw_private_values(tmp_path: Path):
    out = tmp_path / "input.json"

    def fake_run(cmd, *, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(GOG_SEARCH_PAYLOAD), stderr="")

    rc = main(
        [
            "--account",
            "private.person@example.com",
            "--query",
            "newer_than:2d in:inbox",
            "--out",
            str(out),
            "--gog-command",
            "palmer-gog",
        ],
        run=fake_run,
    )

    assert rc == 0
    written = out.read_text(encoding="utf-8")
    assert "private.person@example.com" not in written
    assert "Real Person" not in written
    assert "Can you confirm" not in written
    assert json.loads(written)["fixture"] == "provided"

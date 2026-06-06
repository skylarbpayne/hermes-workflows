from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from examples.build_hackathon_email_snapshot import build_snapshot
from examples.redact_hackathon_review_packet import public_packet, write_public_packet
from examples.render_hackathon_output_packet import render_packet
from examples.workflows_demo_2026_06_05 import (
    _highlight_python_line,
    run_full_demo,
    render_demo_artifact,
)


class _CodeTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_code = False
        self.code_text = []

    def handle_starttag(self, tag, attrs):
        if tag == "code" and any(name == "class" and value and "language-python" in value for name, value in attrs):
            self.in_code = True

    def handle_endtag(self, tag):
        if tag == "code":
            self.in_code = False

    def handle_data(self, data):
        if self.in_code:
            self.code_text.append(data)


def test_demo_runs_realistic_hackathon_email_workflow_with_agent_and_human_approvals(tmp_path):
    db = tmp_path / "demo.sqlite"
    artifact_path = tmp_path / "artifact" / "index.html"

    receipt = run_full_demo(db_path=db, workflow_id="wf_demo_test", artifact_path=artifact_path)

    assert receipt["final_result"]["status"] == "completed"
    assert receipt["agent_calls"] >= 6
    assert receipt["generated_workflow"]["symbol"] == "participant_email_personalization_workflow"
    assert receipt["approvals"] == [
        "generated_workflow_execution",
        "agent_email_quality_approval",
        "human_email_batch_approval",
    ]
    result = receipt["final_result"]["result"]
    assert result["ready_to_create_drafts"] is True
    assert result["side_effects"] == {"gmail_drafts_created": 0, "emails_sent": 0}
    assert len(result["draft_batch"]["drafts"]) == 4
    winner_draft = next(d for d in result["draft_batch"]["drafts"] if d["participant_email"] == "maya@example.edu")
    assert "Best Use of AI" in winner_draft["body"]
    nonwinner_draft = next(d for d in result["draft_batch"]["drafts"] if d["participant_email"] == "noah@example.edu")
    assert "project showcase" in nonwinner_draft["body"].lower()

    assert artifact_path.exists()
    html = artifact_path.read_text(encoding="utf-8")
    assert "Hackathon Participant Email Command Center" in html
    assert "personalized email drafts" in html.lower()
    assert "Agent approval" in html
    assert "Human approval" in html
    assert "Participant roster" in html
    assert "Project + prize lookup" in html
    assert "Maya Chen" in html
    assert "Best Use of AI" in html
    assert "agent:email_quality_reviewer" in html
    assert "human:skylar" in html
    assert "ApprovalRequested" in html
    assert "audit log" in html.lower()
    assert "Interactive approval walkthrough" in html
    assert "data-approve-index=\"0\"" in html
    assert "Parent workflow code" in html
    assert "workflows_meeting_demo" in html
    assert "participant_email_personalization_workflow" in html
    assert "class=\"code-block language-python\"" in html
    assert "tok kw" in html
    assert '<span <span class="tok kw">class</span>=' not in html
    assert 'class=""tok kw"' not in html
    parser = _CodeTextParser()
    parser.feed(html)
    rendered_code_text = "".join(parser.code_text)
    assert 'class="tok kw"' not in rendered_code_text


def test_python_highlighter_does_not_highlight_inside_its_own_markup():
    highlighted = _highlight_python_line("return {")

    assert highlighted == '<span class="tok kw">return</span> {'
    assert '<span <span class="tok kw">class</span>=' not in highlighted
    assert 'class=""tok kw"' not in highlighted


def test_python_highlighter_keeps_code_strings_as_text_not_markup():
    highlighted = _highlight_python_line('    "class": "tok kw",')

    parser = _CodeTextParser()
    parser.feed(f'<code class="language-python">{highlighted}</code>')
    assert ''.join(parser.code_text) == '    "class": "tok kw",'


def test_demo_artifact_can_be_regenerated_from_existing_db(tmp_path):
    db = tmp_path / "demo.sqlite"
    first_artifact = tmp_path / "first" / "index.html"
    second_artifact = tmp_path / "second" / "index.html"

    run_full_demo(db_path=db, workflow_id="wf_demo_test_regen", artifact_path=first_artifact)
    snapshot = render_demo_artifact(db_path=db, workflow_id="wf_demo_test_regen", artifact_path=second_artifact)

    assert snapshot["workflow_id"] == "wf_demo_test_regen"
    assert snapshot["status"]["status"] == "completed"
    assert second_artifact.exists()
    assert "wf_demo_test_regen" in second_artifact.read_text(encoding="utf-8")


def test_real_snapshot_mode_runs_same_workflow_without_side_effects(tmp_path, monkeypatch):
    registration_csv = tmp_path / "registration.csv"
    registration_csv.write_text(
        "Email Address,Full Name,Withdrawn,Checked In,Have you been to a hackathon before?\n"
        "ada@example.edu,Ada Lovelace,FALSE,TRUE,No\n"
        "grace@example.edu,Grace Hopper,FALSE,TRUE,Yes\n",
        encoding="utf-8",
    )
    submissions_json = tmp_path / "submissions.json"
    submissions_json.write_text(
        json.dumps(
            [
                {
                    "results": [
                        {
                            "contact_email": "ada@example.edu",
                            "project_title": "Compiler Coach",
                            "team_name": "Byte Brigade",
                            "track": "AI",
                            "payload_json": json.dumps(
                                {
                                    "contactEmail": "ada@example.edu",
                                    "projectTitle": "Compiler Coach",
                                    "teamName": "Byte Brigade",
                                    "members": "Ada Lovelace, Grace Hopper",
                                    "description": "An AI tutor that explains compiler errors in plain English.",
                                    "demoLink": "https://example.edu/compiler-coach",
                                }
                            ),
                        }
                    ]
                }
            ]
        ),
        encoding="utf-8",
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = build_snapshot(registration_csv=registration_csv, submissions_json=submissions_json)
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setenv("HERMES_WORKFLOWS_HACKATHON_SNAPSHOT", str(snapshot_path))

    receipt = run_full_demo(db_path=tmp_path / "real.sqlite", workflow_id="wf_real_snapshot_test", artifact_path=tmp_path / "real" / "index.html")

    assert receipt["final_result"]["status"] == "completed"
    result = receipt["final_result"]["result"]
    assert result["side_effects"] == {"gmail_drafts_created": 0, "emails_sent": 0}
    assert len(result["draft_batch"]["drafts"]) == 2
    assert {draft["project_name"] for draft in result["draft_batch"]["drafts"]} == {"Compiler Coach"}
    assert snapshot["stats"]["unique_active_participants"] == 2
    assert snapshot["stats"]["participants_without_project_match"] == 0


def test_redacted_public_packet_omits_private_participant_and_project_content(tmp_path, monkeypatch):
    registration_csv = tmp_path / "registration.csv"
    registration_csv.write_text(
        "Email Address,Full Name,Withdrawn,Checked In,Have you been to a hackathon before?\n"
        "ada@example.edu,Ada Lovelace,FALSE,TRUE,No\n"
        "grace@example.edu,Grace Hopper,FALSE,TRUE,Yes\n",
        encoding="utf-8",
    )
    submissions_json = tmp_path / "submissions.json"
    submissions_json.write_text(
        json.dumps(
            [
                {
                    "results": [
                        {
                            "contact_email": "ada@example.edu",
                            "project_title": "Compiler Coach",
                            "team_name": "Byte Brigade",
                            "track": "AI",
                            "payload_json": json.dumps(
                                {
                                    "contactEmail": "ada@example.edu",
                                    "projectTitle": "Compiler Coach",
                                    "teamName": "Byte Brigade",
                                    "members": "Ada Lovelace, Grace Hopper",
                                    "description": "An AI tutor that explains compiler errors in plain English.",
                                    "demoLink": "https://example.edu/compiler-coach",
                                }
                            ),
                        }
                    ]
                }
            ]
        ),
        encoding="utf-8",
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = build_snapshot(registration_csv=registration_csv, submissions_json=submissions_json)
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setenv("HERMES_WORKFLOWS_HACKATHON_SNAPSHOT", str(snapshot_path))
    receipt = run_full_demo(db_path=tmp_path / "real.sqlite", workflow_id="wf_redaction_test", artifact_path=tmp_path / "private" / "index.html")

    packet = public_packet(snapshot=snapshot, receipt=receipt)
    packet_text = json.dumps(packet, sort_keys=True)
    for private_value in ["Ada Lovelace", "Grace Hopper", "ada@example.edu", "grace@example.edu", "Compiler Coach", "Byte Brigade", "example.edu/compiler-coach"]:
        assert private_value not in packet_text
    assert packet["workflow"]["side_effects"] == {"gmail_drafts_created": 0, "emails_sent": 0}
    assert packet["coverage"]["drafts"][0]["raw_body_omitted"] is True
    assert packet["coverage"]["participants"][0]["participant_ref"] == "Participant 001"

    out_dir = tmp_path / "public-packet"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    write_public_packet(snapshot_path=snapshot_path, receipt_path=receipt_path, out_dir=out_dir)
    html_text = (out_dir / "index.html").read_text(encoding="utf-8")
    json_text = (out_dir / "packet.json").read_text(encoding="utf-8")

    rendered_html = tmp_path / "rendered" / "index.html"
    rendered_json = tmp_path / "rendered" / "summary.json"
    render_packet(snapshot_path=snapshot_path, receipt_path=receipt_path, out_path=rendered_html, summary_json_path=rendered_json)
    html_text += rendered_html.read_text(encoding="utf-8")
    json_text += rendered_json.read_text(encoding="utf-8")

    assert "Hack the Valley /workflows dry run, redacted" in html_text
    assert "Gmail drafts" in html_text
    assert '"body":' not in json_text
    assert "raw_body_omitted" in json_text
    for private_value in ["Ada Lovelace", "Grace Hopper", "ada@example.edu", "grace@example.edu", "Compiler Coach", "Byte Brigade", "example.edu/compiler-coach"]:
        assert private_value not in html_text
        assert private_value not in json_text

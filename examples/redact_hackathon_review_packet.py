from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def public_packet(*, snapshot: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Build a public-safe packet from a private Hack the Valley dry run.

    The private snapshot/review packet may contain participant names, emails,
    project names, repo URLs, demo URLs, and draft bodies. This packet keeps the
    execution evidence and review shape while replacing all participant/project
    content with stable local labels.
    """
    participants = list(snapshot.get("participants") or [])
    projects = list(snapshot.get("projects") or [])
    prizes = list(snapshot.get("prizes") or [])
    final = receipt.get("final_result") or {}
    result = final.get("result") or {}
    drafts = list((result.get("draft_batch") or {}).get("drafts") or [])
    side_effects = result.get("side_effects") or {}

    project_labels = _project_labels(projects, participants)
    participant_labels = _participant_labels(participants)
    participant_project_refs = {
        str(participant.get("participant_id") or ""): project_labels.get(str(participant.get("project_id") or ""), "Unmatched project")
        for participant in participants
    }

    return {
        "kind": "hackathon_email_public_review_packet.v1",
        "source": "redacted from a private Hack the Valley snapshot dry run",
        "privacy": {
            "classification": "public-safe redacted derivative",
            "omitted": [
                "participant names",
                "participant email addresses",
                "raw project titles and descriptions",
                "team/member names",
                "repo/demo URLs",
                "raw generated email body text",
                "raw workflow event payloads",
                "input file paths",
            ],
            "kept": [
                "counts",
                "approval keys",
                "side-effect counts",
                "generated workflow symbol and hash",
                "draft coverage shape",
                "review blockers",
            ],
        },
        "inputs": {
            "stats": snapshot.get("stats") or {},
            "input_hashes": snapshot.get("input_hashes") or {},
        },
        "workflow": {
            "workflow_id": receipt.get("workflow_id"),
            "status": final.get("status"),
            "event_count": receipt.get("event_count"),
            "agent_calls": receipt.get("agent_calls"),
            "approvals": receipt.get("approvals") or [],
            "generated_workflow": _generated_workflow_summary(receipt.get("generated_workflow") or {}),
            "side_effects": {
                "gmail_drafts_created": int(side_effects.get("gmail_drafts_created") or 0),
                "emails_sent": int(side_effects.get("emails_sent") or 0),
            },
        },
        "coverage": {
            "participants": [_redact_participant(p, i, participant_labels, project_labels) for i, p in enumerate(participants, start=1)],
            "projects": [_redact_project(p, i, project_labels, prizes) for i, p in enumerate(projects, start=1)],
            "drafts": [_redact_draft(d, i, participant_labels, participant_project_refs) for i, d in enumerate(drafts, start=1)],
            "quality_review": _redact_quality_review(result.get("quality_review") or {}),
        },
        "review_findings": _review_findings(snapshot=snapshot, drafts=drafts, side_effects=side_effects),
        "next_gates": [
            "Organizer fixes unmatched participant/project rows before project-specific messaging.",
            "Reviewed prize/winner data must be supplied before prize claims appear in drafts.",
            "Gmail draft creation requires explicit human approval after packet review.",
            "Sending/scheduling requires a separate explicit approval gate.",
        ],
    }


def render_public_html(packet: dict[str, Any], out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stats = packet["inputs"]["stats"]
    workflow = packet["workflow"]
    findings = packet["review_findings"]
    data_json = json.dumps(packet, indent=2, sort_keys=True).replace("</", "<\\/")
    cards = [
        ("Participants drafted", stats.get("unique_active_participants", len(packet["coverage"]["participants"]))),
        ("Project submissions", stats.get("submission_rows")),
        ("Unmatched rows", stats.get("participants_without_project_match")),
        ("Agent calls", workflow.get("agent_calls")),
        ("Audit events", workflow.get("event_count")),
        ("Gmail drafts", workflow.get("side_effects", {}).get("gmail_drafts_created")),
        ("Emails sent", workflow.get("side_effects", {}).get("emails_sent")),
    ]
    card_html = "".join(
        f'<article class="card"><div class="label">{html.escape(str(label))}</div><div class="value">{html.escape(str(value))}</div></article>'
        for label, value in cards
    )
    approval_html = "".join(f"<li><code>{html.escape(str(item))}</code></li>" for item in workflow.get("approvals", []))
    finding_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in findings)
    draft_rows = "".join(
        "<tr>"
        f"<td>{html.escape(d['draft_ref'])}</td>"
        f"<td>{html.escape(d['participant_ref'])}</td>"
        f"<td>{html.escape(d['project_ref'])}</td>"
        f"<td>{html.escape(str(d['prize_claim_count']))}</td>"
        f"<td>{html.escape(', '.join(d['risk_flags']) or 'none')}</td>"
        "</tr>"
        for d in packet["coverage"]["drafts"][:40]
    )
    source_hash = workflow.get("generated_workflow", {}).get("source_sha256", "")
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Hack the Valley /workflows redacted dry-run packet</title>
  <style>
    :root {{ color-scheme: dark; --bg:#081018; --panel:#101a26; --line:#26384f; --text:#f5f8ff; --muted:#9fb0c7; --accent:#7dd3fc; --ok:#86efac; --warn:#fbbf24; }}
    * {{ box-sizing: border-box; }} body {{ margin: 0; font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at top left, #13253a, var(--bg) 45%); color: var(--text); }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 42px 18px 64px; }}
    .eyebrow {{ color: var(--accent); text-transform: uppercase; letter-spacing: .12em; font-size: 12px; font-weight: 700; }}
    h1 {{ font-size: clamp(32px, 6vw, 68px); line-height: .95; margin: 10px 0 18px; }}
    p {{ color: var(--muted); max-width: 820px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 28px 0; }}
    .card, section {{ background: color-mix(in srgb, var(--panel) 92%, transparent); border: 1px solid var(--line); border-radius: 18px; padding: 18px; box-shadow: 0 20px 80px rgba(0,0,0,.24); }}
    .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }} .value {{ font-size: 32px; font-weight: 800; margin-top: 6px; }}
    .ok {{ color: var(--ok); }} .warn {{ color: var(--warn); }} code, pre {{ background: #050a12; border: 1px solid #1d2b3d; border-radius: 10px; }} code {{ padding: 2px 6px; }} pre {{ padding: 16px; overflow: auto; max-height: 440px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }} th, td {{ text-align: left; border-bottom: 1px solid var(--line); padding: 10px 8px; vertical-align: top; }} th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .07em; }}
    section {{ margin-top: 18px; }} .two {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(280px,1fr)); gap: 18px; }}
  </style>
</head>
<body>
<main>
  <div class="eyebrow">Public-safe derivative artifact</div>
  <h1>Hack the Valley /workflows dry run, redacted</h1>
  <p>This packet is generated from the private real Hack the Valley snapshot dry run. It keeps the operational proof — counts, approval gates, generated workflow hash, side-effect receipts, and review blockers — while removing participant names, emails, project titles, raw draft bodies, URLs, and event payloads.</p>
  <div class="grid">{card_html}</div>
  <div class="two">
    <section><h2>Approval gates</h2><ol>{approval_html}</ol><p class="ok">Side effects stayed at zero: no Gmail drafts and no sent emails.</p></section>
    <section><h2>What the run surfaced</h2><ul>{finding_html}</ul></section>
  </div>
  <section><h2>Generated workflow receipt</h2><p>Symbol: <code>{html.escape(str(workflow.get('generated_workflow', {}).get('symbol', '')))}</code></p><p>SHA-256: <code>{html.escape(str(source_hash))}</code></p></section>
  <section><h2>Redacted draft coverage</h2><table><thead><tr><th>Draft</th><th>Participant</th><th>Project</th><th>Prize claims</th><th>Risk flags</th></tr></thead><tbody>{draft_rows}</tbody></table></section>
  <section><h2>Machine-readable packet</h2><pre id="packet-json">{html.escape(data_json)}</pre></section>
</main>
<script type="application/json" id="redacted-packet-data">{data_json}</script>
</body>
</html>
"""
    out_path.write_text(body, encoding="utf-8")


def write_public_packet(*, snapshot_path: Path, receipt_path: Path, out_dir: Path) -> dict[str, Any]:
    packet = public_packet(snapshot=load_json(snapshot_path), receipt=load_json(receipt_path))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "packet.json").write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    render_public_html(packet, out_dir / "index.html")
    return packet


def _project_labels(projects: list[dict[str, Any]], participants: list[dict[str, Any]]) -> dict[str, str]:
    labels = {str(project.get("project_id")): f"Project {index:03d}" for index, project in enumerate(projects, start=1)}
    for participant in participants:
        project_id = str(participant.get("project_id") or "")
        if project_id and project_id not in labels:
            labels[project_id] = f"Unmatched project {len([v for v in labels.values() if v.startswith('Unmatched')]) + 1:03d}"
    return labels


def _participant_labels(participants: list[dict[str, Any]]) -> dict[str, str]:
    return {str(participant.get("participant_id")): f"Participant {index:03d}" for index, participant in enumerate(participants, start=1)}


def _redact_participant(participant: dict[str, Any], index: int, participant_labels: dict[str, str], project_labels: dict[str, str]) -> dict[str, Any]:
    participant_id = str(participant.get("participant_id") or f"participant-{index}")
    project_id = str(participant.get("project_id") or "")
    return {
        "participant_ref": participant_labels.get(participant_id, f"Participant {index:03d}"),
        "email_token": _token(participant.get("email_hash") or participant_id),
        "project_ref": project_labels.get(project_id, "Unmatched project"),
        "checked_in": bool(participant.get("checked_in", True)),
        "first_hackathon": bool(participant.get("first_hackathon", False)),
        "track_present": bool(participant.get("track")),
    }


def _redact_project(project: dict[str, Any], index: int, project_labels: dict[str, str], prizes: list[dict[str, Any]]) -> dict[str, Any]:
    project_id = str(project.get("project_id") or f"project-{index}")
    return {
        "project_ref": project_labels.get(project_id, f"Project {index:03d}"),
        "team_member_count": len(project.get("team") or []),
        "has_repo_url": bool(project.get("repo")),
        "has_demo_url": bool(project.get("demo_url")),
        "track_present": bool(project.get("track")),
        "prize_claim_count": len([p for p in prizes if p.get("project_id") == project_id]),
        "raw_content_omitted": True,
    }


def _redact_draft(draft: dict[str, Any], index: int, participant_labels: dict[str, str], participant_project_refs: dict[str, str]) -> dict[str, Any]:
    participant_id = str(draft.get("participant_id") or "")
    body = str(draft.get("body") or "")
    return {
        "draft_ref": f"Draft {index:03d}",
        "participant_ref": participant_labels.get(participant_id, f"Participant {index:03d}"),
        "email_token": _token(draft.get("participant_email") or participant_id),
        "project_ref": participant_project_refs.get(participant_id, "Project redacted"),
        "subject_shape": "Hack the Valley follow-up: [project redacted]",
        "line_count": len(body.splitlines()),
        "has_project_link": "Project link:" in body,
        "has_judge_note": "Judge note:" in body,
        "has_next_step": "Next step:" in body,
        "prize_claim_count": len(draft.get("prize_context") or []),
        "risk_flags": [str(flag) for flag in (draft.get("risk_flags") or [])],
        "raw_body_omitted": True,
    }


def _project_id_from_draft(draft: dict[str, Any], project_labels: dict[str, str]) -> str:
    for prize in draft.get("prize_context") or []:
        project_id = str(prize.get("project_id") or "")
        if project_id:
            return project_id
    # Drafts do not currently carry project_id, so fall back to participant/project
    # coverage in the packet rather than trying to infer from the raw project name.
    return ""


def _redact_quality_review(review: dict[str, Any]) -> dict[str, Any]:
    return {
        "approved": bool(review.get("approved")),
        "reviewer": review.get("reviewer"),
        "checks": [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "detail": _redact_detail(str(item.get("detail") or "")),
            }
            for item in (review.get("checks") or [])
            if isinstance(item, dict)
        ],
        "required_human_review": list(review.get("required_human_review") or []),
    }


def _redact_quality_text(text: str) -> str:
    return _redact_detail(text)


def _redact_detail(text: str) -> str:
    # Current quality-review details are aggregate-only. Keep counts and replace
    # anything that looks like an address defensively.
    if "@" in text:
        return "[redacted detail]"
    return text


def _generated_workflow_summary(generated: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": generated.get("symbol"),
        "source_sha256": generated.get("source_sha256"),
        "approval_key": generated.get("label"),
        "agent_call": generated.get("agent_call"),
    }


def _review_findings(*, snapshot: dict[str, Any], drafts: list[dict[str, Any]], side_effects: dict[str, Any]) -> list[str]:
    stats = snapshot.get("stats") or {}
    findings = [
        f"{len(drafts)} checked-in participant drafts were generated from the snapshot.",
        f"{stats.get('participants_without_project_match', 0)} checked-in participant rows lacked a confident project match.",
        f"{stats.get('prizes', 0)} reviewed prize claims were available for draft personalization.",
        f"Skipped rows stayed explicit: {stats.get('unchecked_rows_skipped', 0)} unchecked, {stats.get('withdrawn_rows_skipped', 0)} withdrawn, {stats.get('duplicate_email_rows_skipped', 0)} duplicate emails.",
        f"External side effects stayed zero: Gmail drafts={int(side_effects.get('gmail_drafts_created') or 0)}, emails sent={int(side_effects.get('emails_sent') or 0)}.",
    ]
    if int(stats.get("participants_without_project_match") or 0):
        findings.append("Organizer data cleanup is required before project-specific follow-up copy is safe.")
    if int(stats.get("prizes") or 0) == 0:
        findings.append("No reviewed prize data was supplied, so prize-specific claims were disabled.")
    return findings


def _token(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "redacted"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return f"redacted-{digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a public-safe redacted packet from a private Hack the Valley /workflows dry run.")
    parser.add_argument("--snapshot", type=Path, required=True, help="Private snapshot.json from the dry run")
    parser.add_argument("--receipt", type=Path, required=True, help="Private receipt.json from the dry run")
    parser.add_argument("--out-dir", type=Path, required=True, help="Directory for index.html and packet.json")
    args = parser.parse_args(argv)

    packet = write_public_packet(snapshot_path=args.snapshot, receipt_path=args.receipt, out_dir=args.out_dir)
    print(json.dumps({
        "out_dir": str(args.out_dir),
        "participants": len(packet["coverage"]["participants"]),
        "drafts": len(packet["coverage"]["drafts"]),
        "side_effects": packet["workflow"]["side_effects"],
        "approvals": packet["workflow"]["approvals"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

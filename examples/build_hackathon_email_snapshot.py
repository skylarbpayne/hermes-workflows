from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def load_registration_csv(path: Path) -> list[dict[str, Any]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def load_d1_submissions_json(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "submissions" in raw:
        rows = raw["submissions"]
    elif isinstance(raw, list) and raw and isinstance(raw[0], dict) and "results" in raw[0]:
        rows = raw[0].get("results") or []
    elif isinstance(raw, list):
        rows = raw
    else:
        raise ValueError("unsupported submissions JSON shape")

    submissions: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload")
        if payload is None and row.get("payload_json"):
            payload = json.loads(row.get("payload_json") or "{}")
        if payload is None:
            payload = row
        submissions.append({"row": row, "payload": payload})
    return submissions


def load_prizes_json(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "prizes" in raw:
        return list(raw["prizes"])
    if isinstance(raw, list):
        return raw
    raise ValueError("unsupported prizes JSON shape")


def normalize_email(value: Any) -> str:
    match = EMAIL_RE.search(str(value or "").strip().lower())
    return match.group(0) if match else ""


def normalize_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "yes", "y", "1", "checked", "x"}


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def member_names(payload: dict[str, Any]) -> list[str]:
    members = payload.get("members") or payload.get("teamMembers") or ""
    if isinstance(members, list):
        names = []
        for item in members:
            if isinstance(item, dict):
                names.append(item.get("name") or item.get("fullName") or "")
            else:
                names.append(str(item))
        return [name for name in names if name]
    return [part.strip() for part in re.split(r"[,;\n]+", str(members)) if part.strip()]


def build_snapshot(*, registration_csv: Path, submissions_json: Path, prizes_json: Path | None = None, include_unchecked: bool = False) -> dict[str, Any]:
    registration_rows = load_registration_csv(registration_csv)
    submission_rows = load_d1_submissions_json(submissions_json)
    prize_rows = load_prizes_json(prizes_json)

    projects: list[dict[str, Any]] = []
    by_email: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for index, item in enumerate(submission_rows, start=1):
        row = item["row"]
        payload = item["payload"]
        contact_email = normalize_email(payload.get("contactEmail") or row.get("contact_email"))
        title = str(payload.get("projectTitle") or row.get("project_title") or f"Submitted project {index}").strip()
        team_name = str(payload.get("teamName") or row.get("team_name") or title).strip()
        project_id = stable_id("project", f"{contact_email}|{team_name}|{title}")
        members = member_names(payload)
        project = {
            "project_id": project_id,
            "name": title,
            "summary": str(payload.get("description") or "Submitted Hack the Valley project.").strip(),
            "repo": str(payload.get("repoLink") or "").strip(),
            "demo_url": str(payload.get("demoLink") or payload.get("website") or payload.get("mediaLink") or "").strip(),
            "team": members,
            "team_name": team_name,
            "track": payload.get("track") or row.get("track") or payload.get("tracks") or "",
            "judges_note": "Real submission context imported from the Hack the Valley submissions snapshot; organizer prize/judging note still needs human review.",
            "contact_email_hash": stable_id("email", contact_email) if contact_email else "",
        }
        projects.append(project)
        if contact_email:
            by_email[contact_email] = project_id
        for name in members:
            by_name[normalize_name(name)] = project_id

    participants: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    duplicate_emails = 0
    withdrawn = 0
    unchecked_skipped = 0
    invalid_emails = 0
    unmatched_project = 0
    for index, row in enumerate(registration_rows, start=1):
        email = normalize_email(row.get("Email Address") or row.get("email"))
        if not email:
            invalid_emails += 1
            continue
        if email in seen_emails:
            duplicate_emails += 1
            continue
        seen_emails.add(email)
        if truthy(row.get("Withdrawn")):
            withdrawn += 1
            continue
        checked_in = truthy(row.get("Checked In"))
        if not checked_in and not include_unchecked:
            unchecked_skipped += 1
            continue
        name = str(row.get("Full Name") or row.get("name") or email.split("@")[0]).strip()
        project_id = by_email.get(email) or by_name.get(normalize_name(name))
        if not project_id:
            project_id = stable_id("unmatched", email)
            unmatched_project += 1
        participants.append(
            {
                "participant_id": stable_id("participant", email),
                "name": name,
                "email": email,
                "email_hash": stable_id("email", email),
                "project_id": project_id,
                "role": "participant",
                "track": row.get("Which area of technology are you most interested in focusing on during the hackathon?") or "",
                "first_hackathon": str(row.get("Have you been to a hackathon before?") or "").strip().lower().startswith("no"),
                "checked_in": checked_in,
                "withdrawn": False,
            }
        )

    prizes = []
    project_by_key = {p["project_id"]: p for p in projects}
    project_by_title = {normalize_name(p["name"]): p for p in projects}
    project_by_team = {normalize_name(p.get("team_name")): p for p in projects if p.get("team_name")}
    for prize in prize_rows:
        if not isinstance(prize, dict):
            continue
        project_id = prize.get("project_id")
        if not project_id:
            key = normalize_name(prize.get("project") or prize.get("project_title") or prize.get("team") or "")
            project = project_by_title.get(key) or project_by_team.get(key)
            project_id = project["project_id"] if project else ""
        if project_id and project_id in project_by_key:
            prizes.append(
                {
                    "project_id": project_id,
                    "prize": str(prize.get("prize") or prize.get("award") or "Prize").strip(),
                    "sponsor": str(prize.get("sponsor") or "Hack the Valley").strip(),
                    "next_step": str(prize.get("next_step") or "Organizer will follow up with prize details.").strip(),
                }
            )

    return {
        "kind": "hackathon_email_snapshot.v1",
        "source": "real snapshot: Hack the Valley registration export",
        "project_source": "real snapshot: Hack the Valley D1 submissions export",
        "prize_source": "reviewed prize JSON" if prizes_json else "no prize JSON supplied; prize-specific claims disabled",
        "privacy_note": "Real participant snapshot for protected internal dry run. Do not commit raw exports or public-share this artifact.",
        "input_hashes": {
            "registration_csv_sha256": file_sha256(registration_csv),
            "submissions_json_sha256": file_sha256(submissions_json),
            "prizes_json_sha256": file_sha256(prizes_json) if prizes_json else None,
        },
        "stats": {
            "registration_rows": len(registration_rows),
            "unique_active_participants": len(participants),
            "withdrawn_rows_skipped": withdrawn,
            "unchecked_rows_skipped": unchecked_skipped,
            "duplicate_email_rows_skipped": duplicate_emails,
            "invalid_email_rows_skipped": invalid_emails,
            "submission_rows": len(submission_rows),
            "projects": len(projects),
            "participants_without_project_match": unmatched_project,
            "prizes": len(prizes),
        },
        "participants": participants,
        "projects": projects,
        "prizes": prizes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a /workflows hackathon email snapshot from local read-only exports.")
    parser.add_argument("--registration-csv", type=Path, required=True)
    parser.add_argument("--submissions-json", type=Path, required=True)
    parser.add_argument("--prizes-json", type=Path)
    parser.add_argument("--include-unchecked", action="store_true", help="Include active registrants who did not check in. Default is checked-in participants only.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    snapshot = build_snapshot(
        registration_csv=args.registration_csv,
        submissions_json=args.submissions_json,
        prizes_json=args.prizes_json,
        include_unchecked=args.include_unchecked,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "stats": snapshot["stats"], "input_hashes": snapshot["input_hashes"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

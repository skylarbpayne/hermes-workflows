from __future__ import annotations

import html
import shlex
from pathlib import Path
from typing import Any

from .engine import WorkflowEngine


def render_dashboard(
    engine: WorkflowEngine,
    out_path: Path,
    *,
    status: str | None = None,
    recent_events: int = 5,
) -> Path:
    """Render a small read-only HTML dashboard for one workflow DB.

    The dashboard is deliberately static: it displays approval keys, provenance,
    outbox diagnostics, and recent events, but it does not include forms or any
    mutation endpoint. Operators approve through the normal `signal` path so the
    DB keeps a single auditable decision shape.
    """

    workflows = engine.list_workflows(status=status)
    status_packets = [
        engine.workflow_status(
            item["workflow_id"],
            recent_events=recent_events,
            command_history="recent",
            command_limit=5,
            command_payload_chars=500,
        )
        for item in workflows
    ]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_render_html(status_packets), encoding="utf-8")
    return out_path


def _render_html(workflows: list[dict[str, Any]]) -> str:
    workflow_cards = "\n".join(_workflow_card(packet) for packet in workflows)
    if not workflow_cards:
        workflow_cards = '<p class="empty">No workflows found for this filter.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hermes Workflows Dashboard</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b1020; --panel:#121a2e; --muted:#8ea0c4; --text:#eef4ff; --line:#263759; --ok:#38d39f; --warn:#ffd166; --bad:#ff6b6b; }}
    body {{ margin:0; padding:32px; background:var(--bg); color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; }}
    header {{ margin-bottom:24px; }}
    h1 {{ margin:0 0 6px; font-size:28px; }}
    h2 {{ margin:0 0 12px; font-size:20px; }}
    h3 {{ margin:18px 0 8px; font-size:15px; color:#c8d7ff; }}
    code, pre {{ font-family: ui-monospace,SFMono-Regular,Menlo,monospace; }}
    .muted {{ color:var(--muted); }}
    .grid {{ display:grid; gap:16px; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:18px; box-shadow:0 10px 40px rgba(0,0,0,.22); }}
    .meta {{ display:flex; flex-wrap:wrap; gap:8px; margin:8px 0 12px; }}
    .pill {{ border:1px solid var(--line); border-radius:999px; padding:3px 9px; color:#d7e2ff; background:#17213a; }}
    .completed,.approve {{ color:var(--ok); }} .waiting,.pending,.running {{ color:var(--warn); }} .failed,.reject,.invalid_decision,.cancelled {{ color:var(--bad); }}
    table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
    th, td {{ border-top:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); font-weight:600; }}
    pre {{ white-space:pre-wrap; background:#0a0f1d; border:1px solid var(--line); border-radius:10px; padding:10px; max-height:260px; overflow:auto; }}
    .empty {{ color:var(--muted); border:1px dashed var(--line); border-radius:12px; padding:20px; }}
  </style>
</head>
<body>
  <header>
    <h1>Hermes Workflows Dashboard</h1>
    <p class="muted">Read-only local dashboard for the configured workflow DB. Raw local DB paths are intentionally hidden in browser output. Approvals must still go through the normal signal path. Use <code>hermes-workflows approve</code>/<code>reject</code> with the workflow ref shown by your run command so the DB keeps one auditable decision shape.</p>
  </header>
  <main class="grid">
    {workflow_cards}
  </main>
</body>
</html>
"""


def _workflow_card(packet: dict[str, Any]) -> str:
    approvals = packet.get("approvals") or []
    pending = packet.get("pending_commands") or []
    diagnostics = packet.get("diagnostics") or []
    events = packet.get("events") or []
    commands = packet.get("command_history") or []
    return f"""
<section class="card">
  <h2>{_e(packet.get('workflow_id'))}</h2>
  <div class="meta">
    <span class="pill {_e(packet.get('status'))}">status: {_e(packet.get('status'))}</span>
    <span class="pill">workflow: {_e(packet.get('workflow_name'))}</span>
    <span class="pill">waiting: {_e(packet.get('waiting_on') or 'none')}</span>
  </div>
  {_approval_table(approvals, workflow_id=str(packet.get('workflow_id') or '<workflow_id>'), db_path=None)}
  {_command_table('Pending commands', pending)}
  {_diagnostic_table(diagnostics)}
  {_command_table('Recent commands', commands)}
  {_event_table(events)}
</section>
"""


def _approval_table(
    approvals: list[dict[str, Any]],
    *,
    workflow_id: str,
    db_path: Path | None,
) -> str:
    if not approvals:
        return '<h3>Approvals</h3><p class="muted">No approval requests recorded.</p>'
    rows = []
    for item in approvals:
        command = _approval_command(item, workflow_id=workflow_id, db_path=db_path)
        rows.append(
            "<tr>"
            f"<td><code>{_e(item.get('key'))}</code></td>"
            f"<td class=\"{_e(item.get('status'))}\">{_e(item.get('status'))}</td>"
            f"<td>{_e(item.get('prompt'))}</td>"
            f"<td><pre>{_e(item.get('source'))}</pre></td>"
            f"<td><pre>{_e(command)}</pre></td>"
            "</tr>"
        )
    return "<h3>Approvals</h3><table><thead><tr><th>Key</th><th>Status</th><th>Prompt</th><th>Prompt</th><th>Source</th><th>Approval shortcut</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _approval_command(item: dict[str, Any], *, workflow_id: str, db_path: Path | None) -> str:
    if item.get("decision"):
        return "already decided"
    key = item.get("key") or "<approval_key>"
    db = str(db_path) if db_path is not None else "<workflow.sqlite>"
    return (
        "hermes-workflows approve <workflow_ref> "
        f"--db {_q(db)} "
        f"--id {_q(workflow_id)} "
        f"--key {_q(str(key))} "
        "--by <human_id> --channel <channel> --message-url <approval_url>"
    )


def _q(value: str) -> str:
    if value.startswith("<") and value.endswith(">"):
        return value
    return shlex.quote(value)


def _command_table(title: str, commands: list[dict[str, Any]]) -> str:
    if not commands:
        return f'<h3>{_e(title)}</h3><p class="muted">None.</p>'
    rows = []
    for item in commands:
        rows.append(
            "<tr>"
            f"<td><code>{_e(item.get('key'))}</code></td>"
            f"<td>{_e(item.get('type'))}</td>"
            f"<td class=\"{_e(item.get('status'))}\">{_e(item.get('status'))}</td>"
            f"<td>{_e(item.get('diagnostic_label') or ', '.join(item.get('diagnostic_labels') or []))}</td>"
            f"<td><pre>{_e(item.get('last_error'))}</pre></td>"
            "</tr>"
        )
    return f"<h3>{_e(title)}</h3><table><thead><tr><th>Key</th><th>Type</th><th>Status</th><th>Diagnostic</th><th>Last error</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _diagnostic_table(diagnostics: list[dict[str, Any]]) -> str:
    if not diagnostics:
        return '<h3>Diagnostics</h3><p class="muted">No active diagnostics.</p>'
    rows = []
    for item in diagnostics:
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('severity'))}</td>"
            f"<td>{_e(item.get('label'))}</td>"
            f"<td>{_e(item.get('message'))}</td>"
            "</tr>"
        )
    return "<h3>Diagnostics</h3><table><thead><tr><th>Severity</th><th>Label</th><th>Message</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _event_table(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<h3>Recent events</h3><p class="muted">No events.</p>'
    rows = []
    for item in events:
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('seq'))}</td>"
            f"<td>{_e(item.get('type'))}</td>"
            f"<td><code>{_e(item.get('key'))}</code></td>"
            "</tr>"
        )
    return "<h3>Recent events</h3><table><thead><tr><th>Seq</th><th>Type</th><th>Key</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _e(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)

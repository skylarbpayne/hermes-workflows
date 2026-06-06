from __future__ import annotations

import json
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .dashboard import render_dashboard
from .engine import WorkflowEngine


def _dashboard_server(handler: BaseHTTPRequestHandler) -> "DashboardServer":
    return handler.server  # type: ignore[return-value]


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        db_path: Path,
        workflow: Callable[..., Any],
        workflow_ref: str,
        once: bool = False,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.db_path = db_path
        self.workflow = workflow
        self.workflow_ref = workflow_ref
        self.once = once

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/", "/dashboard"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        server = _dashboard_server(self)
        engine = WorkflowEngine(server.db_path, read_only=True)
        with tempfile.TemporaryDirectory(prefix="hermes-workflows-dashboard-") as tmp:
            out = Path(tmp) / "dashboard.html"
            render_dashboard(engine, out)
            html = out.read_text(encoding="utf-8")
        html = self._inject_approval_forms(html)
        self._send_html(html)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/approve":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        fields = self._read_form()
        workflow_id = _required(fields, "workflow_id")
        key = _required(fields, "key")
        by = _required(fields, "by")
        channel = _required(fields, "channel")
        source = {"kind": "human", "id": by, "channel": channel}
        for field in ("message_url", "message_id", "event_id"):
            value = fields.get(field, [""])[0].strip()
            if value:
                source[field] = value
        if not any(field in source for field in ("message_url", "message_id", "event_id")):
            self.send_error(HTTPStatus.BAD_REQUEST, "approval requires message_url, message_id, or event_id")
            return
        payload = {"action": fields.get("action", ["approve"])[0], "by": by}
        note = fields.get("note", [""])[0].strip()
        if note:
            payload["note"] = note
        idempotency_key = fields.get("idempotency_key", [""])[0].strip() or (
            f"{channel}:{workflow_id}:{key}:{payload['action']}:{source.get('message_url') or source.get('message_id') or source.get('event_id')}"
        )

        server = _dashboard_server(self)
        engine = WorkflowEngine(server.db_path)
        engine.signal(
            workflow_id,
            "approval.decision",
            key=key,
            payload=payload,
            source=source,
            idempotency_key=idempotency_key,
        )
        if server.once:
            self._send_html("<h1>Approval recorded</h1>")
            threading.Thread(target=server.shutdown, daemon=True).start()
        else:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()

    def _read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return parse_qs(body, keep_blank_values=True)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _inject_approval_forms(self, html: str) -> str:
        server = _dashboard_server(self)
        form = f"""
<section class="card">
  <h2>Local approval form</h2>
  <p class="muted">This posts to the local server and records human provenance through <code>approval.decision</code>. Workflow ref: <code>{_escape(server.workflow_ref)}</code></p>
  <form method="post" action="/approve" class="approval-form">
    <label>Workflow ID <input name="workflow_id" required placeholder="wf_..." /></label>
    <label>Approval key <input name="key" required placeholder="approve_..." /></label>
    <label>Human ID <input name="by" required placeholder="operator" /></label>
    <label>Channel <input name="channel" required value="local-dashboard" /></label>
    <label>Message ID <input name="message_id" required placeholder="local-click-1" /></label>
    <input type="hidden" name="action" value="approve" />
    <button type="submit">Approve</button>
  </form>
</section>
"""
        return html.replace("</main>", form + "</main>")


def _required(fields: dict[str, list[str]], key: str) -> str:
    value = fields.get(key, [""])[0].strip()
    if not value:
        raise ValueError(f"missing required field: {key}")
    return value


def _escape(value: Any) -> str:
    import html

    return html.escape(str(value), quote=True)


def serve_dashboard(
    *,
    db_path: Path,
    workflow: Callable[..., Any],
    workflow_ref: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    once: bool = False,
) -> str:
    server = DashboardServer(
        (host, port),
        DashboardHandler,
        db_path=db_path,
        workflow=workflow,
        workflow_ref=workflow_ref,
        once=once,
    )
    print(json.dumps({"url": server.url, "db": str(db_path), "workflow_ref": workflow_ref}), flush=True)
    server.serve_forever()
    return server.url

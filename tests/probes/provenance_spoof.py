from __future__ import annotations

import json
import sys
import threading
from http import HTTPStatus
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hermes_workflows.operator_services import OperatorServicesV1  # noqa: E402
from hermes_workflows.provenance import (  # noqa: E402
    PROVENANCE_CONTRACT_VERSION,
    PROVENANCE_SERVICE_ID,
    AuthenticatedPrincipalV1,
    EventProvenanceV1,
    TrustedGatewayContextV1,
    TrustedGatewayHTTPHookV1,
)


class ProbeServer(ThreadingHTTPServer):
    def __init__(self, service: TrustedGatewayHTTPHookV1, context: TrustedGatewayContextV1) -> None:
        super().__init__(("127.0.0.1", 0), ProbeHandler)
        self.service = service
        self.context = context


class ProbeHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:
        server = cast(ProbeServer, self.server)
        if self.path != "/gateway/review-response":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("content-length", "0"))
        try:
            stamped = server.service.handle_http(
                self.rfile.read(length),
                context=server.context,
            )
            response = json.dumps(stamped.to_dict(), sort_keys=True).encode("utf-8")
        except (TypeError, ValueError, PermissionError) as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def _post(server: ProbeServer, body: bytes) -> dict[str, Any]:
    request = Request(
        f"http://127.0.0.1:{server.server_port}/gateway/review-response",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return {
                "status": response.status,
                "body": response.read().decode("utf-8"),
            }
    except HTTPError as exc:
        return {
            "status": exc.code,
            "body": exc.read().decode("utf-8"),
        }
    except RemoteDisconnected as exc:
        return {"status": None, "error": type(exc).__name__}


def main() -> int:
    principal = AuthenticatedPrincipalV1(
        issuer="probe-gateway",
        subject="not-skylar",
        platform="discord",
        tenant_id="guild-42",
        chat_id="channel-7",
        verified_at="2026-07-13T20:00:00Z",
        adapter_evidence_id="probe-auth-901",
    )
    context = TrustedGatewayContextV1(
        principal=principal,
        display_label="Not Skylar",
        event=EventProvenanceV1(channel="discord:channel-7", message_id="message-55"),
    )
    services = OperatorServicesV1(services={PROVENANCE_SERVICE_ID: TrustedGatewayHTTPHookV1()})
    service = services.resolve(PROVENANCE_SERVICE_ID, PROVENANCE_CONTRACT_VERSION)
    if not isinstance(service, TrustedGatewayHTTPHookV1):
        raise RuntimeError("trusted provenance service was not resolved through the operator-service seam")

    server = ProbeServer(service, context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request_payload = {
        "action": "approve",
        "by": "skylar",
        "display_label": "Skylar",
        "principal": {"issuer": "client", "subject": "skylar"},
        "source": {"kind": "human", "id": "skylar"},
    }
    try:
        accepted = _post(server, json.dumps(request_payload).encode("utf-8"))
        malformed_rejection = _post(server, b'{"action":')
        deep_body = b'{"nested":' + (b"[" * 500) + b"null" + (b"]" * 500) + b"}"
        deep_rejection = _post(server, deep_body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    if accepted["status"] != HTTPStatus.OK:
        raise RuntimeError(f"valid trusted-gateway request failed: {accepted['status']}")
    response = json.loads(accepted["body"])

    trace = {
        "schema_version": 1,
        "transport": "actual_loopback_http",
        "service_id": PROVENANCE_SERVICE_ID,
        "request": request_payload,
        "gateway_principal": principal.to_dict(),
        "response": response,
        "malformed_rejection": malformed_rejection,
        "deep_rejection": deep_rejection,
    }
    if response["provenance"]["principal"]["subject"] == "skylar":
        raise RuntimeError("client-supplied principal crossed the trusted boundary")
    print(json.dumps(trace, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

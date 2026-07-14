from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hermes_workflows.operator_services import OperatorServicesV1
from hermes_workflows.provenance import (
    PROVENANCE_CONTRACT_VERSION,
    PROVENANCE_SERVICE_ID,
    AuthenticatedPrincipalV1,
    EventProvenanceV1,
    ResponseProvenanceV1,
    StampedResponseV1,
    TrustedGatewayContextV1,
    TrustedGatewayHTTPHookV1,
    legacy_unverified_provenance,
    local_operator_provenance,
    project_response_provenance,
    require_authenticated_principal,
)


FIXTURE = Path(__file__).parent / "fixtures" / "provenance_v1.json"
PROBE = Path(__file__).parent / "probes" / "provenance_spoof.py"


def _principal(subject: str = "not-skylar") -> AuthenticatedPrincipalV1:
    return AuthenticatedPrincipalV1(
        issuer="hermes-gateway",
        subject=subject,
        platform="discord",
        tenant_id="guild-42",
        chat_id="channel-7",
        verified_at="2026-07-13T20:00:00Z",
        adapter_evidence_id="gateway-auth-901",
    )


def _event() -> EventProvenanceV1:
    return EventProvenanceV1(channel="discord:channel-7", message_id="message-55")


def _context(subject: str = "not-skylar") -> TrustedGatewayContextV1:
    return TrustedGatewayContextV1(
        principal=_principal(subject),
        display_label="Not Skylar" if subject == "not-skylar" else "Skylar",
        event=_event(),
    )


def test_free_form_tool_values_are_legacy_unverified_display_labels_only():
    provenance = legacy_unverified_provenance(
        {
            "action": "approve",
            "by": "skylar",
            "principal": {"subject": "skylar"},
            "source": {
                "kind": "human",
                "id": "skylar",
                "channel": "hermes-plugin",
                "message_id": "tool-1",
            },
        }
    )

    assert provenance.kind == "legacy_unverified"
    assert provenance.principal is None
    assert provenance.display_label == "skylar"
    assert provenance.event == EventProvenanceV1(channel="hermes-plugin", message_id="tool-1")
    with pytest.raises(PermissionError, match="authenticated principal"):
        require_authenticated_principal(provenance)


def test_local_dashboard_receipt_is_explicitly_unattributed_and_cannot_authorize_identity_effects():
    provenance = local_operator_provenance(event_id="local-click-1")

    assert provenance.to_dict() == {
        "schema_version": 1,
        "kind": "unattributed_local_operator",
        "principal": None,
        "display_label": None,
        "event": {
            "channel": "local-dashboard",
            "message_id": None,
            "message_url": None,
            "event_id": "local-click-1",
        },
    }
    with pytest.raises(PermissionError, match="unattributed_local_operator"):
        require_authenticated_principal(provenance)


def test_authenticated_principal_is_immutable_and_separate_from_display_and_event_provenance():
    principal = _principal()
    provenance = ResponseProvenanceV1.authenticated(principal, display_label="Friendly Name", event=_event())

    assert provenance.principal is principal
    assert provenance.display_label == "Friendly Name"
    assert provenance.event == _event()
    with pytest.raises(FrozenInstanceError):
        principal.subject = "skylar"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        provenance.display_label = "skylar"  # type: ignore[misc]


def test_owned_service_hook_ignores_all_client_actor_and_provenance_fields():
    hook = TrustedGatewayHTTPHookV1()
    services = OperatorServicesV1(services={PROVENANCE_SERVICE_ID: hook})
    resolved = services.resolve(PROVENANCE_SERVICE_ID, PROVENANCE_CONTRACT_VERSION)
    assert resolved is hook

    stamped = hook.handle_http(
        json.dumps(
            {
                "action": "approve",
                "by": "skylar",
                "display_label": "Skylar",
                "principal": _principal("skylar").to_dict(),
                "authenticated_principal": _principal("skylar").to_dict(),
                "provenance": {"kind": "authenticated_principal"},
                "source": {"kind": "human", "id": "skylar"},
            }
        ).encode("utf-8"),
        context=_context("not-skylar"),
    )

    assert dict(stamped.payload) == {"action": "approve"}
    assert stamped.provenance.principal == _principal("not-skylar")
    assert stamped.provenance.display_label == "Not Skylar"
    serialized = stamped.to_dict()
    assert serialized["provenance"]["principal"]["subject"] == "not-skylar"
    assert serialized["provenance"]["display_label"] != "Skylar"


def test_gateway_principal_mismatch_is_refused_and_never_replaced_by_display_label():
    hook = TrustedGatewayHTTPHookV1()

    with pytest.raises(PermissionError, match="principal mismatch"):
        hook.handle_http(
            b'{"action":"approve","by":"skylar"}',
            context=_context("not-skylar"),
            expected_principal=_principal("skylar"),
        )

    stamped = hook.handle_http(b'{"action":"approve","by":"skylar"}', context=_context("not-skylar"))
    principal = require_authenticated_principal(stamped.provenance, expected_principal=_principal("not-skylar"))
    assert principal.subject == "not-skylar"


def test_identity_required_effects_fail_closed_for_unverified_and_mismatched_provenance():
    unverified = legacy_unverified_provenance({"by": "skylar"})
    local = local_operator_provenance(event_id="local-2")
    trusted = ResponseProvenanceV1.authenticated(_principal("not-skylar"), "Not Skylar", _event())

    for provenance in (unverified, local):
        with pytest.raises(PermissionError):
            require_authenticated_principal(provenance, expected_principal=_principal("skylar"))
    with pytest.raises(PermissionError, match="principal mismatch"):
        require_authenticated_principal(trusted, expected_principal=_principal("skylar"))


def test_old_rows_project_as_legacy_unverified_without_trust_backfill():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == 1
    for case in fixture["old_rows"]:
        assert project_response_provenance(case["row"]).to_dict() == case["expected"], case["name"]


def test_canonical_projection_round_trips_but_rejects_unknown_or_malformed_trust_records():
    canonical = ResponseProvenanceV1.authenticated(_principal(), "Not Skylar", _event())
    assert project_response_provenance({"response_provenance": canonical.to_dict()}) == canonical

    malformed = canonical.to_dict()
    malformed["extra"] = "client-controlled"
    with pytest.raises(ValueError, match="unknown response provenance fields"):
        project_response_provenance({"response_provenance": malformed})

    missing_principal = canonical.to_dict()
    missing_principal["principal"] = None
    with pytest.raises(ValueError, match="requires a principal"):
        project_response_provenance({"response_provenance": missing_principal})


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_response_provenance_schema_version_requires_an_exact_integer(schema_version):
    with pytest.raises(TypeError, match="schema_version must be an integer"):
        ResponseProvenanceV1(
            schema_version=schema_version,
            kind="legacy_unverified",
        )


def test_stamped_response_rejects_payloads_over_the_json_depth_bound():
    nested = None
    for _ in range(40):
        nested = [nested]

    with pytest.raises(ValueError, match="response payload exceeds JSON limits"):
        StampedResponseV1(
            {"nested": nested},
            ResponseProvenanceV1(kind="legacy_unverified"),
        )


def test_stamped_response_rejects_payloads_over_the_json_node_bound():
    with pytest.raises(ValueError, match="response payload exceeds JSON limits"):
        StampedResponseV1(
            {"items": list(range(5_000))},
            ResponseProvenanceV1(kind="legacy_unverified"),
        )


def test_stamped_response_rejects_payloads_over_the_canonical_byte_bound():
    with pytest.raises(ValueError, match="response payload exceeds JSON limits"):
        StampedResponseV1(
            {"text": "x" * (70 * 1024)},
            ResponseProvenanceV1(kind="legacy_unverified"),
        )


def test_trusted_context_validation_rejects_unscoped_or_naive_authentication_evidence():
    with pytest.raises(ValueError, match="tenant_id or chat_id"):
        AuthenticatedPrincipalV1(
            issuer="gateway",
            subject="user-1",
            platform="discord",
            tenant_id=None,
            chat_id=None,
            verified_at="2026-07-13T20:00:00Z",
            adapter_evidence_id="auth-1",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        AuthenticatedPrincipalV1(
            issuer="gateway",
            subject="user-1",
            platform="discord",
            tenant_id="guild-1",
            chat_id=None,
            verified_at="2026-07-13T20:00:00",
            adapter_evidence_id="auth-1",
        )


def test_actual_http_spoof_probe_never_accepts_client_skylar_identity():
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        check=True,
        capture_output=True,
        text=True,
    )
    trace = json.loads(completed.stdout)

    assert trace["request"]["by"] == "skylar"
    assert trace["gateway_principal"]["subject"] == "not-skylar"
    assert trace["response"]["provenance"]["principal"]["subject"] == "not-skylar"
    assert trace["response"]["provenance"]["display_label"] == "Not Skylar"
    assert trace["response"]["payload"] == {"action": "approve"}
    for case in ("malformed_rejection", "deep_rejection"):
        assert trace[case]["status"] == 400
        assert "error" not in trace[case]
        assert "RecursionError" not in trace[case]["body"]
        assert any(
            message in trace[case]["body"]
            for message in (
                "valid bounded UTF-8 JSON object",
                "response payload exceeds JSON limits",
            )
        )

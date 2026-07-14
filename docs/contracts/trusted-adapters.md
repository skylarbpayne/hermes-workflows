# Trusted adapter provenance contract (v1)

Hermes Workflows keeps three facts separate:

1. `principal`: immutable identity derived from an authenticated gateway request;
2. `display_label`: presentation-only text with no authority;
3. `event`: where the response happened (`channel` plus a message/event reference).

A workflow asks for a response. It does not choose an approver. Deployment policy may compare an authenticated principal at an identity-required effect boundary.

## First-release classifications

| Response surface | `kind` | Principal | May satisfy identity-required effects? |
|---|---|---|---|
| Existing free-form plugin/tool arguments (`by`, `user`, `source`) | `legacy_unverified` | None | No |
| Local dashboard action | `unattributed_local_operator` | None | No |
| Registered trusted gateway adapter | `authenticated_principal` | Server-stamped | Yes, after policy match |
| Pre-boundary local-dashboard row | `unattributed_local_operator` | None | No |
| Other pre-boundary stored row | `legacy_unverified` | None | No |

Old labels are never upgraded to authenticated identities. A source object containing `kind: authenticated_principal` is still legacy unless it was stored in the server-owned `response_provenance` field by this contract.

## Trusted gateway boundary

The v1 process-local operator service is `provenance.trusted_gateway`, contract version `1`. A server resolves `TrustedGatewayHTTPHookV1` through `OperatorServicesV1` and calls it only after its gateway authentication layer has produced a `TrustedGatewayContextV1`.

The server-owned context contains:

- `issuer`: authentication authority or gateway verifier;
- `subject`: immutable platform subject ID;
- `platform`: authenticated platform namespace;
- `tenant_id` and/or `chat_id`: scope included in identity matching;
- `verified_at`: timezone-aware authentication time;
- `adapter_evidence_id`: non-secret verifier receipt/request ID;
- `display_label`: presentation-only label derived by the adapter;
- `event`: channel and message/event evidence.

The HTTP body may contain workflow response data such as `action`, feedback, or typed fields. These client-controlled keys are reserved and stripped: `by`, `user`, `display_label`, `principal`, `authenticated_principal`, `provenance`, and `source`. They can neither override nor supplement the server-owned context.

Stamped response payloads are bounded before use: at most 32 nested JSON levels, 4,096 JSON value nodes, and 65,536 UTF-8 bytes in deterministic canonical encoding. The raw HTTP body has the same 65,536-byte ceiling. Malformed or recursively over-deep input is rejected as a generic bounded JSON validation error rather than exposing decoder/runtime details. Provenance `schema_version` is the exact JSON integer `1`; booleans and floating-point spellings such as `1.0` are invalid.

A policy may pass an expected principal to the hook or to `require_authenticated_principal`. Identity matching includes issuer, subject, platform, tenant, and chat. Any mismatch fails closed. A display-label match never counts.

## Adapter responsibilities

A deployment-specific adapter must document and test:

- which gateway fields establish issuer, subject, platform, tenant/chat, and event identity;
- which component verifies signatures/tokens before constructing `TrustedGatewayContextV1`;
- replay/idempotency handling for `adapter_evidence_id` and event IDs;
- how tenant/chat scope maps into deployment policy;
- evidence retention and redaction rules.

Do not construct trusted context from browser JSON, form fields, free-form tool arguments, forwarded headers accepted directly from the public client, or workflow payloads. If verified context is missing, classify the response as legacy/local as appropriate or reject it. Never invent a principal.

## Projection and authorization

`project_response_provenance(row)` recognizes existing `local-dashboard` event rows as unattributed local actions and projects every other pre-boundary row as `legacy_unverified`. It decodes authenticated provenance only from the server-owned `response_provenance` record. Neither conservative projection can authorize an identity-required effect.

`require_authenticated_principal(provenance, expected_principal=...)` is the fail-closed seam for identity-required effects. It rejects legacy, local-unattributed, missing-principal, malformed, and mismatched records.

## Reproducible spoof probe

Run:

```sh
uv run python tests/probes/provenance_spoof.py
```

The probe posts real loopback HTTP JSON with `by=skylar` and a client principal for Skylar while the server-owned gateway context authenticates `not-skylar`. The response must contain principal `not-skylar` (or the request must be rejected), never Skylar. The trace intentionally contains only synthetic IDs.

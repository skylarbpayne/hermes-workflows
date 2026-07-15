from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from hermes_workflows.registry_v2 import (
    RegistryContractError,
    RegistryIdentityServiceV1,
    decode_registry,
    dry_run_migrate_registry_file,
    encode_registry_v2,
    load_registry_file,
    require_consumer_parity,
)


FIXTURES = Path(__file__).parent / "fixtures"
VALID = FIXTURES / "registry_v2_valid.json"
DRIFT = FIXTURES / "registry_v2_drift.json"
LEGACY = FIXTURES / "registry_v1_legacy.json"


def _write_registry(root: Path, fixture: Path = VALID) -> Path:
    registry = root / ".hermes" / "workflows.registry.json"
    registry.parent.mkdir(parents=True)
    shutil.copy2(fixture, registry)
    return registry


def test_target_schema_fixture_round_trip_and_normalized_fingerprint() -> None:
    raw = json.loads(VALID.read_text(encoding="utf-8"))
    loaded = decode_registry(VALID.read_bytes())

    assert loaded.source_schema_version == 2
    assert json.loads(encode_registry_v2(loaded.catalog)) == raw
    assert loaded.catalog.fingerprint.startswith("sha256:")
    assert len(loaded.catalog.fingerprint) == 71

    reordered = copy.deepcopy(raw)
    reordered["dbs"] = dict(reversed(tuple(reordered["dbs"].items())))
    reordered["workflows"] = dict(reversed(tuple(reordered["workflows"].items())))
    reordered["runner"]["dbs"].reverse()
    reordered["workflows"]["review-workflow"]["tags"].reverse()
    reordered_loaded = decode_registry(json.dumps(reordered, indent=7))

    assert reordered_loaded.catalog.fingerprint == loaded.catalog.fingerprint
    assert encode_registry_v2(reordered_loaded.catalog) == encode_registry_v2(loaded.catalog)


def test_v1_is_read_only_compatible_and_migration_is_a_deterministic_dry_run() -> None:
    before = LEGACY.read_bytes()

    loaded = load_registry_file(LEGACY)
    migration = dry_run_migrate_registry_file(LEGACY)

    assert LEGACY.read_bytes() == before
    assert loaded.source_schema_version == 1
    assert json.loads(encode_registry_v2(loaded.catalog)) == json.loads(VALID.read_text(encoding="utf-8"))
    assert migration.to_dict() == {
        "schema_version": 1,
        "source_schema_version": 1,
        "target_schema_version": 2,
        "would_write": False,
        "target_fingerprint": loaded.catalog.fingerprint,
        "target_registry": json.loads(encode_registry_v2(loaded.catalog)),
    }
    assert migration.canonical_target_json == encode_registry_v2(loaded.catalog)


def test_v1_marker_is_accepted_but_v2_never_emits_it() -> None:
    payload = json.loads(LEGACY.read_text(encoding="utf-8"))
    payload["schema_version"] = 1

    loaded = decode_registry(json.dumps(payload))

    assert loaded.source_schema_version == 1
    assert json.loads(encode_registry_v2(loaded.catalog))["schema_version"] == 2


def test_relative_resolution_is_registry_directory_then_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "copy"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    registry = _write_registry(root)
    monkeypatch.chdir(elsewhere)

    service = RegistryIdentityServiceV1.from_file(registry)
    resolved = service.resolve_db("primary")

    assert resolved.registry_path == registry
    assert resolved.state_root == root / ".hermes" / "state"
    assert resolved.db_path == root / ".hermes" / "state" / "primary" / "workflows.sqlite"
    assert resolved.db_alias == "primary"


def test_identity_is_alias_only_and_redacts_registry_and_db_paths(tmp_path: Path) -> None:
    root = tmp_path / "private-user" / "secret-project"
    registry = _write_registry(root)
    service = RegistryIdentityServiceV1.from_file(registry)

    identity = service.identity("primary")
    encoded = json.dumps(identity.to_dict(), sort_keys=True)

    assert identity.db_alias == "primary"
    assert identity.registry_fingerprint == service.catalog.fingerprint
    assert identity.resolved_db_identity.startswith("sha256:")
    assert identity.registry_identity.startswith("sha256:")
    assert str(root) not in encoded
    assert "workflows.sqlite" not in encoded
    for path_like in (str(registry), "state/primary/workflows.sqlite", "primary/workflows.sqlite"):
        with pytest.raises(RegistryContractError) as raised:
            service.identity(path_like)
        assert raised.value.code == "registry_alias_required"
        assert raised.value.exit_code == 2


def test_two_copy_local_databases_with_the_same_alias_and_catalog_are_not_confused(tmp_path: Path) -> None:
    left = RegistryIdentityServiceV1.from_file(_write_registry(tmp_path / "left"))
    right = RegistryIdentityServiceV1.from_file(_write_registry(tmp_path / "right"))

    left_identity = left.identity("primary")
    right_identity = right.identity("primary")

    assert left_identity.registry_fingerprint == right_identity.registry_fingerprint
    assert left_identity.db_alias == right_identity.db_alias == "primary"
    assert left_identity.registry_identity != right_identity.registry_identity
    assert left_identity.resolved_db_identity != right_identity.resolved_db_identity
    with pytest.raises(RegistryContractError) as raised:
        require_consumer_parity({"cli": left_identity, "plugin": right_identity})
    assert raised.value.code == "registry_drift"
    assert raised.value.exit_code == 2


def test_changed_catalog_fingerprint_detects_similar_database_drift(tmp_path: Path) -> None:
    valid = RegistryIdentityServiceV1.from_file(_write_registry(tmp_path / "valid", VALID))
    drift = RegistryIdentityServiceV1.from_file(_write_registry(tmp_path / "drift", DRIFT))

    assert valid.catalog.fingerprint != drift.catalog.fingerprint
    with pytest.raises(RegistryContractError, match="do not share one registry identity"):
        require_consumer_parity({"cli": valid.identity("primary"), "supervisor": drift.identity("primary")})


def test_v2_uses_one_canonical_spelling_and_rejects_unknown_or_legacy_shapes() -> None:
    valid = json.loads(VALID.read_text(encoding="utf-8"))
    invalid_payloads = []

    extra_root = copy.deepcopy(valid)
    extra_root["version"] = 2
    invalid_payloads.append(extra_root)

    string_db = copy.deepcopy(valid)
    string_db["dbs"]["primary"] = "primary/workflows.sqlite"
    invalid_payloads.append(string_db)

    alternate_ref = copy.deepcopy(valid)
    alternate_ref["workflows"]["review-workflow"]["ref"] = alternate_ref["workflows"]["review-workflow"].pop(
        "workflow_ref"
    )
    invalid_payloads.append(alternate_ref)

    unknown_workflow_field = copy.deepcopy(valid)
    unknown_workflow_field["workflows"]["review-workflow"]["database"] = "primary"
    invalid_payloads.append(unknown_workflow_field)

    for payload in invalid_payloads:
        with pytest.raises(RegistryContractError) as raised:
            decode_registry(json.dumps(payload))
        assert raised.value.code == "registry_invalid"
        assert raised.value.exit_code == 2


def test_malformed_duplicate_and_unknown_ids_fail_closed() -> None:
    valid = json.loads(VALID.read_text(encoding="utf-8"))

    malformed_alias = copy.deepcopy(valid)
    malformed_alias["dbs"]["Primary"] = malformed_alias["dbs"].pop("primary")

    duplicate_runner = copy.deepcopy(valid)
    duplicate_runner["runner"]["dbs"] = ["primary", "primary"]

    unknown_db = copy.deepcopy(valid)
    unknown_db["workflows"]["review-workflow"]["db"] = "missing"

    filesystem_ref = copy.deepcopy(valid)
    filesystem_ref["workflows"]["review-workflow"]["workflow_ref"] = "../workflow.py:run"

    for payload in (malformed_alias, duplicate_runner, unknown_db, filesystem_ref):
        with pytest.raises(RegistryContractError) as raised:
            decode_registry(json.dumps(payload))
        assert raised.value.code == "registry_invalid"

    duplicate_key = VALID.read_text(encoding="utf-8").replace(
        '"schema_version": 2,',
        '"schema_version": 2, "schema_version": 2,',
        1,
    )
    with pytest.raises(RegistryContractError) as raised:
        decode_registry(duplicate_key)
    assert raised.value.code == "registry_invalid"


def test_runner_and_json_values_are_strict_and_deterministic() -> None:
    valid = json.loads(VALID.read_text(encoding="utf-8"))
    invalid_payloads = []
    for lease in (True, 0, 3601, 1.5, "30"):
        payload = copy.deepcopy(valid)
        payload["runner"]["lease_seconds"] = lease
        invalid_payloads.append(payload)

    nan_payload = VALID.read_text(encoding="utf-8").replace('"mode": "review"', '"mode": NaN')
    for payload in invalid_payloads:
        with pytest.raises(RegistryContractError):
            decode_registry(json.dumps(payload))
    with pytest.raises(RegistryContractError):
        decode_registry(nan_payload)


def test_path_traversal_absolute_home_drive_and_backslash_values_fail_closed() -> None:
    valid = json.loads(VALID.read_text(encoding="utf-8"))
    bad_paths = [
        "../escape",
        "nested/../escape",
        "/private/state",
        "~/state",
        "C:/state",
        "nested\\state",
        "nested//state",
        "nested/./state",
    ]
    for bad in bad_paths:
        bad_state = copy.deepcopy(valid)
        bad_state["state_root"] = bad
        bad_db = copy.deepcopy(valid)
        bad_db["dbs"]["primary"]["path"] = bad
        for payload in (bad_state, bad_db):
            with pytest.raises(RegistryContractError) as raised:
                decode_registry(json.dumps(payload))
            assert raised.value.code == "registry_invalid"
            assert raised.value.exit_code == 2


def test_registry_state_root_and_db_symlinks_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    real_registry = _write_registry(tmp_path / "registry-link-source")
    linked_registry = tmp_path / "registry-link.json"
    linked_registry.symlink_to(real_registry)
    with pytest.raises(RegistryContractError) as raised:
        load_registry_file(linked_registry)
    assert raised.value.code == "registry_path_invalid"

    state_root = tmp_path / "state-link-root"
    registry = _write_registry(state_root)
    (state_root / ".hermes" / "state").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RegistryContractError) as raised:
        RegistryIdentityServiceV1.from_file(registry).identity("primary")
    assert raised.value.code == "registry_path_invalid"

    db_link_root = tmp_path / "db-link-root"
    registry = _write_registry(db_link_root)
    state = db_link_root / ".hermes" / "state"
    state.mkdir()
    (state / "primary").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RegistryContractError) as raised:
        RegistryIdentityServiceV1.from_file(registry).identity("primary")
    assert raised.value.code == "registry_path_invalid"


def test_noncanonical_registry_file_path_is_refused(tmp_path: Path) -> None:
    registry = _write_registry(tmp_path / "root")
    noncanonical = registry.parent / ".." / ".hermes" / registry.name

    with pytest.raises(RegistryContractError) as raised:
        load_registry_file(noncanonical)
    assert raised.value.code == "registry_path_invalid"


def test_errors_use_bounded_doctor_exit_2_envelopes_without_private_values(tmp_path: Path) -> None:
    private_root = tmp_path / ("private-" + "x" * 100)
    registry = _write_registry(private_root)
    service = RegistryIdentityServiceV1.from_file(registry)

    with pytest.raises(RegistryContractError) as raised:
        service.identity("missing")

    error = raised.value
    envelope = error.to_dict()
    encoded = json.dumps(envelope, sort_keys=True)
    assert error.exit_code == 2
    assert set(envelope) == {"code", "message", "fields", "conflict_id"}
    assert len(encoded.encode("utf-8")) <= 4096
    assert len(envelope["message"].encode("utf-8")) <= 256
    assert str(private_root) not in encoded
    assert "workflows.sqlite" not in encoded

    oversized = '{"schema_version":2,"private":"' + "secret-value-" * 100_000 + '"}'
    with pytest.raises(RegistryContractError) as oversized_error:
        decode_registry(oversized)
    oversized_encoded = json.dumps(oversized_error.value.to_dict(), sort_keys=True)
    assert oversized_error.value.exit_code == 2
    assert len(oversized_encoded.encode("utf-8")) <= 4096
    assert "secret-value" not in oversized_encoded


def test_secret_like_defaults_never_appear_in_public_identity_or_drift_error(tmp_path: Path) -> None:
    payload = json.loads(VALID.read_text(encoding="utf-8"))
    payload["workflows"]["review-workflow"]["default_input"] = {
        "api_token": "super-secret-token",
        "private_path": "/Users/example/private",
    }
    left_path = tmp_path / "left" / ".hermes" / "workflows.registry.json"
    left_path.parent.mkdir(parents=True)
    left_path.write_text(json.dumps(payload), encoding="utf-8")
    right_path = _write_registry(tmp_path / "right", DRIFT)

    left = RegistryIdentityServiceV1.from_file(left_path).identity("primary")
    right = RegistryIdentityServiceV1.from_file(right_path).identity("primary")
    public = json.dumps(left.to_dict(), sort_keys=True)
    assert "super-secret-token" not in public
    assert "/Users/example/private" not in public

    with pytest.raises(RegistryContractError) as raised:
        require_consumer_parity({"cli": left, "plugin": right})
    error = json.dumps(raised.value.to_dict(), sort_keys=True)
    assert "super-secret-token" not in error
    assert "/Users/example/private" not in error

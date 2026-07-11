from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

import hermes_workflows.registry_location as registry_location
from hermes_workflows.registry_location import (
    RegistryLocationV1,
    RelativeDbPathV1,
    ResolvedRegistryLocationV1,
    decode_registry_location,
    decode_relative_db_path,
    encode_registry_location,
    encode_relative_db_path,
    resolve_registry_location,
    resolve_relative_db_path,
    resolve_relative_db_paths,
)


FIXTURE = Path(__file__).parent / "fixtures" / "registry_location_v1.json"


def test_exact_canonical_objects_and_fixture_round_trip():
    location_payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    location = RegistryLocationV1.from_dict(location_payload)
    db = RelativeDbPathV1(alias="primary-db", path="tenant/workflows.sqlite")

    assert location.to_dict() == {
        "schema_version": 1,
        "registry_file": "registries/workflows.registry.json",
        "state_root": "state",
    }
    assert db.to_dict() == {
        "schema_version": 1,
        "alias": "primary-db",
        "path": "tenant/workflows.sqlite",
    }
    assert encode_registry_location(location) == (
        '{"registry_file":"registries/workflows.registry.json",'
        '"schema_version":1,"state_root":"state"}'
    )
    assert encode_relative_db_path(db) == (
        '{"alias":"primary-db","path":"tenant/workflows.sqlite","schema_version":1}'
    )
    assert decode_registry_location(encode_registry_location(location)) == location
    assert decode_relative_db_path(encode_relative_db_path(db)) == db


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (RegistryLocationV1.from_dict, {"schema_version": 2, "registry_file": "registry.json", "state_root": "state"}),
        (
            RegistryLocationV1.from_dict,
            {"schema_version": 1, "registry_file": "registry.json", "state_root": "state", "extra": True},
        ),
        (RelativeDbPathV1.from_dict, {"schema_version": 2, "alias": "main", "path": "main.sqlite"}),
        (
            RelativeDbPathV1.from_dict,
            {"schema_version": 1, "alias": "main", "path": "main.sqlite", "extra": True},
        ),
    ],
)
def test_decoding_rejects_unknown_versions_and_fields(factory, payload):
    with pytest.raises((TypeError, ValueError)):
        factory(payload)


@pytest.mark.parametrize(
    ("decoder", "payload", "field"),
    [
        (
            decode_registry_location,
            '{"schema_version":2,"schema_version":1,"registry_file":"registry.json","state_root":"state"}',
            "schema_version",
        ),
        (
            decode_registry_location,
            '{"schema_version":1,"registry_file":"/invalid","registry_file":"registry.json","state_root":"state"}',
            "registry_file",
        ),
        (
            decode_registry_location,
            '{"schema_version":1,"registry_file":"registry.json","state_root":"../invalid","state_root":"state"}',
            "state_root",
        ),
        (
            decode_relative_db_path,
            '{"schema_version":1,"alias":"Main","alias":"main","path":"main.sqlite"}',
            "alias",
        ),
        (
            decode_relative_db_path,
            '{"schema_version":1,"alias":"main","path":"../invalid","path":"main.sqlite"}',
            "path",
        ),
    ],
)
def test_decoding_rejects_duplicate_object_keys(decoder, payload, field):
    with pytest.raises(ValueError, match=rf"duplicate object key: {field}"):
        decoder(payload)


@pytest.mark.parametrize(
    "path",
    [
        "",
        " ",
        "/absolute/path",
        "~/personal/path",
        "personal~/path",
        "C:/drive/path",
        "C:drive/path",
        "../escape",
        "nested/../escape",
        "nested/./file",
        "nested//file",
        "nested/",
        "nested\\windows",
        "nul\x00path",
        "é" * 513,
    ],
)
def test_serialized_paths_reject_absolute_personal_drive_traversal_and_malformed_values(path):
    with pytest.raises((TypeError, ValueError)):
        RegistryLocationV1(registry_file=path, state_root="state")
    with pytest.raises((TypeError, ValueError)):
        RegistryLocationV1(registry_file="registry.json", state_root=path)
    with pytest.raises((TypeError, ValueError)):
        RelativeDbPathV1(alias="main", path=path)


@pytest.mark.parametrize("alias", ["", "Main", "1main", "has.dot", "has/slash", "a" * 65])
def test_alias_validation(alias):
    with pytest.raises(ValueError):
        RelativeDbPathV1(alias=alias, path="main.sqlite")


def test_resolution_is_registry_relative_not_cwd_relative_and_relocatable(tmp_path, monkeypatch):
    root_a = tmp_path / f"root-a-{uuid4().hex}"
    root_b = tmp_path / f"root-b-{uuid4().hex}"
    elsewhere = tmp_path / "elsewhere"
    root_a.mkdir()
    root_b.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    value = RegistryLocationV1(registry_file="config/workflows.registry.json", state_root="state")
    db = RelativeDbPathV1(alias="main", path="db/workflows.sqlite")

    resolved_a = resolve_registry_location(root_a, value)
    resolved_b = resolve_registry_location(root_b, value)

    assert resolved_a == ResolvedRegistryLocationV1(
        registry_path=str(root_a / "config" / "workflows.registry.json"),
        registry_dir=str(root_a / "config"),
        state_root_path=str(root_a / "config" / "state"),
    )
    assert resolve_relative_db_path(resolved_a, db) == str(root_a / "config" / "state" / "db" / "workflows.sqlite")
    assert resolve_relative_db_path(resolved_b, db) == str(root_b / "config" / "state" / "db" / "workflows.sqlite")
    assert Path(resolve_relative_db_path(resolved_a, db)).relative_to(root_a) == Path(
        resolve_relative_db_path(resolved_b, db)
    ).relative_to(root_b)
    assert resolved_a.to_dict() == {
        "schema_version": 1,
        "registry_path": str(root_a / "config" / "workflows.registry.json"),
        "registry_dir": str(root_a / "config"),
        "state_root_path": str(root_a / "config" / "state"),
    }


def test_resolution_requires_absolute_config_root(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        resolve_registry_location(Path("relative"), RegistryLocationV1(registry_file="registry.json", state_root="state"))


def test_resolution_fails_closed_on_symlink_escape(tmp_path):
    config_root = tmp_path / "config"
    outside = tmp_path / "outside"
    config_root.mkdir()
    outside.mkdir()
    (config_root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escape"):
        resolve_registry_location(
            config_root,
            RegistryLocationV1(registry_file="linked/registry.json", state_root="state"),
        )

    registry_dir = config_root / "registry"
    registry_dir.mkdir()
    (registry_dir / "linked-state").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escape"):
        resolve_registry_location(
            config_root,
            RegistryLocationV1(registry_file="registry/workflows.json", state_root="linked-state"),
        )

    state_root = registry_dir / "state"
    state_root.mkdir()
    (state_root / "linked-db").symlink_to(outside, target_is_directory=True)
    resolved = resolve_registry_location(
        config_root,
        RegistryLocationV1(registry_file="registry/workflows.json", state_root="state"),
    )
    with pytest.raises(ValueError, match="escape"):
        resolve_relative_db_path(resolved, RelativeDbPathV1(alias="main", path="linked-db/workflows.sqlite"))


def test_registry_resolution_fails_closed_when_registry_dir_swaps_during_receipt_containment(tmp_path, monkeypatch):
    config_root = tmp_path / "config"
    registry_dir = config_root / "registry"
    saved_registry_dir = config_root / "saved-registry"
    outside = tmp_path / "outside"
    registry_dir.mkdir(parents=True)
    outside.mkdir()
    original_require_contained = registry_location._require_contained
    swapped = False

    def swap_before_registry_path_check(parent, candidate, *, label):
        nonlocal swapped
        if label == "registry_path" and not swapped:
            registry_dir.rename(saved_registry_dir)
            registry_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        original_require_contained(parent, candidate, label=label)

    monkeypatch.setattr(registry_location, "_require_contained", swap_before_registry_path_check)

    with pytest.raises(ValueError, match="escape|symlink-resolved"):
        resolve_registry_location(
            config_root,
            RegistryLocationV1(registry_file="registry/workflows.json", state_root="state"),
        )


def test_registry_resolution_fails_closed_on_same_root_swap_during_final_validation(tmp_path, monkeypatch):
    config_root = tmp_path / "config"
    registry_dir = config_root / "registry"
    saved_registry_dir = config_root / "saved-registry"
    alternate_registry_dir = config_root / "alternate-registry"
    registry_dir.mkdir(parents=True)
    alternate_registry_dir.mkdir()
    original_require_contained = registry_location._require_contained
    swapped = False

    def swap_during_final_registry_path_check(parent, candidate, *, label):
        nonlocal swapped
        if label == "registry_path" and parent == config_root and not swapped:
            registry_dir.rename(saved_registry_dir)
            registry_dir.symlink_to(alternate_registry_dir, target_is_directory=True)
            swapped = True
        original_require_contained(parent, candidate, label=label)

    monkeypatch.setattr(registry_location, "_require_contained", swap_during_final_registry_path_check)

    with pytest.raises(ValueError, match="symlink-resolved"):
        resolve_registry_location(
            config_root,
            RegistryLocationV1(registry_file="registry/workflows.json", state_root="state"),
        )


def test_registry_resolution_fails_closed_on_same_root_swap_during_final_receipt(tmp_path, monkeypatch):
    config_root = tmp_path / "config"
    registry_dir = config_root / "registry"
    saved_registry_dir = config_root / "saved-registry"
    alternate_registry_dir = config_root / "alternate-registry"
    registry_dir.mkdir(parents=True)
    alternate_registry_dir.mkdir()
    original_require_contained = registry_location._require_contained
    registry_path_checks = 0

    def swap_during_final_receipt_registry_path_check(parent, candidate, *, label):
        nonlocal registry_path_checks
        if label == "registry_path":
            registry_path_checks += 1
            if registry_path_checks == 3:
                registry_dir.rename(saved_registry_dir)
                registry_dir.symlink_to(alternate_registry_dir, target_is_directory=True)
        original_require_contained(parent, candidate, label=label)

    monkeypatch.setattr(registry_location, "_require_contained", swap_during_final_receipt_registry_path_check)

    with pytest.raises(ValueError, match="symlink-resolved"):
        resolve_registry_location(
            config_root,
            RegistryLocationV1(registry_file="registry/workflows.json", state_root="state"),
        )


def test_db_resolution_rechecks_state_root_containment_at_use_time(tmp_path):
    registry_dir = tmp_path / "registry"
    state_root = registry_dir / "state"
    outside = tmp_path / "outside"
    state_root.mkdir(parents=True)
    outside.mkdir()
    resolved = resolve_registry_location(
        tmp_path,
        RegistryLocationV1(registry_file="registry/workflows.json", state_root="state"),
    )
    state_root.rmdir()
    state_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        resolve_relative_db_path(resolved, RelativeDbPathV1(alias="main", path="workflows.sqlite"))


def test_db_resolution_fails_closed_when_state_root_swaps_during_resolution(tmp_path, monkeypatch):
    registry_dir = tmp_path / "registry"
    state_root = registry_dir / "state"
    saved_state_root = registry_dir / "saved-state"
    outside = tmp_path / "outside"
    state_root.mkdir(parents=True)
    outside.mkdir()
    resolved = resolve_registry_location(
        tmp_path,
        RegistryLocationV1(registry_file="registry/workflows.json", state_root="state"),
    )
    original_require_contained = registry_location._require_contained

    def swap_after_state_root_check(parent, candidate, *, label):
        original_require_contained(parent, candidate, label=label)
        if label == "state_root_path":
            state_root.rename(saved_state_root)
            state_root.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(registry_location, "_require_contained", swap_after_state_root_check)

    with pytest.raises(ValueError, match="escape"):
        resolve_relative_db_path(resolved, RelativeDbPathV1(alias="main", path="escaped.sqlite"))


def test_db_resolution_fails_closed_when_state_root_swaps_after_candidate_resolution(tmp_path, monkeypatch):
    registry_dir = tmp_path / "registry"
    state_root = registry_dir / "state"
    saved_state_root = registry_dir / "saved-state"
    outside = tmp_path / "outside"
    state_root.mkdir(parents=True)
    outside.mkdir()
    resolved = resolve_registry_location(
        tmp_path,
        RegistryLocationV1(registry_file="registry/workflows.json", state_root="state"),
    )
    original_require_contained = registry_location._require_contained
    swapped = False

    def swap_before_candidate_check(parent, candidate, *, label):
        nonlocal swapped
        if label == "DB path for alias 'main'" and not swapped:
            state_root.rename(saved_state_root)
            state_root.symlink_to(outside, target_is_directory=True)
            swapped = True
        original_require_contained(parent, candidate, label=label)

    monkeypatch.setattr(registry_location, "_require_contained", swap_before_candidate_check)

    with pytest.raises(ValueError, match="escape"):
        resolve_relative_db_path(resolved, RelativeDbPathV1(alias="main", path="escaped.sqlite"))


def test_duplicate_db_aliases_fail_closed(tmp_path):
    resolved = resolve_registry_location(
        tmp_path,
        RegistryLocationV1(registry_file="registry/workflows.json", state_root="state"),
    )
    values = [
        RelativeDbPathV1(alias="main", path="one.sqlite"),
        RelativeDbPathV1(alias="main", path="two.sqlite"),
    ]

    with pytest.raises(ValueError, match="duplicate alias"):
        resolve_relative_db_paths(resolved, values)

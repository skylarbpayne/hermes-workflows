import hashlib
import json
import subprocess
import sys
import zipfile
from dataclasses import FrozenInstanceError
from importlib import metadata
from pathlib import Path

import pytest

from hermes_workflows.package_resources import (
    PackageResourceFileV1,
    PackageResourceManifestV1,
    canonical_manifest_json,
    foundation_manifest,
    installed_package_version,
    manifest_from_json,
    ownership_key,
    write_package_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "plugin_payload_manifest.v1.json"


def _file(path="plugin_payload/example.txt", sha256="a" * 64, size_bytes=1):
    return PackageResourceFileV1(
        schema_version=1,
        path=path,
        sha256=sha256,
        size_bytes=size_bytes,
    )


def _manifest(**overrides):
    values = {
        "schema_version": 1,
        "owner_id": "hermes-workflows",
        "package_name": "hermes-workflows",
        "package_version": installed_package_version(),
        "payload_root": "plugin_payload",
        "files": (),
    }
    values.update(overrides)
    return PackageResourceManifestV1(**values)


class _HostileFile(PackageResourceFileV1):
    def to_dict(self):
        return {
            "schema_version": 2,
            "path": "../escaped.txt",
            "sha256": "not-a-sha256",
            "size_bytes": -1,
        }


class _HostileManifest(PackageResourceManifestV1):
    def to_dict(self):
        return {
            "schema_version": 2,
            "owner_id": "attacker",
            "package_name": "attacker",
            "package_version": "999.0.0",
            "payload_root": "../escaped",
            "files": [],
        }


def test_foundation_manifest_is_frozen_empty_and_canonical():
    manifest = foundation_manifest()

    assert manifest == _manifest()
    assert manifest.files == ()
    with pytest.raises(FrozenInstanceError):
        manifest.owner_id = "other"

    expected = {
        "files": [],
        "owner_id": "hermes-workflows",
        "package_name": "hermes-workflows",
        "package_version": installed_package_version(),
        "payload_root": "plugin_payload",
        "schema_version": 1,
    }
    canonical = canonical_manifest_json(manifest)
    assert canonical == json.dumps(expected, sort_keys=True, separators=(",", ":"))
    assert ownership_key(manifest) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_canonical_serialization_and_ownership_reject_manifest_subclasses():
    hostile = _HostileManifest(**_manifest().__dict__)

    with pytest.raises(TypeError, match="PackageResourceManifestV1"):
        canonical_manifest_json(hostile)
    with pytest.raises(TypeError, match="PackageResourceManifestV1"):
        ownership_key(hostile)


def test_canonical_serialization_and_ownership_reject_nested_file_subclasses():
    hostile_file = _HostileFile(**_file().__dict__)
    manifest = _manifest(files=(hostile_file,))

    with pytest.raises(TypeError, match="PackageResourceFileV1"):
        canonical_manifest_json(manifest)
    with pytest.raises(TypeError, match="PackageResourceFileV1"):
        ownership_key(manifest)


def test_manifest_resource_is_present_and_canonical_in_built_wheel(tmp_path):
    outdir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wheels = list(outdir.glob("*.whl"))
    assert len(wheels) == 1

    member = "hermes_workflows/" + MANIFEST_NAME
    with zipfile.ZipFile(wheels[0]) as archive:
        assert member in archive.namelist()
        resource_text = archive.read(member).decode("utf-8")

    assert resource_text == canonical_manifest_json(foundation_manifest())


def test_canonical_json_round_trips_and_rejects_unknown_fields():
    manifest = _manifest(files=(_file(),))
    canonical = canonical_manifest_json(manifest)

    assert manifest_from_json(canonical) == manifest
    payload = json.loads(canonical)
    payload["unknown"] = True
    with pytest.raises(ValueError, match="exactly match"):
        manifest_from_json(json.dumps(payload))

    payload.pop("unknown")
    payload["files"][0]["unknown"] = True
    with pytest.raises(ValueError, match="exactly match"):
        manifest_from_json(json.dumps(payload))


@pytest.mark.parametrize(
    "duplicate_json",
    [
        lambda canonical: canonical.replace(
            '"owner_id":"hermes-workflows"',
            '"owner_id":"hermes-workflows","owner_id":"hermes-workflows"',
        ),
        lambda canonical: canonical.replace(
            '"path":"plugin_payload/example.txt"',
            '"path":"plugin_payload/example.txt","path":"plugin_payload/example.txt"',
        ),
    ],
    ids=["manifest-object", "resource-file-object"],
)
def test_manifest_json_rejects_duplicate_keys_at_every_object_boundary(duplicate_json):
    canonical = canonical_manifest_json(_manifest(files=(_file(),)))

    with pytest.raises(ValueError, match="canonical JSON"):
        manifest_from_json(duplicate_json(canonical))


@pytest.mark.parametrize(
    "noncanonical_json",
    [
        lambda manifest, canonical: canonical.replace(",", ", ", 1),
        lambda manifest, canonical: json.dumps(
            manifest.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ),
    ],
    ids=["whitespace", "key-order"],
)
def test_manifest_json_rejects_noncanonical_byte_spellings(noncanonical_json):
    manifest = _manifest(files=(_file(),))
    canonical = canonical_manifest_json(manifest)
    value = noncanonical_json(manifest, canonical)
    assert json.loads(value) == json.loads(canonical)
    assert value != canonical

    with pytest.raises(ValueError, match="canonical JSON"):
        manifest_from_json(value)


@pytest.mark.parametrize("field", ["owner_id", "package_name"])
@pytest.mark.parametrize(
    "value",
    ["", "UPPER", "has space", "-leading", "x" * 129],
)
def test_owner_and_package_identifiers_are_validated(field, value):
    with pytest.raises(ValueError):
        _manifest(**{field: value})


def test_package_version_must_match_installed_distribution():
    with pytest.raises(ValueError, match="installed distribution version"):
        _manifest(package_version="999.0.0")
    with pytest.raises(ValueError):
        _manifest(package_version=" ")


def test_manifest_constructor_rejects_alternate_installed_distribution():
    pytest_version = metadata.version("pytest")

    with pytest.raises(ValueError, match="package_name must equal hermes-workflows"):
        _manifest(package_name="pytest", package_version=pytest_version)


def test_manifest_decoder_rejects_alternate_installed_distribution():
    payload = _manifest().to_dict()
    payload["package_name"] = "pytest"
    payload["package_version"] = metadata.version("pytest")
    alternate_distribution_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    with pytest.raises(ValueError, match="package_name must equal hermes-workflows"):
        manifest_from_json(alternate_distribution_json)


@pytest.mark.parametrize(
    "path",
    [
        "../plugin_payload/example.txt",
        "plugin_payload/../example.txt",
        "plugin_payload/./example.txt",
        "plugin_payload//example.txt",
        "plugin_payload\\example.txt",
        "plugin_payload/\x00example.txt",
        "/plugin_payload/example.txt",
        "plugin_payload",
        "other/example.txt",
        "plugin_payload/example.txt/",
    ],
)
def test_resource_paths_must_be_normalized_relative_posix_beneath_payload_root(path):
    with pytest.raises(ValueError):
        _manifest(files=(_file(path=path),))


@pytest.mark.parametrize(
    "payload_root",
    ["", ".", "../plugin_payload", "/plugin_payload", "plugin_payload/", "plugin\\payload"],
)
def test_payload_root_must_be_a_normalized_relative_posix_path(payload_root):
    with pytest.raises(ValueError):
        _manifest(payload_root=payload_root)


def test_file_entries_must_be_unique_and_lexicographically_sorted():
    first = _file(path="plugin_payload/a.txt")
    second = _file(path="plugin_payload/b.txt")

    assert _manifest(files=(first, second)).files == (first, second)
    with pytest.raises(ValueError, match="sorted"):
        _manifest(files=(second, first))
    with pytest.raises(ValueError, match="duplicate"):
        _manifest(files=(first, first))


@pytest.mark.parametrize("sha256", ["A" * 64, "a" * 63, "g" * 64, ""])
def test_resource_hash_must_be_lowercase_sha256(sha256):
    with pytest.raises(ValueError):
        _file(sha256=sha256)


@pytest.mark.parametrize("size_bytes", [-1, 1.5, True])
def test_resource_size_must_be_a_nonnegative_integer(size_bytes):
    with pytest.raises(ValueError):
        _file(size_bytes=size_bytes)


def test_all_schema_versions_must_equal_one():
    with pytest.raises(ValueError):
        _file().__class__(2, "plugin_payload/example.txt", "a" * 64, 1)
    with pytest.raises(ValueError):
        _manifest(schema_version=2)


def test_empty_payload_performs_no_filesystem_write(tmp_path):
    destination = tmp_path / "must-not-exist"

    written = write_package_payload(foundation_manifest(), destination)

    assert written == ()
    assert not destination.exists()

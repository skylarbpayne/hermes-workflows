from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_workflows import plugin_install


REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = REPO_ROOT / "src" / "hermes_workflows" / "plugin_payload" / plugin_install.PLUGIN_NAME
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "plugin_manifest_v1.json"


def _versioned_payload(tmp_path: Path, version: str, marker: str) -> Path:
    destination = tmp_path / ("payload-" + version.replace(".", "-"))
    shutil.copytree(PAYLOAD_ROOT, destination)
    plugin_yaml = destination / "plugin.yaml"
    plugin_yaml.write_text(
        plugin_yaml.read_text(encoding="utf-8").replace(plugin_install.PACKAGE_VERSION, version),
        encoding="utf-8",
    )
    manifest_path = destination / "dashboard" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    style_path = destination / "dashboard" / "dist" / "style.css"
    style_path.write_text(style_path.read_text(encoding="utf-8") + f"\n/* {marker} */\n", encoding="utf-8")
    return destination


def _installed(profile: Path) -> Path:
    return profile / "plugins" / plugin_install.PLUGIN_NAME


def _tree_snapshot(root: Path):
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", None)
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def test_dangling_config_symlink_is_refused_without_any_profile_mutation(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.mkdir()
    config = profile / "config.yaml"
    missing_target = tmp_path / "missing-config-target"
    config.symlink_to(missing_target)
    before = _tree_snapshot(profile)

    with pytest.raises(plugin_install.UserFileConflictError, match="config.yaml.*non-symlink"):
        plugin_install.install_plugin(profile)

    assert config.is_symlink()
    assert os.readlink(config) == str(missing_target)
    assert not missing_target.exists()
    assert _tree_snapshot(profile) == before


def test_dangling_plugin_root_symlink_is_refused_without_any_profile_mutation(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.mkdir()
    config = profile / "config.yaml"
    config.write_bytes(b"model:\n  default: user/model\n")
    plugins = profile / "plugins"
    missing_target = tmp_path / "missing-plugin-root"
    plugins.symlink_to(missing_target, target_is_directory=True)
    before = _tree_snapshot(profile)

    with pytest.raises(plugin_install.UserFileConflictError, match="plugin root.*symlink"):
        plugin_install.install_plugin(profile)

    assert plugins.is_symlink()
    assert os.readlink(plugins) == str(missing_target)
    assert not missing_target.exists()
    assert _tree_snapshot(profile) == before


@pytest.mark.parametrize(
    "managed_name",
    [plugin_install.PLUGIN_NAME, f".{plugin_install.PLUGIN_NAME}.rollback"],
)
def test_dangling_managed_plugin_symlink_is_refused_without_any_profile_mutation(
    tmp_path: Path,
    managed_name: str,
):
    profile = tmp_path / "profile"
    plugins = profile / "plugins"
    plugins.mkdir(parents=True)
    config = profile / "config.yaml"
    config.write_bytes(b"model:\n  default: user/model\n")
    managed_path = plugins / managed_name
    missing_target = tmp_path / f"missing-{managed_name}"
    managed_path.symlink_to(missing_target, target_is_directory=True)
    before = _tree_snapshot(profile)

    with pytest.raises(plugin_install.UserFileConflictError, match="plugin.*symlink"):
        plugin_install.install_plugin(profile)

    assert managed_path.is_symlink()
    assert os.readlink(managed_path) == str(missing_target)
    assert not missing_target.exists()
    assert _tree_snapshot(profile) == before


@pytest.mark.parametrize("action", ["install", "upgrade", "rollback", "uninstall", "discovery"])
def test_symlinked_profile_root_is_rejected_without_touching_target(tmp_path: Path, action: str):
    target = tmp_path / "profile-target"
    target.mkdir()
    (target / "user-file.txt").write_bytes(b"must remain byte-for-byte unchanged\n")

    if action == "rollback":
        old = _versioned_payload(tmp_path, "0.0.1rc0", "symlink-root-rollback")
        plugin_install.install_plugin(target, payload_root=old, expected_package_version="0.0.1rc0")
        plugin_install.upgrade_plugin(target)
    elif action != "install":
        plugin_install.install_plugin(target)

    profile = tmp_path / "supplied-profile"
    profile.symlink_to(target, target_is_directory=True)
    before = _tree_snapshot(target)

    with pytest.raises(plugin_install.UserFileConflictError, match="profile home.*symlink"):
        if action == "install":
            plugin_install.install_plugin(profile)
        elif action == "upgrade":
            plugin_install.upgrade_plugin(profile)
        elif action == "rollback":
            plugin_install.rollback_plugin(profile)
        elif action == "uninstall":
            plugin_install.uninstall_plugin(profile)
        else:
            plugin_install.discover_installed_plugin(profile)

    assert _tree_snapshot(target) == before


def test_canonical_payload_has_one_version_and_all_dashboard_surfaces():
    payload = plugin_install.inspect_payload(PAYLOAD_ROOT)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert payload.package_version == plugin_install.PACKAGE_VERSION
    assert payload.plugin_version == plugin_install.PACKAGE_VERSION
    assert list(payload.files) == fixture["owned_paths"]
    assert payload.dashboard_manifest["name"] == plugin_install.PLUGIN_NAME
    assert payload.dashboard_manifest["api"] == "plugin_api.py"
    assert payload.dashboard_manifest["entry"] == "dist/index.js"
    assert payload.dashboard_manifest["css"] == "dist/style.css"
    assert "__HERMES_PLUGINS__.register" in (PAYLOAD_ROOT / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")
    assert (PAYLOAD_ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8").find("router = APIRouter()") >= 0


def test_install_is_profile_scoped_atomic_enabled_and_reports_reload_contract(tmp_path: Path):
    profile = tmp_path / "fresh-profile"

    report = plugin_install.install_plugin(profile)
    discovery = plugin_install.discover_installed_plugin(profile)
    receipt = json.loads((_installed(profile) / plugin_install.RECEIPT_NAME).read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert report.action == "install"
    assert report.enabled is True
    assert report.restart_required is True
    assert report.rescan_supported is True
    assert report.rescan_endpoint == "/api/dashboard/plugins/rescan"
    assert discovery.plugin_name == plugin_install.PLUGIN_NAME
    assert discovery.api_route == "/api/plugins/hermes-workflows-approvals"
    assert discovery.asset_routes == (
        "/dashboard-plugins/hermes-workflows-approvals/dist/index.js",
        "/dashboard-plugins/hermes-workflows-approvals/dist/style.css",
    )
    assert set(receipt) == set(fixture["receipt_fields"])
    assert [item["path"] for item in receipt["files"]] == fixture["owned_paths"]
    config = (profile / "config.yaml").read_text(encoding="utf-8")
    assert "plugins:" in config
    assert "enabled:" in config
    assert f"- {plugin_install.PLUGIN_NAME}" in config
    assert not list((profile / "plugins").glob(f".{plugin_install.PLUGIN_NAME}.stage-*"))


def test_enablement_preserves_other_config_and_removes_explicit_disable(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "model:\n  default: test/model\nplugins:\n  enabled:\n    - other-plugin\n  disabled:\n    - hermes-workflows-approvals\n    - noisy-plugin\n",
        encoding="utf-8",
    )

    plugin_install.install_plugin(profile)

    config = (profile / "config.yaml").read_text(encoding="utf-8")
    assert "default: test/model" in config
    assert "- other-plugin" in config
    assert "- noisy-plugin" in config
    assert config.count("- hermes-workflows-approvals") == 1


def test_unsupported_inline_plugin_config_is_refused_without_mutation(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.mkdir()
    config_path = profile / "config.yaml"
    original = "plugins: {enabled: [other-plugin]}\n"
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(plugin_install.UserFileConflictError, match="block mapping"):
        plugin_install.install_plugin(profile)

    assert config_path.read_text(encoding="utf-8") == original
    assert not _installed(profile).exists()


def test_upgrade_retains_one_owned_rollback_and_rollback_swaps_versions(tmp_path: Path):
    profile = tmp_path / "profile"
    old = _versioned_payload(tmp_path, "0.0.1rc0", "old-payload")
    plugin_install.install_plugin(profile, payload_root=old, expected_package_version="0.0.1rc0")

    upgraded = plugin_install.upgrade_plugin(profile)
    assert upgraded.previous_version == "0.0.1rc0"
    assert upgraded.plugin_version == plugin_install.PACKAGE_VERSION
    assert upgraded.rollback_available is True
    assert "old-payload" not in (_installed(profile) / "dashboard" / "dist" / "style.css").read_text(encoding="utf-8")

    rolled_back = plugin_install.rollback_plugin(profile)
    assert rolled_back.action == "rollback"
    assert rolled_back.plugin_version == "0.0.1rc0"
    assert rolled_back.previous_version == plugin_install.PACKAGE_VERSION
    assert "old-payload" in (_installed(profile) / "dashboard" / "dist" / "style.css").read_text(encoding="utf-8")


def test_interrupted_upgrade_restores_previous_install_and_cleans_stage(tmp_path: Path, monkeypatch):
    profile = tmp_path / "profile"
    old = _versioned_payload(tmp_path, "0.0.1rc0", "survives-interruption")
    plugin_install.install_plugin(profile, payload_root=old, expected_package_version="0.0.1rc0")
    real_replace = plugin_install.os.replace

    def fail_stage_promotion(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(f".{plugin_install.PLUGIN_NAME}.stage-") and destination_path.name == plugin_install.PLUGIN_NAME:
            raise OSError("simulated interruption")
        return real_replace(source, destination)

    monkeypatch.setattr(plugin_install.os, "replace", fail_stage_promotion)
    with pytest.raises(plugin_install.PluginInstallError, match="simulated interruption"):
        plugin_install.upgrade_plugin(profile)

    current = plugin_install.discover_installed_plugin(profile)
    assert current.plugin_version == "0.0.1rc0"
    assert "survives-interruption" in (_installed(profile) / "dashboard" / "dist" / "style.css").read_text(encoding="utf-8")
    assert not list((profile / "plugins").glob(f".{plugin_install.PLUGIN_NAME}.stage-*"))


def test_recovery_removes_only_receipted_stale_stage(tmp_path: Path):
    profile = tmp_path / "profile"
    plugin_install.install_plugin(profile)
    stage = profile / "plugins" / f".{plugin_install.PLUGIN_NAME}.stage-stale"
    shutil.copytree(_installed(profile), stage)

    report = plugin_install.upgrade_plugin(profile)

    assert report.plugin_version == plugin_install.PACKAGE_VERSION
    assert not stage.exists()


def test_recovery_restores_receipted_tree_from_interrupted_uninstall(tmp_path: Path):
    profile = tmp_path / "profile"
    plugin_install.install_plugin(profile)
    destination = _installed(profile)
    interrupted = profile / "plugins" / f".{plugin_install.PLUGIN_NAME}.remove-current-interrupted"
    os.replace(destination, interrupted)

    report = plugin_install.upgrade_plugin(profile)

    assert report.previous_version == plugin_install.PACKAGE_VERSION
    assert destination.exists()
    assert not interrupted.exists()


def test_recovery_completes_interrupted_rollback_swap_safely(tmp_path: Path):
    profile = tmp_path / "profile"
    old = _versioned_payload(tmp_path, "0.0.1rc0", "rollback-after-crash")
    plugin_install.install_plugin(profile, payload_root=old, expected_package_version="0.0.1rc0")
    plugin_install.upgrade_plugin(profile)
    destination = _installed(profile)
    interrupted = profile / "plugins" / f".{plugin_install.PLUGIN_NAME}.swap-interrupted"
    os.replace(destination, interrupted)

    report = plugin_install.rollback_plugin(profile)

    assert report.plugin_version == "0.0.1rc0"
    assert destination.exists()
    assert not interrupted.exists()


def test_corrupt_or_stale_receipt_blocks_upgrade_and_uninstall_without_touching_files(tmp_path: Path):
    profile = tmp_path / "profile"
    plugin_install.install_plugin(profile)
    destination = _installed(profile)
    manifest_before = (destination / "dashboard" / "manifest.json").read_bytes()
    (destination / plugin_install.RECEIPT_NAME).write_text("{not-json", encoding="utf-8")

    with pytest.raises(plugin_install.OwnershipError):
        plugin_install.upgrade_plugin(profile)
    with pytest.raises(plugin_install.OwnershipError):
        plugin_install.uninstall_plugin(profile)

    assert destination.exists()
    assert (destination / "dashboard" / "manifest.json").read_bytes() == manifest_before


def test_modified_owned_file_is_treated_as_stale_and_never_replaced(tmp_path: Path):
    profile = tmp_path / "profile"
    plugin_install.install_plugin(profile)
    style = _installed(profile) / "dashboard" / "dist" / "style.css"
    style.write_text(style.read_text(encoding="utf-8") + "\n/* user edit */\n", encoding="utf-8")

    with pytest.raises(plugin_install.OwnershipError, match="no longer matches"):
        plugin_install.upgrade_plugin(profile)

    assert style.read_text(encoding="utf-8").endswith("/* user edit */\n")


def test_user_owned_destination_and_added_file_are_never_overwritten_or_deleted(tmp_path: Path):
    profile = tmp_path / "profile"
    destination = _installed(profile)
    destination.mkdir(parents=True)
    note = destination / "user-note.txt"
    note.write_text("mine", encoding="utf-8")

    with pytest.raises(plugin_install.UserFileConflictError):
        plugin_install.install_plugin(profile)
    assert note.read_text(encoding="utf-8") == "mine"

    shutil.rmtree(destination)
    plugin_install.install_plugin(profile)
    note = _installed(profile) / "user-note.txt"
    note.write_text("mine", encoding="utf-8")
    with pytest.raises(plugin_install.UserFileConflictError):
        plugin_install.uninstall_plugin(profile)
    assert note.read_text(encoding="utf-8") == "mine"


def test_uninstall_removes_only_verified_owned_install_and_enablement(tmp_path: Path):
    profile = tmp_path / "profile"
    plugin_install.install_plugin(profile)

    report = plugin_install.uninstall_plugin(profile)

    assert report.action == "uninstall"
    assert report.enabled is False
    assert not _installed(profile).exists()
    assert not (profile / "plugins" / f".{plugin_install.PLUGIN_NAME}.rollback").exists()
    assert plugin_install.PLUGIN_NAME not in (profile / "config.yaml").read_text(encoding="utf-8")


def test_payload_and_receipt_traversal_or_symlink_escape_are_refused(tmp_path: Path):
    hostile = tmp_path / "hostile-payload"
    shutil.copytree(PAYLOAD_ROOT, hostile)
    style = hostile / "dashboard" / "dist" / "style.css"
    style.unlink()
    outside = tmp_path / "outside.css"
    outside.write_text("outside", encoding="utf-8")
    style.symlink_to(outside)

    with pytest.raises(plugin_install.PayloadValidationError):
        plugin_install.install_plugin(tmp_path / "profile-a", payload_root=hostile)

    profile = tmp_path / "profile-b"
    plugin_install.install_plugin(profile)
    receipt_path = _installed(profile) / plugin_install.RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["files"][0]["path"] = "../escape"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(plugin_install.OwnershipError):
        plugin_install.uninstall_plugin(profile)
    assert _installed(profile).exists()


def test_payload_version_mismatch_is_rejected_before_profile_mutation(tmp_path: Path):
    hostile = _versioned_payload(tmp_path, "9.9.9", "mismatch")
    manifest_path = hostile / "dashboard" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "8.8.8"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    profile = tmp_path / "profile"

    with pytest.raises(plugin_install.PayloadValidationError, match="version"):
        plugin_install.install_plugin(profile, payload_root=hostile, expected_package_version="9.9.9")
    assert not profile.exists()


def test_fresh_profile_probe_exercises_discovery_upgrade_rollback_and_uninstall():
    completed = subprocess.run(
        [sys.executable, "tests/probes/fresh_profile_plugin.py"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    receipt = json.loads(completed.stdout)

    assert receipt["temporary_profile"] is True
    assert receipt["discovered"]["plugin_name"] == plugin_install.PLUGIN_NAME
    assert receipt["upgrade"]["plugin_version"] == plugin_install.PACKAGE_VERSION
    assert receipt["rollback"]["plugin_version"] == "0.0.1rc0"
    assert receipt["uninstall"]["action"] == "uninstall"
    assert receipt["live_profile_mutated"] is False
    assert receipt["wheel_payload_verified"] is False

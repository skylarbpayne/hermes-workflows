from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from hermes_workflows import plugin_install


def _scratch_base():
    if sys.platform != "darwin":
        return None

    base = Path("/private/tmp")
    try:
        resolved = base.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("macOS probe scratch base /private/tmp is unavailable") from exc
    if resolved != base or base.is_symlink() or not base.is_dir():
        raise RuntimeError("macOS probe scratch base /private/tmp must be a real non-symlink directory")
    if not os.access(base, os.W_OK | os.X_OK):
        raise RuntimeError("macOS probe scratch base /private/tmp must be writable and searchable")
    return str(base)


def _old_payload(root: Path) -> Path:
    source = plugin_install.canonical_payload_root()
    destination = root / "old-payload"
    shutil.copytree(source, destination)
    plugin_yaml = destination / "plugin.yaml"
    plugin_yaml.write_text(
        plugin_yaml.read_text(encoding="utf-8").replace(plugin_install.PACKAGE_VERSION, "0.0.1rc0"),
        encoding="utf-8",
    )
    manifest_path = destination / "dashboard" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.0.1rc0"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    style = destination / "dashboard" / "dist" / "style.css"
    style.write_text(style.read_text(encoding="utf-8") + "\n/* probe-old */\n", encoding="utf-8")
    return destination


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hermes-workflows-plugin-probe-", dir=_scratch_base()) as raw:
        root = Path(raw)
        profile = root / "profile"
        old_payload = _old_payload(root)
        plugin_install.install_plugin(profile, payload_root=old_payload, expected_package_version="0.0.1rc0")
        upgrade = plugin_install.upgrade_plugin(profile)
        discovered = plugin_install.discover_installed_plugin(profile)
        rollback = plugin_install.rollback_plugin(profile)
        uninstall = plugin_install.uninstall_plugin(profile)
        result = {
            "scratch_root": str(root),
            "temporary_profile": True,
            "live_profile_mutated": False,
            "wheel_payload_verified": False,
            "deferred_wheel_gate": "INT-PKG-META and INT-PKG-ASSETS",
            "discovered": discovered.to_dict(),
            "upgrade": upgrade.to_dict(),
            "rollback": rollback.to_dict(),
            "uninstall": uninstall.to_dict(),
            "profile_removed": not (profile / "plugins" / plugin_install.PLUGIN_NAME).exists(),
        }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

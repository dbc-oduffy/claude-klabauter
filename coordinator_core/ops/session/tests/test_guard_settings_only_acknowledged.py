"""
Tests for the settings-only acknowledgement in `detect_hook_delivery_duplication`: a
settings-only hook is exempt when allowlisted in `hooks.settings_only_acknowledged` or
when it lives under a directory-source marketplace plugin install; any other
settings-only hook still raises the banner.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core import machine_resolver
from coordinator_core.ops.session import guard_settings_integrity as gsi


def _fixture(tmp_path: Path, monkeypatch, *, ack=None, directory_source=False):
    content_root = tmp_path / "plugin-root"
    (content_root / "hooks" / "scripts").mkdir(parents=True)
    (content_root / "hooks" / "scripts" / "foo.py").write_text("", encoding="utf-8")
    (content_root / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/foo.py"}
        ]}]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gsi, "resolve_content_root", lambda: str(content_root))

    install = tmp_path / "plugins" / "example-game-repo"
    only = install / "hooks" / "scripts" / "nudge.py"
    only.parent.mkdir(parents=True)
    only.write_text("", encoding="utf-8")

    config_dir = tmp_path / "config"
    (config_dir / "plugins").mkdir(parents=True)
    settings = {
        "enabledPlugins": {"foo@bar": True},
        "hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": f"python3 {content_root / 'hooks/scripts/foo.py'}"},
            {"type": "command", "command": f"python3 {only}"},
        ]}]},
    }
    if directory_source:
        settings["extraKnownMarketplaces"] = {
            "example-game-repo-mkt": {"source": {"source": "directory", "path": str(tmp_path / "mkt")}}
        }
        (config_dir / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"example-game-repo@example-game-repo-mkt": [{"installPath": str(install)}]}}),
            encoding="utf-8",
        )
    (config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    monkeypatch.setattr(
        machine_resolver,
        "registry_get",
        lambda key: ack if key == "hooks.settings_only_acknowledged" else None,
    )
    return config_dir, only


def test_unacknowledged_settings_only_still_raises(tmp_path, monkeypatch):
    config_dir, only = _fixture(tmp_path, monkeypatch)
    report = gsi.detect_hook_delivery_duplication(config_dir)
    assert report.settings_only_scripts == [str(only.resolve())]
    assert "declared ONLY in settings.json" in gsi.evaluate_hook_delivery_duplication(config_dir)


def test_allowlisted_basename_is_exempt(tmp_path, monkeypatch):
    config_dir, _ = _fixture(tmp_path, monkeypatch, ack="other.py\nnudge.py")
    assert gsi.detect_hook_delivery_duplication(config_dir).settings_only_scripts == []
    assert "declared ONLY in settings.json" not in gsi.evaluate_hook_delivery_duplication(config_dir)


def test_allowlist_does_not_exempt_a_different_basename(tmp_path, monkeypatch):
    config_dir, only = _fixture(tmp_path, monkeypatch, ack="other.py")
    assert gsi.detect_hook_delivery_duplication(config_dir).settings_only_scripts == [
        str(only.resolve())
    ]


def test_directory_source_plugin_install_is_exempt(tmp_path, monkeypatch):
    config_dir, _ = _fixture(tmp_path, monkeypatch, directory_source=True)
    assert gsi.detect_hook_delivery_duplication(config_dir).settings_only_scripts == []


def test_directory_source_exemption_leaves_double_fire_detection_intact(tmp_path, monkeypatch):
    config_dir, _ = _fixture(tmp_path, monkeypatch, directory_source=True)
    assert gsi.detect_hook_delivery_duplication(config_dir).double_fire is True


def test_unreadable_registry_defaults_to_empty(tmp_path, monkeypatch):
    config_dir, only = _fixture(tmp_path, monkeypatch)

    def boom(key):
        raise OSError("unreadable")

    monkeypatch.setattr(machine_resolver, "registry_get", boom)
    assert gsi.detect_hook_delivery_duplication(config_dir).settings_only_scripts == [
        str(only.resolve())
    ]

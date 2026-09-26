
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.session import guard_settings_integrity as _gsi
from coordinator_core.ops.session.guard_settings_integrity import (
    evaluate_guardless_sessions,
    evaluate_plugin_gating_drift,
    evaluate_settings_integrity,
)
from coordinator_core.ops.detect_guardless_sessions import (
    DetectionResult,
    ProcessObservation,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _hook_layer_always_reachable(monkeypatch):
    monkeypatch.setattr(_gsi, "_hook_layer_reachable", lambda settings_data: True)


def _write_settings(config_dir: Path, enabled_plugins: dict) -> None:
    (config_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": enabled_plugins}), encoding="utf-8"
    )


def _write_installed(config_dir: Path, plugins: dict) -> None:
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8"
    )


def test_declared_true_and_installed_is_silent(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"foo@bar": True})
    _write_installed(config_dir, {"foo@bar": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert text == ""


def test_declared_true_and_absent_bannered(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"example-game-repo@example-game-workbench-repo": True})
    _write_installed(config_dir, {"other@marketplace": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert "example-game-repo@example-game-workbench-repo" in text
    assert "claude plugin marketplace add <example-game-workbench-repo checkout dir>" in text
    assert "claude plugin install example-game-repo@example-game-workbench-repo" in text


def test_banner_resolves_declared_marketplace_dir_once(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings = {
        "enabledPlugins": {
            "example-game-repo@example-game-workbench-repo": True,
            "game-dev@example-game-workbench-repo": True,
        },
        "extraKnownMarketplaces": {
            "example-game-workbench-repo": {
                "source": {"source": "directory", "path": "/X/example-game-workbench-repo/plugin"}
            }
        },
    }
    (config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    _write_installed(config_dir, {"other@marketplace": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert text.count("claude plugin marketplace add") == 1
    assert "claude plugin marketplace add /X/example-game-workbench-repo/plugin" in text
    assert "claude plugin install example-game-repo@example-game-workbench-repo" in text
    assert "claude plugin install game-dev@example-game-workbench-repo" in text


def test_malformed_extra_known_marketplaces_still_banners(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings = {
        "enabledPlugins": {"example-game-repo@example-game-workbench-repo": True},
        "extraKnownMarketplaces": {"example-game-workbench-repo": "not-a-dict"},
    }
    (config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    _write_installed(config_dir, {"other@marketplace": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert "example-game-repo@example-game-workbench-repo" in text
    assert "<example-game-workbench-repo checkout dir>" in text


def test_declared_false_and_absent_is_silent(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"clangd-lsp@claude-plugins-official": False})
    _write_installed(config_dir, {})

    text = evaluate_settings_integrity(config_dir)
    assert text == ""


def test_missing_installed_plugins_json_is_silent(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"foo@bar": True})

    text = evaluate_settings_integrity(config_dir)
    assert text == ""


def test_malformed_installed_plugins_json_is_silent(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"foo@bar": True})
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text("{not valid json", encoding="utf-8")

    text = evaluate_settings_integrity(config_dir)
    assert text == ""


def test_malformed_installed_plugins_non_dict_plugins_key_is_silent(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"foo@bar": True})
    plugins_dir = config_dir / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": "not-a-dict"}), encoding="utf-8"
    )

    text = evaluate_settings_integrity(config_dir)
    assert text == ""


def test_inline_dev_source_install_is_silent_even_with_unreachable_key(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doe_clone = tmp_path / "doe-clone"
    (doe_clone / "coordinator").mkdir(parents=True)
    (config_dir / ".doe-root").write_text(str(doe_clone), encoding="utf-8")
    _write_settings(config_dir, {"coordinator-claude@local": True})

    text = evaluate_settings_integrity(config_dir)
    assert text == ""


def test_multiple_unreachable_keys_all_named(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "example-game-repo@example-game-workbench-repo": True,
            "example-game-repo-control@example-game-workbench-repo": True,
            "game-dev@example-game-workbench-repo": True,
            "context7@claude-plugins-official": True,
        },
    )
    _write_installed(config_dir, {"context7@claude-plugins-official": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert "example-game-repo@example-game-workbench-repo" in text
    assert "example-game-repo-control@example-game-workbench-repo" in text
    assert "game-dev@example-game-workbench-repo" in text
    assert "context7@claude-plugins-official" not in text


def test_plugin_gating_drift_fires_on_drifted_fixture(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "game-dev@example-game-workbench-repo": True,
            "example-game-repo@example-game-workbench-repo": False,
            "example-game-repo-control@example-game-workbench-repo": False,
        },
    )

    text = evaluate_plugin_gating_drift(config_dir)
    assert "game-dev@example-game-workbench-repo" in text
    assert "example-game-repo-control@example-game-workbench-repo" not in text
    assert "expected" in text
    assert "False" in text


def test_plugin_gating_drift_silent_on_shipped_default(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "game-dev@example-game-workbench-repo": False,
            "example-game-repo@example-game-workbench-repo": False,
            "example-game-repo-control@example-game-workbench-repo": False,
            "example-retrieval-repo@example-retrieval-repo": True,
        },
    )

    assert evaluate_plugin_gating_drift(config_dir) == ""


def test_plugin_gating_drift_silent_on_missing_settings_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    assert evaluate_plugin_gating_drift(config_dir) == ""


def test_plugin_gating_drift_silent_on_malformed_settings_json(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text("{not valid json", encoding="utf-8")

    assert evaluate_plugin_gating_drift(config_dir) == ""


def test_plugin_gating_drift_silent_on_missing_enabled_plugins_key(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({}), encoding="utf-8")

    assert evaluate_plugin_gating_drift(config_dir) == ""


def test_plugin_gating_drift_silent_on_non_dict_enabled_plugins(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": "not-a-dict"}), encoding="utf-8"
    )

    assert evaluate_plugin_gating_drift(config_dir) == ""


def test_plugin_gating_drift_silent_on_unreadable_contract(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"game-dev@example-game-workbench-repo": True})
    monkeypatch.setattr(
        _gsi, "_PLUGIN_GATING_CONTRACT_PATH", tmp_path / "does-not-exist.json"
    )

    assert evaluate_plugin_gating_drift(config_dir) == ""


def test_guardless_sessions_fires_and_names_pid(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.detect",
        lambda: DetectionResult(
            cannot_determine=False,
            reason=None,
            observed=[
                ProcessObservation(
                    pid=17152, command_line="claude.exe --dangerously-skip-permissions", guarded=False
                )
            ],
            guardless=[
                ProcessObservation(
                    pid=17152, command_line="claude.exe --dangerously-skip-permissions", guarded=False
                )
            ],
        ),
    )

    text = evaluate_guardless_sessions()
    assert "17152" in text
    assert "claude-doe" in text


def test_guardless_sessions_silent_when_all_guarded(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.detect",
        lambda: DetectionResult(
            cannot_determine=False,
            reason=None,
            observed=[
                ProcessObservation(
                    pid=43052,
                    command_line='claude.exe --plugin-dir X:/DoE-claude/coordinator',
                    guarded=True,
                )
            ],
            guardless=[],
        ),
    )

    assert evaluate_guardless_sessions() == ""


def test_guardless_sessions_silent_when_cannot_determine(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.detect",
        lambda: DetectionResult(
            cannot_determine=True,
            reason="process-table probe is Windows-only (platform.system() == 'Linux')",
        ),
    )

    assert evaluate_guardless_sessions() == ""


def test_guardless_sessions_silent_on_empty_observations(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.detect",
        lambda: DetectionResult(cannot_determine=False, reason=None, observed=[], guardless=[]),
    )

    assert evaluate_guardless_sessions() == ""


def test_evaluate_settings_integrity_composes_guardless_session_banner(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"foo@bar": True})
    _write_installed(config_dir, {"foo@bar": [{"scope": "user"}]})

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.detect",
        lambda: DetectionResult(
            cannot_determine=False,
            reason=None,
            observed=[
                ProcessObservation(
                    pid=17152, command_line="claude.exe --dangerously-skip-permissions", guarded=False
                )
            ],
            guardless=[
                ProcessObservation(
                    pid=17152, command_line="claude.exe --dangerously-skip-permissions", guarded=False
                )
            ],
        ),
    )

    text = evaluate_settings_integrity(config_dir)
    assert "17152" in text
    assert "claude-doe" in text


def test_evaluate_settings_integrity_own_config_banner_survives_guardless_composition(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(config_dir, {"example-game-repo@example-game-workbench-repo": True})
    _write_installed(config_dir, {"other@marketplace": [{"scope": "user"}]})

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.detect",
        lambda: DetectionResult(
            cannot_determine=False,
            reason=None,
            observed=[
                ProcessObservation(pid=17152, command_line="claude.exe", guarded=False)
            ],
            guardless=[
                ProcessObservation(pid=17152, command_line="claude.exe", guarded=False)
            ],
        ),
    )

    text = evaluate_settings_integrity(config_dir)
    assert "example-game-repo@example-game-workbench-repo" in text
    assert "17152" in text


def test_is_inline_install_true_on_flat_published_mirror(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    mirror = tmp_path / "coordinator-claude"
    (mirror / ".claude-plugin").mkdir(parents=True)
    (mirror / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    (config_dir / ".doe-root").write_text(str(mirror), encoding="utf-8")

    assert _gsi.is_inline_install(config_dir) is True


def test_is_inline_install_false_on_bare_directory(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    bare = tmp_path / "bare"
    bare.mkdir()
    (config_dir / ".doe-root").write_text(str(bare), encoding="utf-8")

    assert _gsi.is_inline_install(config_dir) is False


@pytest.mark.parametrize("migrated_body", ["", "/nonexistent/DoE-claude\n"])
def test_non_live_migrated_rung_does_not_shadow_live_legacy(tmp_path, monkeypatch, migrated_body):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_home = tmp_path / "settings_home"
    (settings_home / "machine-local").mkdir(parents=True)
    (settings_home / "machine-local" / ".doe-root").write_text(migrated_body, encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    doe = tmp_path / "DoE-claude"
    (doe / "coordinator").mkdir(parents=True)
    (config_dir / ".doe-root").write_text(str(doe) + "\n", encoding="utf-8")

    assert _gsi.is_inline_install(config_dir) is True

    (config_dir / ".doe-root").write_text("/nonexistent/legacy\n", encoding="utf-8")
    assert _gsi.is_inline_install(config_dir) is False


def test_flat_mirror_install_is_silent_even_with_unreachable_key(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    mirror = tmp_path / "coordinator-claude"
    (mirror / ".claude-plugin").mkdir(parents=True)
    (mirror / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    (config_dir / ".doe-root").write_text(str(mirror), encoding="utf-8")
    _write_settings(config_dir, {"coordinator-claude@local": True})

    assert evaluate_settings_integrity(config_dir) == ""

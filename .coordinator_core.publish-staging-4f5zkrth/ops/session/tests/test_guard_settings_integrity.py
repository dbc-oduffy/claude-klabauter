"""
coordinator_core.ops.session.tests.test_guard_settings_integrity

Tests for the session.guard_settings_integrity reconciliation lens — the
declared-true-but-unreachable-plugin detector added alongside the pre-existing
clobber lens (2026-07-28, holodeck/holodeck-control/game-dev incident: all
three read `enabledPlugins: true` for days while loading nothing; a separate
MCP `.mcp.json` connection kept working and camouflaged the gap).

Import guard: coordinator_core.ops.session.guard_settings_integrity MUST be
imported at module load time to fire the @register_op(...) side-effect
(pcore-11 registry-completeness pattern) — done implicitly here via the
direct `evaluate_settings_integrity` import, which is also the function under
test (mirrors the DoE stub's own direct-import call shape, not the async IPC
handler).

Coverage (reconciliation lens only — the pre-existing clobber-lens tests
live in the DoE-side pytest port, coordinator/tests/test_guard_settings_
integrity.py, which exercises this op indirectly through the hook script):
  - declared-true and installed (non-empty record) -> silent
  - declared-true and absent from installed_plugins.json -> banner naming
    the key, with the exact remediation lines
  - declared-false and absent -> silent (opt-outs are never flagged)
  - installed_plugins.json missing -> silent (fail-open: cannot verify)
  - installed_plugins.json malformed (invalid JSON / non-dict / no
    "plugins" key) -> silent (fail-open)
  - inline dev-source (--plugin-dir) install (.doe-root present, resolves)
    -> silent even with an unreachable declared-true key (whole-lens
    carve-out; no per-key way to isolate "this key IS the inline install")
  - multiple unreachable keys -> banner names every one of them

Negative-spec: does NOT re-test the clobber lens (restore-from-snapshot,
restore-from-git-HEAD, inline-install clobber carve-out) — those are
unchanged by this addition and already covered by the DoE-side hook tests.
"""

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

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _hook_layer_always_reachable(monkeypatch):
    """This module exercises the reconciliation lens (declared-true-vs-
    installed) added atop the pre-existing clobber lens — a concern
    orthogonal to hook-layer reachability (`_is_healthy`'s 2026-07-28
    conjunct; dedicated coverage lives in
    test_guard_settings_integrity_hook_layer.py). None of this module's
    fixtures set up a resolvable coordinator content root or a settings-side
    `hooks` block, and the autouse HOME-quarantine fixture elsewhere in this
    tree (`coordinator_core/conftest.py::_quarantine_real_home`) means the
    live resolver can't find one either — so without this override every
    fixture here would fail the NEW hook-layer conjunct and never reach the
    reconciliation lens this module actually tests. Force reachable so
    `_is_healthy` behaves exactly as it did pre-hook-layer for this module's
    purposes, matching "tests follow production" (the production predicate
    is unchanged; this narrows what THIS module is exercising)."""
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
    _write_settings(config_dir, {"holodeck@claude-unreal-holodeck": True})
    _write_installed(config_dir, {"other@marketplace": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert "holodeck@claude-unreal-holodeck" in text
    # No declared directory for this marketplace in settings, so the banner
    # names the marketplace in the placeholder rather than emitting a bare
    # `<dir>` the operator cannot act on.
    assert "claude plugin marketplace add <claude-unreal-holodeck checkout dir>" in text
    assert "claude plugin install holodeck@claude-unreal-holodeck" in text


def test_banner_resolves_declared_marketplace_dir_once(tmp_path):
    """Two unreachable plugins from ONE marketplace with a declared path ->
    the real path appears, and `marketplace add` is emitted once, not per
    plugin. Repeated boilerplate is what trains operators to skim banners."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings = {
        "enabledPlugins": {
            "holodeck@claude-unreal-holodeck": True,
            "game-dev@claude-unreal-holodeck": True,
        },
        "extraKnownMarketplaces": {
            "claude-unreal-holodeck": {
                "source": {"source": "directory", "path": "/X/claude-unreal-holodeck/plugin"}
            }
        },
    }
    (config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    _write_installed(config_dir, {"other@marketplace": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert text.count("claude plugin marketplace add") == 1
    assert "claude plugin marketplace add /X/claude-unreal-holodeck/plugin" in text
    assert "claude plugin install holodeck@claude-unreal-holodeck" in text
    assert "claude plugin install game-dev@claude-unreal-holodeck" in text


def test_malformed_extra_known_marketplaces_still_banners(tmp_path):
    """A junk `extraKnownMarketplaces` shape must not break the banner — path
    resolution is best-effort decoration on top of a fail-open guard."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings = {
        "enabledPlugins": {"holodeck@claude-unreal-holodeck": True},
        "extraKnownMarketplaces": {"claude-unreal-holodeck": "not-a-dict"},
    }
    (config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    _write_installed(config_dir, {"other@marketplace": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert "holodeck@claude-unreal-holodeck" in text
    assert "<claude-unreal-holodeck checkout dir>" in text


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
    # No plugins/installed_plugins.json written at all.

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
    # No installed_plugins.json at all -- would otherwise be unreachable.

    text = evaluate_settings_integrity(config_dir)
    assert text == ""


def test_multiple_unreachable_keys_all_named(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "holodeck@claude-unreal-holodeck": True,
            "holodeck-control@claude-unreal-holodeck": True,
            "game-dev@claude-unreal-holodeck": True,
            "context7@claude-plugins-official": True,
        },
    )
    _write_installed(config_dir, {"context7@claude-plugins-official": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert "holodeck@claude-unreal-holodeck" in text
    assert "holodeck-control@claude-unreal-holodeck" in text
    assert "game-dev@claude-unreal-holodeck" in text
    assert "context7@claude-plugins-official" not in text


# ---------------------------------------------------------------------------
# evaluate_plugin_gating_drift -- 2026-08-14 config-value drift detector
# (state/subagent-share/ed69af78-ccc1-4efc-8b58-4cc93cb3b461/
# coordinatorstaff-eng-3c57c48d.md). Uses the real
# `plugin_gating_contract.json` shipped beside the module (all-`False`
# today) rather than monkeypatching it -- these tests pin behavior against
# the live contract, matching the fixture-not-real-settings.json discipline
# for the OTHER file this module reads.
# ---------------------------------------------------------------------------


def test_plugin_gating_drift_fires_on_drifted_fixture(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "game-dev@claude-unreal-holodeck": True,
            "holodeck@claude-unreal-holodeck": False,
            "holodeck-control@claude-unreal-holodeck": False,
        },
    )

    text = evaluate_plugin_gating_drift(config_dir)
    assert "game-dev@claude-unreal-holodeck" in text
    assert "holodeck-control@claude-unreal-holodeck" not in text
    assert "expected" in text
    assert "False" in text


def test_plugin_gating_drift_silent_on_shipped_default(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_settings(
        config_dir,
        {
            "game-dev@claude-unreal-holodeck": False,
            "holodeck@claude-unreal-holodeck": False,
            "holodeck-control@claude-unreal-holodeck": False,
            "project-rag@project-rag": True,
        },
    )

    assert evaluate_plugin_gating_drift(config_dir) == ""


def test_plugin_gating_drift_silent_on_missing_settings_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # No settings.json written at all.

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
    _write_settings(config_dir, {"game-dev@claude-unreal-holodeck": True})
    monkeypatch.setattr(
        _gsi, "_PLUGIN_GATING_CONTRACT_PATH", tmp_path / "does-not-exist.json"
    )

    assert evaluate_plugin_gating_drift(config_dir) == ""


# ---------------------------------------------------------------------------
# evaluate_guardless_sessions -- 2026-08-14 unguarded-peer-session detector
# (state/bug-backlog/2026-08-14-a-live-session-is-running-plugin-dir-les-
# 2ee0e6522f92.yaml). Injects the underlying
# `coordinator_core.ops.detect_guardless_sessions.detect` verdict directly --
# never enumerates real processes in a test (brief requirement).
# ---------------------------------------------------------------------------


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
                    command_line='claude.exe --plugin-dir X:/DoE-claude/coordinator',  # abs-path-ok: fixture-only placeholder command line, not a real host path
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

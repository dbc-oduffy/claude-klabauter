"""
coordinator_core.ops.session.tests.test_guard_settings_integrity

Tests for the session.guard_settings_integrity reconciliation lens — the
declared-true-but-unreachable-plugin detector added alongside the pre-existing
clobber lens (2026-07-28, example-game-repo/example-game-repo-control/game-dev incident: all
three read `enabledPlugins: true` for days while loading nothing; a separate
MCP `.mcp.json` connection kept working and camouflaged the gap).

Import guard: coordinator_core.ops.session.guard_settings_integrity MUST be
imported at module load time to fire the @register_op(...) side-effect
(pcore-11 registry-completeness pattern) — done implicitly here via the
direct `evaluate_settings_integrity` import, which is also the function under
test (mirrors the coordinator-claude stub's own direct-import call shape, not the async IPC
handler).

Coverage (reconciliation lens only — the pre-existing clobber-lens tests
live in the coordinator-claude-side pytest port, coordinator/tests/test_guard_settings_
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
unchanged by this addition and already covered by the coordinator-claude-side hook tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.session import guard_settings_integrity as _gsi
from coordinator_core.ops.session.guard_settings_integrity import (
    evaluate_settings_integrity,
)


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
    _write_settings(config_dir, {"example-game-repo@example-game-workbench-repo": True})
    _write_installed(config_dir, {"other@marketplace": [{"scope": "user"}]})

    text = evaluate_settings_integrity(config_dir)
    assert "example-game-repo@example-game-workbench-repo" in text
    # No declared directory for this marketplace in settings, so the banner
    # names the marketplace in the placeholder rather than emitting a bare
    # `<dir>` the operator cannot act on.
    assert "claude plugin marketplace add <example-game-workbench-repo checkout dir>" in text
    assert "claude plugin install example-game-repo@example-game-workbench-repo" in text


def test_banner_resolves_declared_marketplace_dir_once(tmp_path):
    """Two unreachable plugins from ONE marketplace with a declared path ->
    the real path appears, and `marketplace add` is emitted once, not per
    plugin. Repeated boilerplate is what trains operators to skim banners."""
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
    """A junk `extraKnownMarketplaces` shape must not break the banner — path
    resolution is best-effort decoration on top of a fail-open guard."""
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

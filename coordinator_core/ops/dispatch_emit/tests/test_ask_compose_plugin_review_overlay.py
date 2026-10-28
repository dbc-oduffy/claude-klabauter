"""Compose's review vocabulary includes installed plugins' overlay signals, base signals unchanged."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import plugin_review_overlay as overlay
from coordinator_core.ops.dispatch_emit.ask_compose import load_review_signal_selects

_BASE = {"signals": {"docs": {"selects": "coordinator:staff-eng"}}}


def _base_root(tmp_path: Path) -> str:
    root = tmp_path / "coord"
    (root / "contract").mkdir(parents=True)
    (root / "contract" / "review-signals.json").write_text(json.dumps(_BASE), encoding="utf-8")
    return str(root)


def _plugin(tmp_path: Path, name: str, signals: list, schema: str = "plugin-review-signals") -> Path:
    root = tmp_path / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (root / "review-signals.json").write_text(json.dumps({"schema": schema, "signals": signals}), encoding="utf-8")
    return root


def _install(monkeypatch, tmp_path: Path, *roots: Path) -> None:
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    plugins = {f"{r.name}@m": [{"installPath": str(r)}] for r in roots}
    (home / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": plugins}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv(overlay.PLUGIN_ROOTS_ENV, raising=False)


_UNREAL = {"id": "unreal", "selects": "game-dev:staff-game-dev", "prose_terms": ["x"], "write_triggers": []}


def test_overlay_signal_listed_and_mapped_base_unchanged(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _plugin(tmp_path, "game-dev", [_UNREAL]))
    assert load_review_signal_selects(_base_root(tmp_path)) == {
        "docs": "coordinator:staff-eng",
        "unreal": "game-dev:staff-game-dev",
    }


def test_no_overlay_installed_returns_base_only(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    assert load_review_signal_selects(_base_root(tmp_path)) == {"docs": "coordinator:staff-eng"}


def test_overlay_cannot_shadow_base_or_name_foreign_agent(monkeypatch, tmp_path):
    bad = [
        {"id": "docs", "selects": "game-dev:evil"},
        {"id": "other", "selects": "coordinator:staff-eng"},
        {"id": "Not_Kebab", "selects": "game-dev:a"},
    ]
    _install(monkeypatch, tmp_path, _plugin(tmp_path, "game-dev", bad))
    assert load_review_signal_selects(_base_root(tmp_path)) == {"docs": "coordinator:staff-eng"}


def test_wrong_schema_overlay_skipped(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _plugin(tmp_path, "game-dev", [_UNREAL], schema="nope"))
    assert load_review_signal_selects(_base_root(tmp_path)) == {"docs": "coordinator:staff-eng"}


def test_overlay_read_once_per_compose(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _plugin(tmp_path, "game-dev", [_UNREAL]))
    calls = []
    real = overlay.installed_plugin_roots
    monkeypatch.setattr(overlay, "installed_plugin_roots", lambda *a, **k: calls.append(1) or real(*a, **k))
    load_review_signal_selects(_base_root(tmp_path))
    assert len(calls) == 1

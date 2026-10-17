"""Tests for the per-session plugin-root binding the breadcrumb op also writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import session_start_write_plugin_root_breadcrumb as mod
from coordinator_core.install import plugin_cache_sessions as pcs


@pytest.fixture
def env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    home.mkdir()
    cfg.mkdir()
    root = tmp_path / "plugin-root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    return cfg, root, home


def _binding(cfg: Path, sid: str) -> Path:
    return pcs.bindings_dir(cfg) / sid


def test_binding_written_for_session_id(env):
    cfg, root, _ = env
    mod._handler({"session_id": "sess-1"})
    assert _binding(cfg, "sess-1").read_text(encoding="utf-8").strip() == root.as_posix()


def test_binding_written_for_dispatcher_nested_payload(env):
    cfg, root, _ = env
    mod._handler({"payload": {"session_id": "sess-n"}})
    assert _binding(cfg, "sess-n").read_text(encoding="utf-8").strip() == root.as_posix()


def test_no_binding_without_session_id(env):
    cfg, _, _ = env
    mod._handler({})
    assert not pcs.bindings_dir(cfg).exists()


def test_no_binding_without_plugin_root(env, monkeypatch):
    cfg, _, _ = env
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT")
    mod._handler({"session_id": "sess-2"})
    assert not _binding(cfg, "sess-2").exists()


def test_unchanged_breadcrumb_still_writes_binding(env):
    cfg, root, home = env
    crumb = home / mod.BREADCRUMB_RELATIVE_PATH
    crumb.parent.mkdir(parents=True)
    crumb.write_text(root.as_posix() + "\n", encoding="utf-8")
    mod._handler({"session_id": "sess-3"})
    assert _binding(cfg, "sess-3").read_text(encoding="utf-8").strip() == root.as_posix()


def test_write_binding_exception_is_swallowed(env, monkeypatch):
    _, root, home = env

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(pcs, "write_binding", boom)
    result = mod._handler({"session_id": "sess-4"})
    assert result == mod.no_advisory()
    assert (home / mod.BREADCRUMB_RELATIVE_PATH).read_text(encoding="utf-8").strip() == root.as_posix()

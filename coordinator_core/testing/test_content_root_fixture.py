"""Tests for coordinator_core.testing.content_root."""

from __future__ import annotations

from coordinator_core.testing import content_root as fx


def test_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_KLABAUTER_TEST_CONTENT_ROOT", str(tmp_path))
    assert fx.resolve_content_root() == str(tmp_path)
    assert fx.content_root_and_present() == (str(tmp_path), True)


def test_override_missing_dir_is_not_present(tmp_path, monkeypatch):
    missing = str(tmp_path / "nope")
    monkeypatch.setenv("CLAUDE_KLABAUTER_TEST_CONTENT_ROOT", missing)
    assert fx.content_root_and_present() == (missing, False)


def test_falls_back_to_read_content_root(monkeypatch):
    monkeypatch.delenv("CLAUDE_KLABAUTER_TEST_CONTENT_ROOT", raising=False)
    monkeypatch.setattr(fx, "read_content_root", lambda: "")
    assert fx.resolve_content_root() == ""
    assert fx.content_root_and_present() == ("", False)
    monkeypatch.setattr(fx, "read_content_root", lambda: "/x/y")
    assert fx.resolve_content_root() == "/x/y"

"""Tests for coordinator_core.ops.content_root_resolve."""

from __future__ import annotations

from coordinator_core.ops import content_root_resolve as crr


def _patch_rungs(monkeypatch, *fns):
    names = ("content-ladder", "clone-root", "engine-checkout")
    monkeypatch.setattr(crr, "_RUNGS", tuple(zip(names, fns)))


def test_first_existing_rung_wins(monkeypatch, tmp_path):
    _patch_rungs(monkeypatch, lambda: str(tmp_path), lambda: "unused", lambda: "unused")
    assert crr.resolve_plugin_content_root() == (str(tmp_path), "content-ladder")


def test_falls_through_none_nonexistent_and_raising_rungs(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("x")

    _patch_rungs(monkeypatch, boom, lambda: str(tmp_path / "missing"), lambda: str(tmp_path))
    assert crr.resolve_plugin_content_root() == (str(tmp_path), "engine-checkout")


def test_all_rungs_fail_is_unresolved(monkeypatch):
    _patch_rungs(monkeypatch, lambda: None, lambda: "", lambda: None)
    assert crr.resolve_plugin_content_root() == (None, "unresolved")


def test_env_root_resolves_in_process(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert crr.resolve_plugin_content_root() == (str(tmp_path), "content-ladder")


def test_main_prints_newline_terminated_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert crr.main([]) == 0
    assert capsys.readouterr().out == str(tmp_path) + "\n"


def test_registered_warm_op_returns_path_and_rung(monkeypatch, tmp_path):
    import asyncio

    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP["install.content_root_resolve"] == "coordinator_core.ops.content_root_resolve"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    out = asyncio.run(crr._content_root_resolve({}, None))
    assert out == {"path": str(tmp_path), "rung": "content-ladder"}


def test_registered_warm_op_reports_none_when_unresolved(monkeypatch):
    import asyncio

    _patch_rungs(monkeypatch, lambda: None, lambda: None, lambda: None)
    assert asyncio.run(crr._content_root_resolve({}, None)) == {"path": None, "rung": "unresolved"}


def test_main_refuses_when_unresolved(monkeypatch, capsys):
    _patch_rungs(monkeypatch, lambda: None, lambda: None, lambda: None)
    assert crr.main([]) == 1
    assert capsys.readouterr().out == ""

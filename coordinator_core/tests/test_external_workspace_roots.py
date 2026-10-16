"""Tests for machine_resolver.external_workspace_roots / external_workspace_containing / external_workspace_registry_readable."""

from __future__ import annotations

import os

import pytest

from coordinator_core import machine_resolver as mr


@pytest.fixture
def reg(tmp_path, monkeypatch):
    d = tmp_path / "ml"
    d.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    mr._external_ws_cache.clear()
    yield d
    mr._external_ws_cache.clear()


def _write(reg, text):
    (reg / "registry.local.toml").write_text(text, encoding="utf-8")
    mr._external_ws_cache.clear()


def _ws(tmp_path, name="slice"):
    p = tmp_path / name
    p.mkdir()
    return p


def test_existing_dir_entry_returned(reg, tmp_path):
    ws = _ws(tmp_path)
    _write(reg, f'[external_workspaces.p4_x]\npath = "{ws.as_posix()}"\nkind = "external-readonly"\nvcs = "p4"\np4_client = "c1"\n')
    roots = mr.external_workspace_roots()
    assert set(roots) == {"p4_x"}
    assert roots["p4_x"] == {
        "path": os.path.normcase(os.path.realpath(ws)),
        "kind": "external-readonly",
        "vcs": "p4",
        "p4_client": "c1",
    }
    assert mr.external_workspace_registry_readable() is True


def test_missing_dir_and_empty_path_not_returned(reg, tmp_path):
    _write(
        reg,
        f'[external_workspaces.gone]\npath = "{(tmp_path / "nope").as_posix()}"\n'
        '[external_workspaces.empty]\npath = ""\n'
        "[external_workspaces.nopath]\nvcs = \"p4\"\n",
    )
    assert mr.external_workspace_roots() == {}


def test_absent_and_unknown_kind_read_as_external_readonly(reg, tmp_path):
    a, b = _ws(tmp_path, "a"), _ws(tmp_path, "b")
    _write(
        reg,
        f'[external_workspaces.a]\npath = "{a.as_posix()}"\n'
        f'[external_workspaces.b]\npath = "{b.as_posix()}"\nkind = "writable-maybe"\n',
    )
    roots = mr.external_workspace_roots()
    assert roots["a"]["kind"] == "external-readonly"
    assert roots["a"]["vcs"] == "" and roots["a"]["p4_client"] == ""
    assert roots["b"]["kind"] == "external-readonly"


def test_containment_root_child_and_shared_prefix_sibling(reg, tmp_path):
    ws = _ws(tmp_path, "slice")
    sib = _ws(tmp_path, "slice2")
    (ws / "sub").mkdir()
    _write(reg, f'[external_workspaces.k]\npath = "{ws.as_posix()}"\n')
    assert mr.external_workspace_containing(str(ws)) == "k"
    assert mr.external_workspace_containing(str(ws / "sub" / "not-yet.txt")) == "k"
    assert mr.external_workspace_containing(str(sib)) is None
    assert mr.external_workspace_containing("") is None


def test_case_variant_matches_under_case_insensitive_normcase(reg, tmp_path, monkeypatch):
    ws = _ws(tmp_path, "Slice")
    monkeypatch.setattr(os.path, "normcase", lambda s: s.lower())
    _write(reg, f'[external_workspaces.k]\npath = "{ws.as_posix()}"\n')
    variant = str(tmp_path) + os.sep + "SLICE" + os.sep + "child"
    assert mr.external_workspace_containing(variant) == "k"


def test_windows_drive_letter_path_never_raises_and_is_not_contained(reg, tmp_path):
    ws = _ws(tmp_path)
    _write(reg, f'[external_workspaces.k]\npath = "{ws.as_posix()}"\n')
    assert mr.external_workspace_containing("C:\\somedrive\\slice") is None  # abs-path-ok: synthetic drive-letter fixture
    _write(reg, '[external_workspaces.w]\npath = "C:\\\\nonexistent-drive\\\\slice"\n')
    assert mr.external_workspace_roots() == {}


def test_unreadable_registry_yields_empty_and_not_readable(reg):
    (reg / "registry.local.toml").write_text("this is [not toml", encoding="utf-8")
    mr._external_ws_cache.clear()
    assert mr.external_workspace_roots() == {}
    assert mr.external_workspace_registry_readable() is False
    assert mr.external_workspace_containing("/anywhere") is None


def test_no_registry_is_readable_and_empty(reg):
    assert mr.external_workspace_roots() == {}
    assert mr.external_workspace_registry_readable() is True


def test_registry_change_invalidates_cache(reg, tmp_path):
    ws = _ws(tmp_path)
    _write(reg, f'[external_workspaces.k]\npath = "{ws.as_posix()}"\n')
    assert "k" in mr.external_workspace_roots()
    p = reg / "registry.local.toml"
    p.write_text("", encoding="utf-8")
    os.utime(p, ns=(1, 1))
    assert mr.external_workspace_roots() == {}

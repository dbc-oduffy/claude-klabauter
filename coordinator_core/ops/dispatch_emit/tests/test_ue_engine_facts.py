"""ue_engine_facts_clause: served / unserved decisions, hermetic against a fake addon."""

from __future__ import annotations

import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ue_engine_facts as uef


def _fake_enabled(root: Path) -> bool:
    return any(p.suffix.lower() == ".uproject" for p in Path(root).iterdir() if p.is_file())


@pytest.fixture
def addon(monkeypatch):
    """Install a fake addon: find_spec sees it, detector is root-only *.uproject."""
    pkg = types.ModuleType("project_rag_ue_addon")
    mod = types.ModuleType("example_retrieval_repo_ue_addon.tooling_opt_in")
    mod.ue_tooling_enabled = _fake_enabled
    monkeypatch.setitem(sys.modules, "project_rag_ue_addon", pkg)
    monkeypatch.setitem(sys.modules, "example_retrieval_repo_ue_addon.tooling_opt_in", mod)
    monkeypatch.setattr(uef.importlib.util, "find_spec", lambda name: object())
    return mod


def _repo(tmp_path: Path, *, index: bool = True, uproject: str | None = None) -> Path:
    if index:
        (tmp_path / ".project-rag").mkdir()
        (tmp_path / ".project-rag" / "state.json").write_text("{}", encoding="utf-8")
    if uproject:
        target = tmp_path / uproject
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
    return tmp_path


def test_clause_text_pins_owner_line_and_enumeration_routing():
    assert uef.CLAUSE_STALE_LINE.startswith("UE project: your training data on UE5 APIs is stale.")
    assert "validate with validate_ue_api." in uef.CLAUSE_STALE_LINE
    enum = uef.CLAUSE_ENUMERATION_LINE
    assert "project_engine_list_modules" in enum
    assert "not semantic search" in enum
    assert "project_semantic_search" not in enum
    assert "example_retrieval_repo_instructions" in enum
    assert uef.CLAUSE == uef.CLAUSE_STALE_LINE + "\n" + enum


def test_served_with_root_uproject(tmp_path, addon):
    assert uef.ue_engine_facts_clause(str(_repo(tmp_path, uproject="Foo.uproject"))) == uef.CLAUSE


def test_empty_without_example_retrieval_repo_index(tmp_path, addon):
    repo = _repo(tmp_path, index=False, uproject="Foo.uproject")
    assert uef.ue_engine_facts_clause(str(repo)) == ""


def test_empty_when_addon_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(uef.importlib.util, "find_spec", lambda name: None)
    repo = _repo(tmp_path, uproject="Foo.uproject")
    assert uef.ue_engine_facts_clause(str(repo)) == ""


def test_empty_when_detector_raises(tmp_path, addon, monkeypatch):
    def boom(root):
        raise RuntimeError("addon broke")

    monkeypatch.setattr(addon, "ue_tooling_enabled", boom)
    assert uef.ue_engine_facts_clause(str(_repo(tmp_path, uproject="Foo.uproject"))) == ""


def test_subdirectory_uproject_is_not_served(tmp_path, addon):
    repo = _repo(tmp_path, uproject="Samples/Bar/Bar.uproject")
    assert uef.ue_engine_facts_clause(str(repo)) == ""


def test_no_subprocess_or_socket(tmp_path, addon, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("spawn/socket forbidden")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    repo = _repo(tmp_path, uproject="Foo.uproject")
    assert uef.ue_engine_facts_clause(str(repo)) == uef.CLAUSE
    assert uef.ue_engine_facts_clause(str(tmp_path / "nowhere")) == ""

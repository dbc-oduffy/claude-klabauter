"""The doctor's claude-klabauter.root.resolve probe refuses an unstamped (claude-klabauter) engine root."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROBE = Path(__file__).resolve().parent.parent / "claude-klabauter-doctor-probe.py"


@pytest.fixture()
def mod(monkeypatch):
    spec = importlib.util.spec_from_file_location("_doctor_probe_refuse_claude_klabauter", _PROBE)
    m = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, m)
    spec.loader.exec_module(m)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    return m


def _tree(tmp_path: Path, stamped: bool) -> Path:
    core = tmp_path / "coordinator_core"
    core.mkdir()
    if stamped:
        (core / "_engine_stamp").write_text("build-1\n", encoding="utf-8")
    return tmp_path


def test_env_claude_klabauter_tree_is_refused(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(_tree(tmp_path, stamped=False)))
    result, root = mod._run_probe_claude_klabauter_root()
    assert root is None
    assert result.status == mod._BROKEN
    assert "claude-klabauter is never an engine" in result.detail
    assert "claude-klabauter" in result.remediation


def test_registry_or_git_root_claude_klabauter_tree_is_refused(mod, tmp_path, monkeypatch):
    tree = _tree(tmp_path, stamped=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setattr(
        mod, "_resolve_claude_klabauter_root", lambda: (tree, "machine-local repos.claude_klabauter")
    )
    result, root = mod._run_probe_claude_klabauter_root()
    assert root is None
    assert result.status == mod._BROKEN
    assert "claude-klabauter" in result.remediation


def test_stamped_engine_root_still_passes(mod, tmp_path, monkeypatch):
    tree = _tree(tmp_path, stamped=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(tree))
    result, root = mod._run_probe_claude_klabauter_root()
    assert result.status == mod._PASS
    assert root == tree

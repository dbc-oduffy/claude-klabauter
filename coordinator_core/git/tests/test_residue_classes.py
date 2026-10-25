"""Tests for coordinator_core.git.residue_classes."""

import ast
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import residue_classes as rc
from coordinator_core.session import record_homes


@pytest.mark.parametrize("path", [
    "docs/plans/x.seam.yaml", "docs/plans/x.wave-2.seam.yaml", "state/sizings/a.yaml",
    "state/mise-inventory/p-t1.yaml", "state/mise-workflows/a.seam/b.json",
    "archive/releases/v0.2.0.yaml", "state/lessons/a.md", "state/apm-rulings/a.yaml",
    "state\\sizings\\a.yaml",
])
def test_residue_true(path):
    assert rc.is_engine_residue(path)


@pytest.mark.parametrize("path", [
    "docs/plans/x.md", "state/handoffs/a.md", "src/x.py", "docs/plans/sub/x.seam.yaml",
])
def test_residue_false(path):
    assert not rc.is_engine_residue(path)


def test_build_churn():
    assert rc.is_build_churn("src/lib/build/a.generated.json")
    assert rc.is_build_churn("src\\lib\\build\\a.generated.json")
    assert not rc.is_build_churn("src/lib/build/a.json")
    assert not rc.is_build_churn("generated/readme.md")


def _dirty(root, *rels):
    for r in rels:
        p = root / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")


def test_gap_reports_none_without_spawn(tmp_path, monkeypatch):
    import coordinator_core.git.scoped_worktree_status as sws

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(sws, "scoped_worktree_status", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert rc.dirty_engine_residue(tmp_path, []) == {"engine_residue": None}


def test_module_top_imports_only_record_homes():
    src = Path(rc.__file__).read_text(encoding="utf-8")
    names = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module)
            names += [f"{node.module}.{a.name}" for a in node.names]
    core = [n for n in names if n and n.startswith("coordinator_core")]
    assert "coordinator_core.session.record_homes" in core
    assert not any("scoped_worktree_status" in n or "git" in n for n in core)


def test_roots_come_from_record_homes_not_literals():
    for k in ("sizings", "mise-inventory", "mise-workflows", "lessons"):
        assert Path(record_homes.home_dir("", k)).as_posix() in rc.ENGINE_RESIDUE_ROOTS
    src = Path(rc.__file__).read_text(encoding="utf-8")
    for lit in ("state/sizings", "state/lessons", "state/mise-inventory", "state/mise-workflows"):
        assert f'"{lit}' not in src and f"'{lit}" not in src


def test_process_time_on_own_tree():
    root = Path(rc.__file__).resolve().parents[2]
    rc.dirty_engine_residue(root, [])
    t0 = time.process_time()
    out = rc.dirty_engine_residue(root, [])
    dt = time.process_time() - t0
    print(f"dirty_engine_residue process_time={dt * 1000:.1f}ms")
    assert out is not None
    assert dt < 0.25

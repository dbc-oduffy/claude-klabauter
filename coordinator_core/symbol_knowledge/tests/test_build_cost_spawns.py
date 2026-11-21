"""Tests for symbol_knowledge.build_cost, the contract types it returns, and the code_intel op shells."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.symbol_knowledge import build_cost

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("def alpha():\n    '''doc'''\n    return 1\n")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
    (tmp_path / "notes.md").write_text("not python\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_measure_counts_spawns_and_returns_result() -> None:
    result, cost = build_cost.measure(
        "t.op",
        lambda: subprocess.run(["git", "--version"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode,
        files_reextracted=3,
    )
    assert result == 0
    assert (cost.op, cost.spawns, cost.files_reextracted) == ("t.op", 1, 3)
    assert cost.process_ms >= 0


def test_full_tree_reextract_counts_only_python_files_and_one_git_spawn(repo: Path) -> None:
    cost, absent = build_cost.full_tree_reextract(repo, "HEAD")
    assert cost.op == build_cost.FULL_TREE_OP
    assert cost.files_reextracted == 2
    assert cost.spawns == 1
    assert absent == 0


def test_full_tree_reextract_reports_files_absent_from_worktree(repo: Path) -> None:
    (repo / "b.py").unlink()
    cost, absent = build_cost.full_tree_reextract(repo, "HEAD")
    assert (cost.files_reextracted, absent) == (1, 1)

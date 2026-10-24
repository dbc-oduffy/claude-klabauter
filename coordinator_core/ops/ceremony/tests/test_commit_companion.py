"""`commit_companion.own_left_behind` against a real `git init` fixture."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.commit_companion import own_left_behind
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for rel in ("a.py", "tracked_b.py", "sub/c.py"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "test_a.py").write_text("t\n", encoding="utf-8")
    (root / "sub" / "new.py").write_text("n\n", encoding="utf-8")
    return root


def test_own_untracked_is_carried(repo):
    assert own_left_behind(repo, {"me": ["a.py", "test_a.py"]}, "me", ["a.py"]) == ["test_a.py"]


def test_own_tracked_modified_is_carried(repo):
    (repo / "tracked_b.py").write_text("changed longer\n", encoding="utf-8")
    assert own_left_behind(repo, {"me": ["tracked_b.py"]}, "me", []) == ["tracked_b.py"]


def test_own_clean_is_omitted(repo):
    assert own_left_behind(repo, {"me": ["a.py", "sub/c.py"]}, "me", []) == []


def test_own_touched_but_same_content_is_omitted(repo):
    p = repo / "a.py"
    p.write_text("x\n", encoding="utf-8")
    os.utime(p, ns=(1_000_000_000, 1_000_000_000))
    assert own_left_behind(repo, {"me": ["a.py"]}, "me", []) == []


def test_own_in_pathspec_is_omitted(repo):
    assert own_left_behind(repo, {"me": ["test_a.py"]}, "me", ["test_a.py"]) == []


def test_own_contested_is_omitted(repo):
    held = {"me": ["test_a.py", "sub/new.py"]}
    assert own_left_behind(repo, held, "me", [], contested=["sub\\new.py"]) == ["test_a.py"]


def test_own_ignores_peers_and_missing_files(repo):
    held = {"me": ["ghost.py"], "peer": ["test_a.py"]}
    assert own_left_behind(repo, held, "me", []) == []


def test_own_left_behind_makes_no_subprocess_call(repo, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess used")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert own_left_behind(repo, {"me": ["test_a.py"]}, "me", []) == ["test_a.py"]

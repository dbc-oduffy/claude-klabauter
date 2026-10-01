"""`commit_companion.untracked_companions` against a real `git init` fixture."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.commit_companion import untracked_companions
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


def test_untracked_present_is_named(repo):
    held = {"p": ["a.py", "test_a.py"]}
    assert untracked_companions(repo, held, ["p"], ["a.py"]) == {"p": ["test_a.py"]}


def test_tracked_is_not_named(repo):
    held = {"p": ["a.py", "tracked_b.py"]}
    assert untracked_companions(repo, held, ["p"], ["a.py"]) == {}


def test_path_in_pathspec_is_not_named(repo):
    held = {"p": ["test_a.py"]}
    assert untracked_companions(repo, held, ["p"], ["test_a.py"]) == {}


def test_backslashed_pathspec_still_excludes(repo):
    held = {"p": ["sub/new.py"]}
    assert untracked_companions(repo, held, ["p"], ["sub\\new.py"]) == {}


def test_absent_on_disk_is_not_named(repo):
    held = {"p": ["ghost.py"]}
    assert untracked_companions(repo, held, ["p"], []) == {}


def test_two_peers_keyed_separately(repo):
    held = {"p": ["test_a.py"], "q": ["sub/new.py", "a.py"], "z": ["test_a.py"]}
    assert untracked_companions(repo, held, ["p", "q"], []) == {
        "p": ["test_a.py"],
        "q": ["sub/new.py"],
    }


def test_unknown_peer_and_empty_candidates(repo):
    assert untracked_companions(repo, {}, ["nobody"], []) == {}


def test_helper_makes_no_subprocess_call(repo, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess used")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    held = {"p": ["test_a.py"]}
    assert untracked_companions(repo, held, ["p"], []) == {"p": ["test_a.py"]}

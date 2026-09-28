"""Tests for `review_brightline_gate`'s `--worktree-base` mode (D7/C9).

Pins AC8: over a fixture with one modified tracked file and one new
untracked declared file, the mode counts both files' LOC, prints
`basis=code-only,worktree`, and never applies the commits threshold.
Also pins the plan body's own constraint: `--worktree-base` requires
`--paths`, and only the declared `paths` are counted — a peer commit to an
undeclared path after base must not be swept in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.review_brightline_gate import main
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")


def test_worktree_base_counts_tracked_and_untracked_declared_paths(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD").strip()

    # Modify the already-tracked file (worktree-only, not committed).
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    # New, untracked declared file.
    (repo / "b.py").write_text("z = 3\nw = 4\n", encoding="utf-8")

    monkeypatch.chdir(repo)

    rc = main(["--worktree-base", base, "--paths", "a.py", "b.py"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "basis=code-only,worktree" in captured.out
    assert "commits=n/a" in captured.out
    assert f"range={base}..worktree" in captured.out
    # a.py: +1 line; b.py: +2 lines (all-added, untracked) = loc=3
    assert "loc=3" in captured.out
    assert "surfaces=1" in captured.out
    assert "files=2" in captured.out
    assert "VERDICT=single-reviewer-ok" in captured.out


def test_worktree_base_requires_paths(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(repo)

    rc = main(["--worktree-base", base])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert "--paths" in captured.err


def test_worktree_base_ignores_peer_commit_to_undeclared_path(
    tmp_path, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD").strip()

    # Declared path: modified in the worktree.
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    # Peer commits to an UNDECLARED path after base — must not be counted.
    (repo / "peer.py").write_text("p = 1\np = 2\np = 3\n", encoding="utf-8")
    _git(repo, "add", "peer.py")
    _git(repo, "commit", "-q", "-m", "peer work")

    monkeypatch.chdir(repo)

    rc = main(["--worktree-base", base, "--paths", "a.py"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "loc=1" in captured.out
    assert "files=1" in captured.out
    assert "peer.py" not in captured.out

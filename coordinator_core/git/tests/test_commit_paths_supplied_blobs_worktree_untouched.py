"""`commit_paths(supplied_blobs=...)` commits the supplied blob and leaves the worktree bytes
identical. Throwaway repo under `tmp_path`, ceilinged so no git call can reach an outer repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import commit_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        input=stdin, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def test_supplied_blob_commits_while_worktree_stays_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    (repo / "f.py").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "--", "f.py")
    _git(repo, "commit", "-q", "-m", "seed")
    mine = _git(repo, "hash-object", "-w", "--stdin", stdin="base\nmine\n").strip()
    worktree = b"base\nmine\npeer\n"
    (repo / "f.py").write_bytes(worktree)

    commit_paths(
        repo, ["f.py"], "m\n\nSession-Id: 11111111-1111-4111-8111-111111111111",
        supplied_blobs={"f.py": mine},
    )

    assert _git(repo, "rev-parse", "HEAD:f.py").strip() == mine
    assert (repo / "f.py").read_bytes() == worktree

"""After a prefer_staged commit, an unstaged peer hunk on the same path stays visible to git diff."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "core.autocrlf", "false"], repo)
    (repo / "f.txt").write_bytes(b"base\n")
    (repo / "g.txt").write_bytes(b"g\n")
    _git(["add", "--", "f.txt", "g.txt"], repo)
    _git(["commit", "-qm", "seed"], repo)
    return repo


def test_prefer_staged_commit_leaves_unstaged_peer_hunk_visible(tmp_path):
    repo = _repo(tmp_path)
    (repo / "f.txt").write_bytes(b"base\nstaged\n")
    _git(["add", "--", "f.txt"], repo)
    (repo / "f.txt").write_bytes(b"base\npeer!!\n")
    os.utime(repo / "f.txt", (1_600_000_000, 1_600_000_000))

    result = commit_v2._handler(
        {"paths": ["f.txt"], "message": "m", "prefer_staged": ["f.txt"]},
        repo_root=repo / ".git",
    )

    assert result["committed"] is True
    assert _git(["show", "HEAD:f.txt"], repo).stdout == "base\nstaged\n"
    assert _git(["diff", "--quiet", "--", "f.txt"], repo, check=False).returncode == 1
    assert "+peer!!" in _git(["diff", "--", "f.txt"], repo).stdout
    assert _git(["diff", "--cached", "--quiet", "--", "f.txt"], repo, check=False).returncode == 0


def test_clean_committed_path_stays_clean(tmp_path):
    repo = _repo(tmp_path)
    (repo / "f.txt").write_bytes(b"base\nmore\n")

    result = commit_v2._handler({"paths": ["f.txt"], "message": "m"}, repo_root=repo / ".git")

    assert result["committed"] is True
    assert _git(["diff", "--quiet", "--", "f.txt"], repo, check=False).returncode == 0

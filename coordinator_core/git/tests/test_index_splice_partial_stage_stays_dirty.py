"""A landing that splices a staged blob differing from the worktree must not record the worktree's
stat, or git reports the unlanded hunks clean. Throwaway repos under `tmp_path`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import index_write
from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MSG = "m\n\nSession-Id: 11111111-1111-4111-8111-111111111111"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).write_bytes(text.encode("utf-8"))


def _age(path: Path) -> None:
    """Push the mtime into the past so git does not treat the entry as racily clean."""
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.checkStat", "minimal")
    _write(root, "a.py", "one\ntwo\nthree\n")
    _git(root, "add", "--", "a.py")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _land(repo: Path, **extra) -> dict:
    reply = commit_v2._handler(
        {"paths": ["a.py"], "message": _MSG, **extra}, repo_root=repo / ".git"
    )
    assert reply["committed"] is True, reply
    return reply


def test_partially_staged_landing_leaves_residue_visible(repo):
    _write(repo, "a.py", "ONE\ntwo\nthree\n")
    _git(repo, "add", "--", "a.py")
    _write(repo, "a.py", "ONE\ntwo\nTHREE\n")
    _age(repo / "a.py")

    _land(repo, prefer_staged=["a.py"])

    assert _git(repo, "show", "HEAD:a.py") == "ONE\ntwo\nthree\n"
    assert _git(repo, "status", "--short", "--", "a.py").rstrip() == " M a.py"
    assert "+THREE" in _git(repo, "diff", "--", "a.py")
    assert _git(repo, "hash-object", "a.py").strip() != _git(repo, "rev-parse", ":a.py").strip()


def test_full_file_landing_reads_clean(repo):
    _write(repo, "a.py", "ONE\ntwo\nthree\n")

    _land(repo)

    assert _git(repo, "status", "--short", "--", "a.py") == ""
    assert _git(repo, "diff", "--", "a.py") == ""


def test_splice_records_real_stat_when_bytes_match_and_zero_when_not(repo):
    _write(repo, "a.py", "edited\n")
    good = _git(repo, "hash-object", "-w", "a.py").strip()
    _age(repo / "a.py")
    index_write.splice_index(repo, {"a.py": (0o100644, good)})
    assert _git(repo, "status", "--short", "--", "a.py").rstrip() == "M  a.py"
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["a.py"]
    assert _git(repo, "diff", "--name-only") == ""

    _write(repo, "a.py", "EDITED\n")
    _age(repo / "a.py")
    assert _git(repo, "diff", "--name-only").split() == ["a.py"]
    index_write.splice_index(repo, {"a.py": (0o100644, good)})
    assert _git(repo, "status", "--short", "--", "a.py").strip() == "MM a.py"

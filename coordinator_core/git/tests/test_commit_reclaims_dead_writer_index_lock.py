"""`commit_paths` at default arguments lands over an ownerless, unheld, non-fresh `index.lock`
(moved aside to `index.lock.reclaimed-*` with a `lock-reap.log` line) and still refuses over a
lock a process holds open or one younger than the reclaim floor, leaving its bytes untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

import pytest

from coordinator_core.git import commit, index_write
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="the unheld-lock probe exists only on Windows; POSIX reads as held"
)
_HOLDER = (
    "import sys; f = open(sys.argv[1], 'rb'); print('ready', flush=True); sys.stdin.read()"
)


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(commit, "_SPLICE_LOCK_BACKOFF_SECS", ())
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "seed.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "--", "seed.txt"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "seed.txt").write_text("v2\n", encoding="utf-8")
    return root


def _plant_lock(repo: Path, *, age_sec: float) -> Path:
    lock = repo / ".git" / "index.lock"
    lock.write_bytes((repo / ".git" / "index").read_bytes())
    old = time.time() - age_sec
    os.utime(lock, (old, old))
    return lock


def _land(repo: Path):
    return commit.commit_paths(
        repo, ["seed.txt"], "land\n",
        blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
    )


@_WINDOWS_ONLY
def test_ownerless_unheld_old_lock_is_moved_aside_and_commit_lands(repo):
    lock = _plant_lock(repo, age_sec=index_write._OWNERLESS_RECLAIM_FLOOR_SEC + 60)
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    log = repo / ".git" / "lock-reap.log"
    lines_before = log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    outcome = _land(repo)

    head_after = _git(["rev-parse", "HEAD"], repo).strip()
    assert head_after != head_before
    assert head_after == outcome.sha
    assert not lock.exists()
    assert len(list((repo / ".git").glob("index.lock.reclaimed-*"))) == 1
    lines_after = log.read_text(encoding="utf-8").splitlines()
    assert len(lines_after) == len(lines_before) + 1
    assert "reclaimed ownerless unheld" in lines_after[-1]


@_WINDOWS_ONLY
def test_lock_held_open_by_another_process_is_refused_and_untouched(repo):
    lock = _plant_lock(repo, age_sec=index_write._OWNERLESS_RECLAIM_FLOOR_SEC + 60)
    bytes_before = lock.read_bytes()
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        **no_console_creationflags(),
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(commit.CommitRefused, match="a peer holds the index"):
            _land(repo)
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
        holder.stdout.close()
    assert lock.read_bytes() == bytes_before
    assert not list((repo / ".git").glob("index.lock.reclaimed-*"))
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before


def test_fresh_lock_under_the_floor_is_refused_and_untouched(repo):
    lock = _plant_lock(repo, age_sec=0.0)
    bytes_before = lock.read_bytes()
    head_before = _git(["rev-parse", "HEAD"], repo).strip()

    with pytest.raises(commit.CommitRefused, match="a peer holds the index"):
        _land(repo)

    assert lock.read_bytes() == bytes_before
    assert not list((repo / ".git").glob("index.lock.reclaimed-*"))
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before

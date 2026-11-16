"""`commit_paths` at default arguments over a ref lock planted on the checked-out branch.

A dead-writer lock whose sha is a child of the ref and the reflog head is completed; any other
dead-writer lock is moved aside and the commit lands; a lock whose writer cannot be proven gone is
reported with its path, age and content, never as "moved since".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

import pytest

from coordinator_core.git import commit, ref_lock
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_AGE_SECS = ref_lock.STALE_REF_LOCK_FLOOR_SECS + 90


def _git(args, cwd: Path, **kw) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **kw, **no_console_creationflags(),
    ).stdout.strip()


def _land(repo: Path):
    return commit.commit_paths(
        repo, ["seed.txt"], "land\n",
        blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
    )


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"], **no_console_creationflags())
    child.wait()
    return child.pid


@pytest.fixture
def repo(tmp_path: Path) -> Path:
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


def _ref(repo: Path) -> str:
    return _git(["symbolic-ref", "HEAD"], repo)


def _plant(repo: Path, content: str, *, owner_pid: int | None, reflog_new: str | None) -> Path:
    gitdir = repo / ".git"
    ref = _ref(repo)
    lock = gitdir / (ref + ".lock")
    lock.write_bytes(content.encode("ascii") + b"\n")
    old = time.time() - _AGE_SECS
    os.utime(lock, (old, old))
    if owner_pid is not None:
        owner = ref_lock._owner_path(gitdir, ref)
        owner.parent.mkdir(parents=True, exist_ok=True)
        owner.write_text(json.dumps({"pid": owner_pid, "token": "dead-1", "started": old}), encoding="utf-8")
    if reflog_new is not None:
        head = _git(["rev-parse", "HEAD"], repo)
        log = gitdir / "logs" / ref
        with open(log, "ab") as fh:
            fh.write(f"{head} {reflog_new} t <t@example.com> {int(old)} +0000\tcommit: planted\n".encode())
    return lock


def _child_commit(repo: Path) -> str:
    tree = _git(["rev-parse", "HEAD^{tree}"], repo)
    head = _git(["rev-parse", "HEAD"], repo)
    return _git(["commit-tree", tree, "-p", head, "-m", "orphaned update"], repo)


def _records(repo: Path) -> list:
    log = repo / ".git" / ref_lock.RECOVERY_DIR / ref_lock.RECOVERY_LOG
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_dead_writer_child_lock_is_completed_then_next_commit_lands(repo) -> None:
    head = _git(["rev-parse", "HEAD"], repo)
    child = _child_commit(repo)
    lock = _plant(repo, child, owner_pid=_dead_pid(), reflog_new=child)

    # The completion moves the ref under the caller's captured HEAD, so this land reports the
    # peer-style move; the lock itself is gone and the ref holds the orphaned update.
    with pytest.raises(commit.CommitRefused, match="moved since"):
        _land(repo)

    assert not lock.exists()
    assert _git(["rev-parse", "HEAD"], repo) == child
    [record] = _records(repo)
    assert record["outcome"] == "completed" and record["content"] == child

    outcome = _land(repo)
    assert _git(["rev-parse", "HEAD"], repo) == outcome.sha
    assert _git(["rev-parse", "HEAD^"], repo) == child
    assert head != child


def test_dead_writer_non_child_lock_is_moved_aside_and_commit_lands(repo) -> None:
    head = _git(["rev-parse", "HEAD"], repo)
    stray = _stray_commit(repo)
    lock = _plant(repo, stray, owner_pid=_dead_pid(), reflog_new=stray)

    outcome = _land(repo)

    assert not lock.exists()
    assert _git(["rev-parse", "HEAD"], repo) == outcome.sha
    assert _git(["rev-parse", "HEAD^"], repo) == head
    [record] = _records(repo)
    assert record["outcome"] == "moved-aside" and record["content"] == stray
    assert Path(record["moved_to"]).read_bytes() == stray.encode("ascii") + b"\n"


def _stray_commit(repo: Path) -> str:
    tree = _git(["rev-parse", "HEAD^{tree}"], repo)
    return _git(["commit-tree", tree, "-m", "unrelated root"], repo)


def test_ownerless_old_lock_is_reported_with_path_age_and_content_never_moved_since(repo) -> None:
    head = _git(["rev-parse", "HEAD"], repo)
    child = _child_commit(repo)
    lock = _plant(repo, child, owner_pid=None, reflog_new=child)

    with pytest.raises(ref_lock.RefLockHeld) as raised:
        _land(repo)

    text = str(raised.value)
    assert str(lock) in text and child in text and "moved since" not in text
    assert raised.value.age_secs >= _AGE_SECS and raised.value.stale
    assert lock.read_bytes() == child.encode("ascii") + b"\n"
    assert _git(["rev-parse", "HEAD"], repo) == head

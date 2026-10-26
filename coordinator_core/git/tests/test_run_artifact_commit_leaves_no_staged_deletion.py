"""A run-artifact commit whose shared-index splice lost the lock race leaves no staged deletion.

(a) pins the producer: `commit_paths` under a lock held throughout raises `CommitRefused`
before any commit lands; HEAD is unmoved and `git diff --cached` is empty.
(b) pins the run-level contract: through `ceremony.commit_v2`, a publish that fails after the ref
moved, with the in-commit reset failing once, still leaves the index at HEAD for the committed path.
"""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest

from coordinator_core.git import commit, index_write
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-4333-8444-555555555555"
_ARTIFACT = "state/warp/ask-x/halted.json"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _staged_head_deletions(root: Path, prefix: str) -> list:
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=D", "-z", "--", prefix], root)
    return [p for p in out.split("\0") if p]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _git(["add", "--", "a.txt"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    artifact = root / _ARTIFACT
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"halted": true}\n', encoding="utf-8")
    return root


def test_a_commit_paths_under_held_lock_refuses_and_leaves_index_intact(repo, monkeypatch):
    """The producer: a held index.lock refuses before any commit lands; the index is untouched."""
    monkeypatch.setattr(commit, "_SPLICE_LOCK_BACKOFF_SECS", ())
    head_before = _git(["rev-parse", "HEAD"], repo)
    cached_before = _git(["diff", "--cached"], repo)
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    try:
        with pytest.raises(commit.CommitRefused):
            commit.commit_paths(
                repo, [_ARTIFACT], "land artifact\n",
                blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
            )
    finally:
        lock.unlink(missing_ok=True)
    assert _git(["rev-parse", "HEAD"], repo) == head_before
    assert _git(["diff", "--cached"], repo) == cached_before == ""
    assert _staged_head_deletions(repo, "state/warp/") == []


def test_b_commit_v2_leg_heals_when_publish_fails_after_the_ref_moved(repo, monkeypatch):
    """The publish fails after the ref moved and the in-commit reset fails once: the leg must still end with the index at HEAD."""

    def failing_publish(self):
        self.abort()
        raise index_write.IndexWriteError("simulated publish failure")

    real_reset = commit._reset_index_paths
    calls = {"n": 0}

    def reset_fails_once(repo_path, paths):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return real_reset(repo_path, paths)

    monkeypatch.setattr(index_write.PreparedSplice, "publish", failing_publish)
    monkeypatch.setattr(commit, "_reset_index_paths", reset_fails_once)

    reply = commit_v2._handler(
        {"paths": [_ARTIFACT], "message": "land artifact\n", "session_id": _SID},
        repo_root=repo / ".git",
    )

    assert reply["committed"] is True and reply["sha"], reply
    assert _git(["cat-file", "-e", f"HEAD:{_ARTIFACT}"], repo) == ""
    assert _staged_head_deletions(repo, "state/warp/") == []
    assert reply["index_stale"] == []

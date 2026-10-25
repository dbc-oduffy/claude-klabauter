"""A run-artifact commit whose shared-index splice lost the lock race leaves no staged deletion.

(a) pins the producer: `commit_paths` under a lock held throughout lands the commit, raises
`IndexStaleAfterCommit`, and the shared index is behind HEAD (a `D` entry under state/warp/).
(b) pins the run-level contract: through `ceremony.commit_v2`, a lock released before the leg ends
leaves the index at HEAD for the committed path.
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


def test_a_commit_paths_under_held_lock_leaves_staged_deletion(repo):
    """The producer: splice and reset both lose to a held index.lock; the commit still lands."""
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    try:
        with pytest.raises(index_write.IndexStaleAfterCommit) as caught:
            commit.commit_paths(
                repo, [_ARTIFACT], "land artifact\n",
                blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
            )
    finally:
        lock.unlink(missing_ok=True)
    assert _ARTIFACT in caught.value.paths
    assert _git(["cat-file", "-e", f"HEAD:{_ARTIFACT}"], repo) == ""
    assert _staged_head_deletions(repo, "state/warp/") == [_ARTIFACT]


def test_b_commit_v2_leg_heals_when_lock_released_before_leg_ends(repo, monkeypatch):
    """The lock outlasts the in-commit reset, then releases: the leg must end with the index at HEAD."""

    def busy(_repo_path, _updates, **_kw):
        raise index_write.IndexWriteLockBusy("simulated peer lock")

    real_reset = commit._reset_index_paths
    calls = {"n": 0}

    def reset_fails_once(repo_path, paths):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return real_reset(repo_path, paths)

    monkeypatch.setattr(index_write, "splice_index", busy)
    monkeypatch.setattr(commit, "_reset_index_paths", reset_fails_once)

    reply = commit_v2._handler(
        {"paths": [_ARTIFACT], "message": "land artifact\n", "session_id": _SID},
        repo_root=repo / ".git",
    )

    assert reply["committed"] is True and reply["sha"], reply
    assert _git(["cat-file", "-e", f"HEAD:{_ARTIFACT}"], repo) == ""
    assert _staged_head_deletions(repo, "state/warp/") == []
    assert reply["index_stale"] == []

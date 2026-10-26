"""A post-ref index splice that cannot take a peer's index.lock leaves no staged change.

The ref has moved when the splice runs, so an index left at its pre-commit bytes reads as staged
deletions of added paths and a staged revert of modified ones. The contract: whether
`commit_paths` returns or raises `IndexStaleAfterCommit`, once the peer releases the lock
`git diff --cached` is empty, and the commit itself landed.
"""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest

from coordinator_core.git import commit, index_write
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ADDED = ["new/a.txt", "new/b.txt", "new/c.txt"]
_MODIFIED = "seed.txt"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / _MODIFIED).write_text("v1\n", encoding="utf-8")
    _git(["add", "--", _MODIFIED], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / _MODIFIED).write_text("v2\n", encoding="utf-8")
    for rel in _ADDED:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"content of {rel}\n", encoding="utf-8")
    return root


def test_foreign_lock_refuses_before_ref_moves_and_index_is_byte_identical(repo, monkeypatch):
    monkeypatch.setattr(commit, "_SPLICE_LOCK_BACKOFF_SECS", ())
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    index_path = repo / ".git" / "index"
    index_before = index_path.read_bytes()
    cached_before = _git(["diff", "--cached"], repo)
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    try:
        with pytest.raises(commit.CommitRefused):
            commit.commit_paths(
                repo, [*_ADDED, _MODIFIED], "land\n",
                blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
            )
        assert lock.exists(), "the peer's lock must survive a refused commit"
    finally:
        lock.unlink(missing_ok=True)
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before
    assert index_path.read_bytes() == index_before
    assert _git(["diff", "--cached"], repo) == cached_before


def test_peer_lock_across_whole_splice_leaves_index_matching_new_head(repo, monkeypatch):
    monkeypatch.setattr(commit, "_SPLICE_LOCK_BACKOFF_SECS", ())
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    landed = None
    refused = False
    try:
        try:
            outcome = commit.commit_paths(
                repo, [*_ADDED, _MODIFIED], "land\n",
                blob_fallback=partial(commit.hash_worktree_blobs_via_spawn, cwd=repo),
            )
            landed = outcome.sha
        except index_write.IndexStaleAfterCommit as exc:
            landed = exc.outcome.sha
        except commit.CommitRefused:
            refused = True
    finally:
        lock.unlink(missing_ok=True)

    head_after = _git(["rev-parse", "HEAD"], repo).strip()
    if refused:
        assert head_after == head_before, "a refused commit must not move HEAD"
    else:
        assert landed is not None
        assert head_after == landed
    staged = _git(["diff", "--cached", "--name-status"], repo)
    assert staged == "", f"partial splice left staged changes against HEAD:\n{staged}"

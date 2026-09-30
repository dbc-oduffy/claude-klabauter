"""Falsifier helper: a commit leaves the shared index agreeing with HEAD.

`assert_commit_leaves_index_agreeing_with_head` is the contract the per-site
legs of the commit_paths swaps build against. Real git only: index/HEAD
divergence cannot be exhibited by a mocked git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Sequence

import pytest

from coordinator_core.git.commit import commit_paths
from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import make_diverged_path, make_peer_staged_path, real_git_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout


def _head_blobs(repo: Path, paths: Sequence[str]) -> dict[str, str]:
    return {p: _git(repo, "rev-parse", f"HEAD:{p}").strip() for p in paths}


def assert_commit_leaves_index_agreeing_with_head(
    repo: Path,
    commit_fn: Callable[[], object],
    paths: Sequence[str],
    *,
    peer_path: str,
) -> None:
    """Stage `peer_path`, run `commit_fn()`, then assert three legs.

    1. `git status --porcelain -- <paths>` is empty (tracked and clean).
    2. A bare `git commit` leaves every committed path's HEAD blob unchanged.
    3. `peer_path`'s index entry is byte-identical to its pre-commit entry.
    """
    make_peer_staged_path(repo, peer_path, "peer\n")
    peer_before = _git(repo, "ls-files", "-s", "--", peer_path)
    assert peer_before.strip(), "peer path was not staged"

    commit_fn()

    status = _git(repo, "status", "--porcelain", "--", *paths)
    assert status == "", f"leg 1: committed paths not clean: {status!r}"

    head_before = _head_blobs(repo, paths)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "x")
    head_after = _head_blobs(repo, paths)
    assert head_after == head_before, (
        f"leg 2: bare commit changed committed blobs: {head_before} -> {head_after}"
    )

    peer_after = _git(repo, "ls-files", "-s", "--", peer_path)
    assert peer_after == peer_before, (
        f"leg 3: peer index entry changed: {peer_before!r} -> {peer_after!r}"
    )


def _writer(repo: Path, paths: Sequence[str]) -> None:
    """Stage each path at one content, then edit the worktree past it."""
    for p in paths:
        make_diverged_path(repo, p, f"staged {p}\n", f"worktree {p}\n")


def test_commit_paths_satisfies_all_three_legs(tmp_path):
    repo = real_git_repo(tmp_path)
    paths = ["a/new.txt", "seed.txt"]

    def commit_fn():
        _writer(repo, paths)
        return commit_paths(repo, paths, "via commit_paths")

    assert_commit_leaves_index_agreeing_with_head(
        repo, commit_fn, paths, peer_path="peer.txt"
    )


def test_commit_scoped_fails_the_helper(tmp_path):
    repo = real_git_repo(tmp_path)
    paths = ["a/new.txt", "seed.txt"]

    def commit_fn():
        _writer(repo, paths)
        msg = repo / "MSG"
        msg.write_text("via commit_scoped\n", encoding="utf-8", newline="\n")
        result = git_native.commit_scoped(paths, str(msg), cwd=str(repo))
        assert result.ok, result.stderr

    with pytest.raises(AssertionError):
        assert_commit_leaves_index_agreeing_with_head(
            repo, commit_fn, paths, peer_path="peer.txt"
        )

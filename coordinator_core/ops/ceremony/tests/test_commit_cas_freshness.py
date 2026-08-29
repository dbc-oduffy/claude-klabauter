"""
coordinator_core.ops.ceremony.tests.test_commit_cas_freshness

C1 (claude-klabauter-75): `_commit_via_head_spine`'s AC11(b) index-`stat_
identity` re-check read `read_index(root).stat_identity` WITHOUT `fresh=
True`. Inside an open `index_read_cache_scope()`, that re-check could be
served the SAME snapshot its own comparand came from -- a compare-and-swap
that structurally cannot fail, on a tree ~50 sessions write concurrently.

This module pins the REFUSAL, not the read shape: with a cache scope open
and a simulated peer write to `.git/index` landing between the comparand
capture and the CAS, `_commit_via_head_spine` (reached through the public
`commit_scoped()` entrypoint, the same seam `test_commit_scoped_edges.py`'s
own AC11(b) case uses) must return the `compare-and-swap failed` `GitResult`
-- never a silent success that commits the peer's newer staged blob. A
`fresh=True` kwarg-shaped assertion is deliberately NOT what this pins --
that would pin today's call shape rather than the invariant, and would go
green on a rewrite that reads identity from the cache-scope context instead
of calling `read_index` at all. The oracle here is the observable CAS
outcome, exercised specifically with the cache scope this defect needed to
hide behind.

The paired classification fix this module used to pin alongside the refusal
(`commit_pipeline._classify_commit_scoped_failure_reason`) died with that
module (C4, docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-
pipeline-can-go.md) -- it had no caller left once `commit_pipeline.commit()`
was deleted, and nothing repoints it: this module's own refusal-observable
assertions below are the surviving oracle.

Spec backlink: docs/plans/2026-08-27-the-commit-op-resolves-one-pass-
context.md, chunk C1; dispatch brief state/dispatch-briefs/2026-08-27-the-
commit-op-resolves-one-pass-context/C1.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import pytest

from coordinator_core.git.git_state import index_read_cache_scope
from coordinator_core.ops.ceremony import git_native

from .fixtures.real_git import make_diverged_path, real_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **kwargs
    )


def _write_msg(tmp_path: Path, text: str = "cas freshness\n") -> Path:
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


def _committed_files_at_head(repo: Path) -> list[str]:
    result = _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo)
    return [line for line in result.stdout.splitlines() if line]


def _install_peer_action(monkeypatch, attr_name: str, peer_action: Callable[[], None]) -> None:
    """Same seam as `test_commit_scoped_edges.py`'s own AC6/AC11(b) helper
    -- hooks `git_native.<attr_name>` so `peer_action()` runs immediately
    before the real call, landing the peer's own side effect in the exact
    window between whatever this committer already read and that call.
    """
    real_fn = getattr(git_native, attr_name)

    def _wrapped(*args, **kwargs):
        peer_action()
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(git_native, attr_name, _wrapped)


def test_index_cas_refuses_inside_open_cache_scope_when_peer_writes_index(
    tmp_path, monkeypatch
):
    """The defect's own reproduction: identical race to `test_commit_
    scoped_edges.py`'s AC11(b) case, but run with an `index_read_cache_
    scope()` open around the whole commit -- the condition the un-fixed
    `read_index(root).stat_identity` (no `fresh=True`) could be served a
    cached, stale snapshot under, making the CAS a no-op. Pre-fix this
    silently commits the peer's newer staged blob; post-fix it refuses
    loud in the `compare-and-swap failed` diagnostic family.
    """
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="OURS\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    def _peer_restages_same_path() -> None:
        (repo / "file.txt").write_text("PEER-NEWER\n", encoding="utf-8")
        _git(["add", "--", "file.txt"], repo)

    # Same seam as `test_commit_scoped_edges.py`'s AC11(b) case: the last
    # real call `_commit_via_head_spine` makes before its own fresh
    # `read_index()` re-check, immediately preceding the CAS.
    _install_peer_action(monkeypatch, "_resolve_commit_identity", _peer_restages_same_path)

    with index_read_cache_scope():
        result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert not result.ok
    assert "compare-and-swap failed" in result.stderr
    # No commit landed at all -- HEAD is still the seed commit.
    log_count = _git(["rev-list", "--count", "HEAD"], repo).stdout.strip()
    assert log_count == "1"
    # The peer's newer staged blob was never silently committed.
    assert "file.txt" not in _committed_files_at_head(repo)

    # Reconcile the peer's own still-pending staged edit before any later
    # test in this process touches the same repo path.
    _git(["reset", "--", "file.txt"], repo)
    (repo / "file.txt").unlink()


# `test_classify_marks_head_spine_index_refusal_distinct_from_head_moved`
# (deleted, C4 of docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-
# the-pipeline-can-go.md): a pure unit test of `commit_pipeline._classify_
# commit_scoped_failure_reason`, which died with the module -- it had no
# caller left once `commit_pipeline.commit()` was deleted, and nothing
# repoints it.

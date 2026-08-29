"""
coordinator_core.ops.ceremony.tests.test_push_reject_landed_by_peer

Guards the reject-recovery arm that answers "a peer already pushed these
commits" WITHOUT needing a clean worktree.

The defect these tests pin: on this fleet every session drives the SAME
worktree and the SAME branch, so a peer's push routinely carries this
session's commits to the remote. Our own push then rejects non-fast-forward
for a range that is already published, and `push_with_retry` answered that
by reaching for `_rebase_onto_fetched_ref` -- which refuses outright on a
dirty tree, and at the 50-70 concurrent-session load norm the tree is never
clean. The recovery path was therefore dead in practice: a `failed` push
reporting "rebase recovery cannot run: worktree has uncommitted changes"
for work that had already reached the remote, with no correct way forward
that did not involve committing or stashing a peer's files.

Negative-spec: these tests do NOT assert that a genuinely diverged HEAD is
papered over. `test_genuine_divergence_still_reports_the_dirty_rebase_refusal`
is the counterweight -- when our commits are NOT on the remote, the dirty
worktree is still a real blocker and must still be reported as one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native, push as push_mod
from coordinator_core.ops.ceremony.git_native import GitResult

pytestmark = [pytest.mark.spawns_process]


_NON_FAST_FORWARD_STDERR = (
    "! [rejected] work/x -> work/x (non-fast-forward)\n"
    "error: failed to push some refs to 'origin'\n"
    "hint: Updates were rejected because the tip of your current branch is behind\n"
)


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _init_repo_with_upstream(tmp_path: Path) -> Path:
    """A repo on `work/x` with a real bare origin and a real upstream
    tracking ref -- `_resolve_upstream_local` reads `.git/config`, so the
    branch.<name>.remote/merge keys `push -u` writes must genuinely exist."""
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)
    return repo


def _dirty_the_tree_with_a_peers_file(repo: Path) -> None:
    """An uncommitted change nobody in this test owns -- the standing state
    of the shared worktree, and the exact input `_rebase_onto_fetched_ref`
    refuses on."""
    (repo / "peer-scratch.txt").write_text("a peer is mid-edit", encoding="utf-8")


def _reject_then_never_again(monkeypatch, push_calls: list) -> None:
    def _fake_push(*a, **kw):
        push_calls.append(1)
        return GitResult(returncode=1, stdout="", stderr=_NON_FAST_FORWARD_STDERR)

    monkeypatch.setattr(git_native, "push", _fake_push)
    monkeypatch.setattr(
        git_native, "fetch", lambda *a, **kw: GitResult(returncode=0, stdout="", stderr="")
    )


def test_head_equal_to_upstream_after_reject_is_landed_by_peer_not_a_rebase(
    tmp_path, monkeypatch
):
    """Arm 1, zero spawns: our commit IS the remote tip because a peer
    pushed it. The reject must resolve to `push:landed-by-peer` at exit 0,
    with the rebase never attempted -- on a DIRTY tree, which is the only
    state this box is ever in."""
    repo = _init_repo_with_upstream(tmp_path)
    _dirty_the_tree_with_a_peers_file(repo)

    push_calls: list = []
    _reject_then_never_again(monkeypatch, push_calls)

    rebase_calls: list = []
    monkeypatch.setattr(
        push_mod,
        "_rebase_onto_fetched_ref",
        lambda *a, **kw: rebase_calls.append(1) or (1, "should never run"),
    )

    outcome = push_mod.push_with_retry(repo)

    assert rebase_calls == []
    assert outcome.exit_code == 0
    assert outcome.failed == []
    assert outcome.unconfirmed == []
    assert outcome.skipped == ["push:landed-by-peer"]
    assert len(push_calls) == 1


def test_head_is_ancestor_of_upstream_after_reject_is_landed_by_peer(tmp_path, monkeypatch):
    """Arm 2, one spawn: a peer pushed our commits AND some of their own on
    top, so HEAD is an ancestor of -- not equal to -- the fetched tip. Still
    nothing to rebase and nothing left to push."""
    repo = _init_repo_with_upstream(tmp_path)
    ours = _git(["rev-parse", "HEAD"], repo).strip()

    (repo / "peer.txt").write_text("peer work on top of ours", encoding="utf-8")
    _git(["add", "--", "peer.txt"], repo)
    _git(["commit", "-q", "-m", "peer commit on top"], repo)
    _git(["push", "-q", "origin", "work/x"], repo)
    _git(["reset", "-q", "--hard", ours], repo)

    _dirty_the_tree_with_a_peers_file(repo)

    push_calls: list = []
    _reject_then_never_again(monkeypatch, push_calls)

    rebase_calls: list = []
    monkeypatch.setattr(
        push_mod,
        "_rebase_onto_fetched_ref",
        lambda *a, **kw: rebase_calls.append(1) or (1, "should never run"),
    )

    outcome = push_mod.push_with_retry(repo)

    assert rebase_calls == []
    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:landed-by-peer"]
    assert outcome.failed == []


def test_genuine_divergence_still_reports_the_dirty_rebase_refusal(tmp_path, monkeypatch):
    """The counterweight: our commit is NOT on the remote and the tree is
    dirty. That is a real blocker and must still be reported as one -- this
    fix narrows the rebase path, it does not silence it."""
    repo = _init_repo_with_upstream(tmp_path)

    (repo / "ours.txt").write_text("unpublished work", encoding="utf-8")
    _git(["add", "--", "ours.txt"], repo)
    _git(["commit", "-q", "-m", "ours, not on the remote"], repo)

    _dirty_the_tree_with_a_peers_file(repo)

    push_calls: list = []
    _reject_then_never_again(monkeypatch, push_calls)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code != 0
    assert outcome.skipped == []
    assert outcome.failed
    assert "uncommitted changes" in outcome.failed[0]


def test_head_already_reached_upstream_is_false_when_is_ancestor_cannot_answer(
    tmp_path, monkeypatch
):
    """An indeterminate `--is-ancestor` (exit 128, a bad ref, a spawn
    failure) is never read as a confident "yes" -- the caller falls through
    to the rebase exactly as it did before this arm existed."""
    repo = _init_repo_with_upstream(tmp_path)

    monkeypatch.setattr(
        git_native,
        "merge_base_is_ancestor",
        lambda *a, **kw: GitResult(returncode=128, stdout="", stderr="fatal: bad revision"),
    )

    assert push_mod._head_already_reached_upstream(repo, "origin/work/x", None) is False

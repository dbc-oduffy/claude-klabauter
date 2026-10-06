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
papered over. The counterweights are the two `..._divergence...` tests below:
when our commits are NOT on the remote the recovery must actually integrate them
(2026-08-30 PM ruling: publishing does not require a pristine tree, so the
dirty case replays rather than refusing), and when a peer's uncommitted edit
sits on a path that the merge would have to overwrite, the recovery must DECLINE
having touched nothing -- never overwrite it, never stash it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native, push as push_mod
from coordinator_core.ops.ceremony.git_native import GitResult

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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
    # `_init_repo_with_upstream` configures a same-name upstream, so
    # `push_with_retry` pushes by explicit refspec (see that function's
    # `upstream_info` branch, `push_with_retry: push a configured upstream
    # by explicit refspec`) -- both call shapes must reject identically or
    # the real (unmocked) `push_refspec` reports "up to date" against the
    # real remote and this fake reject is never seen.
    monkeypatch.setattr(git_native, "push_refspec", _fake_push)
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


def _advance_the_remote(tmp_path: Path, repo: Path, filename: str, body: str) -> None:
    """Land a commit on the remote that this worktree does not have, via a
    second clone -- the genuine-divergence input, as opposed to a faked
    reject. `repo` is left unfetched so its next push really is rejected."""
    other = tmp_path / f"other-{filename}"
    _git(["clone", "-q", str(tmp_path / "origin.git"), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", "work/x"], other)
    (other / filename).write_text(body, encoding="utf-8")
    _git(["add", "--", filename], other)
    _git(["commit", "-q", "-m", f"remote-only: {filename}"], other)
    _git(["push", "-q", "origin", "work/x"], other)


def test_genuine_divergence_on_a_dirty_tree_merges_and_lands(tmp_path):
    """The 2026-08-30 ruling, pinned: our commit is NOT on the remote, the
    remote has moved, and the tree is dirty with a peer's uncommitted file.
    The push must LAND -- a dirty tree is the standing state of this box, not
    a reason to stop publishing -- and the peer's file must come through
    untouched."""
    repo = _init_repo_with_upstream(tmp_path)
    _advance_the_remote(tmp_path, repo, "upstream-only.txt", "landed elsewhere")

    (repo / "ours.txt").write_text("unpublished work", encoding="utf-8")
    _git(["add", "--", "ours.txt"], repo)
    _git(["commit", "-q", "-m", "ours, not on the remote"], repo)

    _dirty_the_tree_with_a_peers_file(repo)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    assert outcome.exit_code == 0
    # Our commit reached the remote, and the remote-only commit reached us.
    assert _git(["rev-list", "--count", "origin/work/x..HEAD"], repo).strip() == "0"
    assert (repo / "upstream-only.txt").read_text(encoding="utf-8") == "landed elsewhere"
    # The peer's uncommitted file is exactly as they left it.
    assert (repo / "peer-scratch.txt").read_text(encoding="utf-8") == "a peer is mid-edit"


def test_divergence_over_a_peers_uncommitted_edit_declines_touching_nothing(tmp_path):
    """The counterweight to the ruling: when the merge would have to
    overwrite a path a peer is mid-edit on, it must DECLINE -- reporting the
    failure, leaving that peer's bytes, the index and the branch ref exactly
    as it found them. Never a stash, never an overwrite."""
    repo = _init_repo_with_upstream(tmp_path)
    _advance_the_remote(tmp_path, repo, "README.md", "rewritten on the remote")

    (repo / "ours.txt").write_text("unpublished work", encoding="utf-8")
    _git(["add", "--", "ours.txt"], repo)
    _git(["commit", "-q", "-m", "ours, not on the remote"], repo)

    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    (repo / "README.md").write_text("a peer is mid-edit here", encoding="utf-8")

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code != 0
    assert outcome.failed
    assert "nothing touched" in outcome.failed[0], outcome.failed
    assert (repo / "README.md").read_text(encoding="utf-8") == "a peer is mid-edit here"
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before


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


def _commits(repo: Path, n: int) -> None:
    for i in range(n):
        (repo / f"ours-{i}.txt").write_text(f"unpublished {i}", encoding="utf-8")
        _git(["add", "--", f"ours-{i}.txt"], repo)
        _git(["commit", "-q", "-m", f"ours {i}"], repo)


def test_replay_never_rewrites_unpushed(tmp_path):
    """Five unpushed commits, a moved remote and a dirty tree: recovery keeps
    every old sha reachable from the new tip, stamps a reflog message, and
    leaves the peer's uncommitted file alone."""
    repo = _init_repo_with_upstream(tmp_path)
    _advance_the_remote(tmp_path, repo, "upstream-only.txt", "landed elsewhere")
    _commits(repo, 5)
    old_shas = _git(["rev-list", "origin/work/x..HEAD"], repo).split()
    assert len(old_shas) == 5
    _dirty_the_tree_with_a_peers_file(repo)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    new_tip = _git(["rev-parse", "HEAD"], repo).strip()
    for sha in old_shas:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, new_tip],
            cwd=str(repo), check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    reflog = _git(["reflog", "show", "--format=%gs", "refs/heads/work/x"], repo).splitlines()
    assert reflog[0].strip() and "merge" in reflog[0]
    assert (repo / "peer-scratch.txt").read_text(encoding="utf-8") == "a peer is mid-edit"
    assert (repo / "upstream-only.txt").read_text(encoding="utf-8") == "landed elsewhere"


def test_merge_conflict_declines_touching_nothing(tmp_path):
    repo = _init_repo_with_upstream(tmp_path)
    _advance_the_remote(tmp_path, repo, "README.md", "remote version")
    (repo / "README.md").write_text("local version", encoding="utf-8")
    _git(["commit", "-q", "-am", "local conflicting"], repo)
    _dirty_the_tree_with_a_peers_file(repo)
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    index_before = _git(["ls-files", "-s"], repo)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.exit_code != 0
    assert outcome.failed
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before
    assert _git(["ls-files", "-s"], repo) == index_before
    assert (repo / "README.md").read_text(encoding="utf-8") == "local version"
    assert (repo / "peer-scratch.txt").read_text(encoding="utf-8") == "a peer is mid-edit"


def test_update_ref_refuses_non_descendant(tmp_path):
    repo = _init_repo_with_upstream(tmp_path)
    base = _git(["rev-parse", "HEAD"], repo).strip()
    _commits(repo, 1)
    tip = _git(["rev-parse", "HEAD"], repo).strip()
    _git(["checkout", "-q", "-b", "side", base], repo)
    (repo / "sibling.txt").write_text("sibling", encoding="utf-8")
    _git(["add", "--", "sibling.txt"], repo)
    _git(["commit", "-q", "-m", "sibling"], repo)
    sibling = _git(["rev-parse", "HEAD"], repo).strip()
    _git(["checkout", "-q", "work/x"], repo)

    with pytest.raises(ValueError, match="not an ancestor"):
        git_native.update_ref(repo, "refs/heads/work/x", sibling, tip, "rewrite")
    assert _git(["rev-parse", "refs/heads/work/x"], repo).strip() == tip

    assert git_native.update_ref(
        repo, "refs/heads/work/x", sibling, tip, "deliberate", allow_rewrite=True
    ).ok


def test_update_ref_carries_reflog_message(tmp_path):
    repo = _init_repo_with_upstream(tmp_path)
    _commits(repo, 1)
    tip = _git(["rev-parse", "HEAD"], repo).strip()

    assert git_native.update_ref(repo, "refs/heads/side", tip, "0" * 40, "made side").ok
    reflog = _git(["reflog", "show", "--format=%gs", "refs/heads/side"], repo)
    assert "made side" in reflog
    with pytest.raises(ValueError):
        git_native.update_ref(repo, "refs/heads/side", tip, "0" * 40, "")


def test_merge_keeps_a_peers_staged_entry_on_an_untouched_path(tmp_path):
    """A peer's STAGED (not just modified) entry on a path upstream did not
    touch must survive the two-tree read-tree exactly: index blob and bytes."""
    repo = _init_repo_with_upstream(tmp_path)
    _advance_the_remote(tmp_path, repo, "upstream-only.txt", "landed elsewhere")
    _commits(repo, 1)
    (repo / "peer-staged.txt").write_text("staged by a peer", encoding="utf-8")
    _git(["add", "--", "peer-staged.txt"], repo)
    staged_before = _git(["ls-files", "-s", "--", "peer-staged.txt"], repo)

    outcome = push_mod.push_with_retry(repo)

    assert outcome.failed == [], outcome.failed
    assert _git(["ls-files", "-s", "--", "peer-staged.txt"], repo) == staged_before
    assert "peer-staged.txt" in _git(["diff", "--cached", "--name-only"], repo)


def test_cas_failure_rolls_index_and_worktree_back(tmp_path, monkeypatch):
    """update-ref losing its CAS must leave refs, index and worktree exactly
    as found -- the reverse read-tree is the only thing standing between a
    refused ref move and a staged diff nobody authored."""
    repo = _init_repo_with_upstream(tmp_path)
    _advance_the_remote(tmp_path, repo, "upstream-only.txt", "landed elsewhere")
    _commits(repo, 1)
    _dirty_the_tree_with_a_peers_file(repo)
    _git(["fetch", "-q", "origin"], repo)
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    index_before = _git(["ls-files", "-s"], repo)
    monkeypatch.setattr(
        git_native,
        "update_ref",
        lambda *a, **k: GitResult(returncode=1, stdout="", stderr="fatal: cannot lock ref"),
    )

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code != 0 and "update-ref" in reason and "rollback failed" not in reason
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before
    assert _git(["ls-files", "-s"], repo) == index_before
    assert not (repo / "upstream-only.txt").exists()
    assert (repo / "peer-scratch.txt").read_text(encoding="utf-8") == "a peer is mid-edit"

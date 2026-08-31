"""
coordinator_core.ops.ceremony.tests.test_commit_scoped_in_process

Tests for `git_native._commit_scoped_private_index()`'s C8b rewire --
wiring C8a's tree-input assembler (`_assemble_commit_tree_input`) to C4's
shared "rewrite HEAD's tree spine -> commit object -> locked `cas_ref` CAS"
landing helper (`_commit_via_head_spine`), replacing the old private-index
ladder (`read-tree` + per-path `update-index --cacheinfo` fan-out +
`write-tree`/`commit-tree`/`update-ref`) for the COMMON case.

Spec backlink: docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md
chunk C8b.

Runs against REAL git via `fixtures.real_git` (allowlisted below) -- same
reason `test_commit_scoped.py` does: index/worktree divergence cannot be
exhibited by a mocked git, and the spawn-count assertions here need a real
`subprocess.run` to spy on.

Coverage:
  - the fast (spine-rewrite) path commits correctly and issues NO
    `update-index --cacheinfo` fan-out, regardless of how many staged paths
    are in the batch (AC13's spawn floor).
  - worktree-sourced paths are hashed via AT MOST ONE `git hash-object
    --stdin-paths` spawn, regardless of pathspec length (AC13) -- zero once
    C3's in-process `write_object` route clears the clean-pipeline pre-check,
    which is the ordinary case; never one spawn per path.
  - a brand-new file under a brand-new subdirectory (HEAD has no entry for
    that directory at all) falls back to the private-index ladder and still
    commits correctly -- `_commit_via_head_spine`'s own documented "take
    the ladder" precondition.
  - a genuinely staged-for-deletion path (present in `diverged`, absent
    from the real index) is actually REMOVED from the resulting tree on the
    fast path, never resurrected via an implicit `read-tree HEAD` seed --
    the trap `_assemble_commit_tree_input`'s own docstring names.
  - `mode_only_paths` exclusion-report behaviour survives the rewire
    byte-identically (already covered end-to-end by
    `test_commit_scoped.py::test_mixed_batch_mode_only_and_content_edit_
    both_commit`; re-pinned here directly against the rewired function).
  - bound 7: a path removed from HEAD via `worktree_deleted` leaves NO
    surviving entry in the shared `.git/index`, so the next bare commit by
    any peer session cannot resurrect it -- plus the cost pins that keep
    that reconciliation off every ordinary commit.

Bound-7 backlink: state/handoffs/2026-08-30-the-commit-path-scoped-commits-
the-share.md (first owed item).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags
from .fixtures.real_git import (
    make_agree_path,
    make_diverged_path,
    make_peer_staged_path,
    real_git_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _write_msg(tmp_path: Path, text: str = "a commit message\n") -> Path:
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


def _committed_content_at_head(repo: Path, rel: str) -> str:
    return _git(["show", f"HEAD:{rel}"], repo).stdout


def _committed_files_at_head(repo: Path) -> list[str]:
    result = _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo)
    return [line for line in result.stdout.splitlines() if line]


def _spy_git_argvs(monkeypatch) -> list[list[str]]:
    """Install a spy on `git_native._git()` (not `subprocess.run` directly)
    so the recorded argv list omits the `git` token itself, matching this
    module's own `_git(args, ...)` call-site convention -- e.g. a recorded
    entry of `["update-index", ...]`, never `["git", "update-index", ...]`.
    """
    real_git_fn = git_native._git
    argvs: list[list[str]] = []

    def _spy(args, **kwargs):
        argvs.append(list(args))
        return real_git_fn(args, **kwargs)

    monkeypatch.setattr(git_native, "_git", _spy)
    return argvs


# ---------------------------------------------------------------------------
# Fast path: no per-path cacheinfo fan-out
# ---------------------------------------------------------------------------


def test_fast_path_diverged_batch_issues_no_cacheinfo_fanout(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    for i in range(25):
        make_diverged_path(
            repo, f"file_{i:03d}.txt", staged_content=f"STAGED {i}\n", worktree_content=f"WORKTREE {i}\n"
        )
    msg_file = _write_msg(tmp_path)
    argvs = _spy_git_argvs(monkeypatch)

    result = git_native.commit_scoped(
        [f"file_{i:03d}.txt" for i in range(25)], msg_file, repo
    )

    assert result.ok, result.stderr
    for i in range(25):
        assert _committed_content_at_head(repo, f"file_{i:03d}.txt") == f"STAGED {i}\n"

    # The invariant is NO PER-PATH FAN-OUT, which is not the same as zero
    # `--cacheinfo` calls and was written as if it were. Bound 6 (this
    # function's post-landing shared-index refresh, added after this test)
    # legitimately issues ONE batched `update-index --add` carrying every
    # committed path on repeated `--cacheinfo` flags -- 25 paths, one spawn.
    # Asserting `== []` failed on that batch while a genuine 25-spawn
    # regression and a correct 1-spawn refresh were indistinguishable to it.
    cacheinfo_calls = [a for a in argvs if len(a) > 1 and a[0] == "update-index" and "--cacheinfo" in a]
    assert len(cacheinfo_calls) <= 1, (
        "fast (spine-rewrite) path must never fan `update-index --cacheinfo` "
        f"out per path -- got {len(cacheinfo_calls)} calls: {cacheinfo_calls}"
    )
    for call in cacheinfo_calls:
        assert call.count("--cacheinfo") == 25, (
            "the one permitted `--cacheinfo` call is bound 6's batched "
            "shared-index refresh, which must carry every committed path -- "
            f"got {call.count('--cacheinfo')} of 25"
        )
    assert not any(a[:1] == ["read-tree"] for a in argvs)
    assert not any(a[:1] == ["write-tree"] for a in argvs)
    assert not any(a[:1] == ["commit-tree"] for a in argvs)


def test_fast_path_worktree_sourced_paths_use_one_hash_object_spawn(tmp_path, monkeypatch):
    """A batch mixing one diverged (staged-source) path with several
    non-diverged (worktree-source, brand-new) TOP-LEVEL paths -- all under
    HEAD's existing root, so the fast path applies -- must hash every
    worktree-sourced path in exactly ONE `git hash-object --stdin-paths`
    spawn, never one per path."""
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "diverged.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    new_paths = [f"new_{i:03d}.txt" for i in range(10)]
    for p in new_paths:
        (repo / p).write_text(f"content {p}\n", encoding="utf-8")
        _git(["add", "--", p], repo)
    msg_file = _write_msg(tmp_path)
    argvs = _spy_git_argvs(monkeypatch)

    result = git_native.commit_scoped(["diverged.txt", *new_paths], msg_file, repo)

    assert result.ok, result.stderr
    assert _committed_content_at_head(repo, "diverged.txt") == "STAGED\n"
    for p in new_paths:
        assert _committed_content_at_head(repo, p) == f"content {p}\n"

    # `== 1` was the floor when this test was written and C3 has since beaten
    # it: `_hash_worktree_blobs` writes each worktree blob IN PROCESS via
    # `write_object` for every path a clean-pipeline pre-check clears, and
    # falls back to the one `git hash-object -w --stdin-paths` spawn only for
    # a path that pre-check REFUSES. Ten ordinary text files clear it, so the
    # observed count here is zero. The invariant that survives -- and the only
    # one this test was ever protecting -- is that the fallback never fans out
    # per path.
    hash_object_calls = [
        a for a in argvs if len(a) > 1 and a[0] == "hash-object" and "--stdin-paths" in a
    ]
    assert len(hash_object_calls) <= 1, hash_object_calls


# ---------------------------------------------------------------------------
# Ladder fall-back: a brand-new subdirectory absent from HEAD's tree
# ---------------------------------------------------------------------------


def test_new_file_under_new_subdirectory_falls_back_to_ladder_and_commits(tmp_path):
    """`_commit_via_head_spine` returns `None` (take the ladder) when a
    changed path's parent directory does not exist in HEAD's tree at all --
    a brand-new file under a brand-new subdirectory, alongside a genuinely
    diverged path elsewhere, must still land correctly via the private-index
    ladder rather than failing the whole commit."""
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "diverged.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    (repo / "newdir").mkdir()
    (repo / "newdir" / "brand_new.txt").write_text("brand new content\n", encoding="utf-8")
    _git(["add", "--", "newdir/brand_new.txt"], repo)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["diverged.txt", "newdir/brand_new.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert _committed_content_at_head(repo, "diverged.txt") == "STAGED\n"
    assert _committed_content_at_head(repo, "newdir/brand_new.txt") == "brand new content\n"


def test_new_file_under_new_subdirectory_ladder_never_absorbs_peer_staged_file(tmp_path):
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "diverged.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    (repo / "newdir").mkdir()
    (repo / "newdir" / "brand_new.txt").write_text("brand new content\n", encoding="utf-8")
    _git(["add", "--", "newdir/brand_new.txt"], repo)
    make_peer_staged_path(repo, "peer.txt", "peer content\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["diverged.txt", "newdir/brand_new.txt"], msg_file, repo)

    assert result.ok, result.stderr
    committed = _committed_files_at_head(repo)
    assert "peer.txt" not in committed
    assert set(committed) == {"diverged.txt", "newdir/brand_new.txt"}


# ---------------------------------------------------------------------------
# Staged deletion: no implicit resurrection via a `read-tree HEAD` seed
# ---------------------------------------------------------------------------


def test_staged_deletion_is_actually_removed_not_resurrected(tmp_path):
    """A path resolved `_SOURCE_STAGED` (a `diverged` member) but ABSENT
    from the real index at call time -- a staged deletion -- must land as
    an ACTUAL deletion in the committed tree, never resurrected the way a
    `read-tree HEAD`-seeded private index would implicitly do (the exact
    trap `_assemble_commit_tree_input`'s own docstring names).

    Exercises `_commit_scoped_private_index` DIRECTLY, like this file's
    sibling `test_commit_scoped.py::test_private_index_seeding_succeeds_
    when_shared_index_file_is_absent` already does for a related shape --
    `git`'s own porcelain=v2 records a staged-alone deletion's worktree
    column (`Y`) as unchanged (`.`) even when the worktree copy has since
    been independently re-created, so `commit_scoped()`'s own
    `diverging_paths()` classification never routes this exact shape into
    `diverged` through the real entrypoint; the private-index branch's own
    ABSENT handling is what this test targets, isolated from that
    classification question."""
    repo = real_git_repo(tmp_path)
    (repo / "doomed.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "doomed.txt"], repo)
    _git(["commit", "-q", "-m", "add doomed.txt"], repo)
    _git(["rm", "-q", "--cached", "doomed.txt"], repo)
    (repo / "doomed.txt").write_text("resurrected by mistake?\n", encoding="utf-8")
    msg_file = _write_msg(tmp_path)

    result = git_native._commit_scoped_private_index(["doomed.txt"], [], msg_file, repo)

    assert result.ok, result.stderr
    # `git show --name-only` (the `_committed_files_at_head` helper) lists
    # every path the commit's DIFF touched -- a deletion legitimately
    # appears there too, so tree PRESENCE is checked directly via `ls-tree`
    # instead.
    ls_tree = _git(["ls-tree", "HEAD", "--", "doomed.txt"], repo).stdout
    assert ls_tree == "", f"doomed.txt must not exist in the new HEAD tree: {ls_tree!r}"
    show = subprocess.run(
        ["git", "show", "HEAD:doomed.txt"], cwd=str(repo), capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert show.returncode != 0, "doomed.txt must not exist in the new HEAD tree"


# ---------------------------------------------------------------------------
# mode_only_paths exclusion-report behaviour (re-pinned directly against the
# rewired function; end-to-end coverage already lives in test_commit_scoped.py)
# ---------------------------------------------------------------------------


def test_mode_only_path_excluded_from_worktree_excluded_report(tmp_path):
    repo = real_git_repo(tmp_path)
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (repo / "content.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "script.sh", "content.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)

    _git(["update-index", "--chmod=+x", "--", "script.sh"], repo)
    make_agree_path(repo, "content.txt", "edited\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["script.sh", "content.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert "script.sh" not in result.worktree_excluded
    assert "worktree edits" not in result.stderr
    mode_line = _git(["ls-tree", "HEAD", "--", "script.sh"], repo).stdout
    assert mode_line.split()[0] == "100755"


# ---------------------------------------------------------------------------
# Bound 7: the shared index must not keep an entry for a path this commit
# removed from HEAD
# ---------------------------------------------------------------------------


def _archival_deletion_repo(tmp_path: Path) -> Path:
    """The live archival-sweep shape `_assemble_commit_tree_input`'s own
    `worktree_deleted` paragraph names: a tracked, committed file is removed
    from DISK by something that touches no index (`os.replace`, in
    `commit_pipeline._run_in_plane_archive_sweep`), so its `.git/index` entry
    still stands when the ceremony commit is asked to land the removal.

    Deliberately not a `git rm`: that is the OTHER way a path reaches
    `absent` (an already-staged deletion), which leaves nothing behind and is
    pinned separately below.
    """
    repo = real_git_repo(tmp_path)
    (repo / "gone.txt").write_text("original\n", encoding="utf-8")
    (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(["add", "--", "gone.txt", "kept.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    (repo / "gone.txt").unlink()
    return repo


def test_worktree_deleted_path_leaves_no_entry_in_the_shared_index(tmp_path):
    """The defect this bound exists for, asserted where it actually bites.

    Before bound 7 this sequence committed the removal correctly and then
    left `AD gone.txt` in the shared index -- HEAD without the path, the
    worktree without the path, `.git/index` still carrying HEAD's PRE-commit
    blob for it. The assertion on `git status` is the one that fails loud on
    a regression; the bare-commit assertion below is what that residue
    actually COSTS.
    """
    repo = _archival_deletion_repo(tmp_path)
    msg_file = _write_msg(tmp_path, "remove gone.txt\n")

    result = git_native.commit_scoped(["gone.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert _git(["ls-tree", "--name-only", "HEAD"], repo).stdout.split() == [
        "kept.txt", "seed.txt",
    ]
    assert _git(["ls-files"], repo).stdout.split() == ["kept.txt", "seed.txt"], (
        "the shared index must not keep an entry for a path this commit "
        "removed from HEAD -- a surviving entry reads as `AD` and the next "
        "bare commit resurrects the file"
    )
    assert _git(["status", "--porcelain"], repo).stdout.strip() == ""


def test_the_next_bare_commit_cannot_resurrect_a_removed_path(tmp_path):
    """The cost of the residue, stated as the outcome rather than the state:
    a peer session's ordinary `git commit` immediately after this one must
    not put the removed file back. This is the shape that lands WRONG
    CONTENT -- HEAD's pre-removal blob -- at rc=0, with nothing in the
    commit's own report to notice it by.
    """
    repo = _archival_deletion_repo(tmp_path)
    msg_file = _write_msg(tmp_path, "remove gone.txt\n")
    assert git_native.commit_scoped(["gone.txt"], msg_file, repo).ok

    (repo / "kept.txt").write_text("kept, edited by a peer\n", encoding="utf-8")
    _git(["add", "--", "kept.txt"], repo)
    _git(["commit", "-q", "-m", "peer commit"], repo)

    assert "gone.txt" not in _git(["ls-tree", "--name-only", "HEAD"], repo).stdout.split()


def test_already_staged_deletion_pays_no_reconcile_spawn(tmp_path, monkeypatch):
    """Class (1) of `absent`: the caller pre-`git rm`'d the path, so the
    shared index has already forgotten it and there is genuinely nothing to
    reconcile. Bound 7 decides that off the index snapshot it already holds,
    so this must cost ZERO additional spawns -- the reconciliation is for the
    class that leaves residue, not for every deletion.
    """
    repo = real_git_repo(tmp_path)
    (repo / "gone.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "gone.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    _git(["rm", "-q", "--", "gone.txt"], repo)
    msg_file = _write_msg(tmp_path, "remove gone.txt\n")
    argvs = _spy_git_argvs(monkeypatch)

    result = git_native.commit_scoped(["gone.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert "gone.txt" not in _git(["ls-tree", "--name-only", "HEAD"], repo).stdout.split()
    assert [a for a in argvs if "--force-remove" in a] == []


def test_ordinary_commit_pays_no_reconcile_spawn(tmp_path, monkeypatch):
    """The cost pin that matters most: a commit with an empty `absent` set --
    every ordinary edit -- must not gain a spawn or a second `read_index`
    from this bound. `.git/index` is a 5.2MB pure-Python parse in this repo;
    an unconditional re-read here would be a brightline defect on the
    commonest path in the engine.
    """
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "edited.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)
    argvs = _spy_git_argvs(monkeypatch)

    reads: list[object] = []
    real_read_index = git_native.read_index
    monkeypatch.setattr(
        git_native,
        "read_index",
        lambda repo_arg, **kw: (reads.append(kw), real_read_index(repo_arg, **kw))[1],
    )

    result = git_native.commit_scoped(["edited.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert [a for a in argvs if "--force-remove" in a] == []
    assert [r for r in reads if r.get("fresh")] == [], (
        "bound 7's verification re-read must sit inside its own `if`, never "
        "on the ordinary-commit path"
    )


def test_a_reconcile_that_does_not_take_is_reported_not_swallowed(tmp_path, monkeypatch):
    """The fail-loud half. The reconciliation is best-effort AS TO THE
    COMMIT -- the CAS has already landed it and a refresh failure must never
    be returned as a commit failure -- but it must not be silent: a peer
    holding `.git/index.lock` would otherwise leave the resurrection live
    with nothing said about it. Simulated by neutering the one
    `--force-remove` call, which is exactly what a declined write looks like
    from this function's side.
    """
    repo = _archival_deletion_repo(tmp_path)
    msg_file = _write_msg(tmp_path, "remove gone.txt\n")

    real_git_fn = git_native._git

    def _decline_force_remove(args, **kwargs):
        if "--force-remove" in args:
            return git_native.GitResult(returncode=0, stdout="", stderr="")
        return real_git_fn(args, **kwargs)

    monkeypatch.setattr(git_native, "_git", _decline_force_remove)

    result = git_native.commit_scoped(["gone.txt"], msg_file, repo)

    assert result.ok, "a reconciliation that did not take must not fail the landed commit"
    assert "gone.txt" in result.stderr
    assert "still staged in .git/index" in result.stderr
    assert "git update-index --force-remove -- gone.txt" in result.stderr

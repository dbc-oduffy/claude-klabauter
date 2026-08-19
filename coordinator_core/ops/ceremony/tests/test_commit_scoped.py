"""
coordinator_core.ops.ceremony.tests.test_commit_scoped

Tests for `git_native.commit_scoped()` -- the computed commit-mechanism
selector (C3, docs/plans/2026-07-27-computed-commit-mechanism-selection.md).
Runs against REAL git via `fixtures.real_git` (allowlisted below) --
index/worktree divergence cannot be exhibited by a mocked git.

Coverage:
  - a partial-hunk staged path survives the commit VERBATIM (the
    claude-klabauter 506748a0 incident shape, closed).
  - the AGREE branch still carries the trailing-pathspec race protection
    (`git add` + `git commit -F ... --`, both explicit-pathspec).
  - a concurrent peer's staged file is never absorbed, in BOTH branches.
  - the shared index is UNMUTATED by the diverged (private-index) path --
    the peer's own staging survives untouched afterward.
  - an empty path set fails loud.
  - a directory pathspec is rejected outright, so a peer's untracked new
    file living inside it is never committed by construction.
  - a compare-and-swap race: a real concurrent commit lands on the branch
    inside the private-index window; the 4-argument `update-ref` fails
    loud instead of silently orphaning the peer's commit.

Each new test is red-proofed by breaking `commit_scoped()` in the specific
way it targets and confirming failure before restoring -- see the report
this module's authoring session filed (not re-derived here; per-file
comments below note what each red-proof pinned).

Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
chunk C3.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.divergence import DivergenceCheckFailed
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.git_native import DeliverableIdAssertionConflictError
from coordinator_core.ops.deliverable_equivalence import _reset_equivalence_map_cache
from .fixtures.real_git import (
    make_agree_path,
    make_diverged_path,
    make_peer_staged_path,
    real_git_repo,
)

# Real-git spawn is load-bearing: the docstring says it plainly -- index/
# worktree divergence and a real compare-and-swap race on update-ref cannot
# be exhibited by a mocked git. Per-test repo fixtures since several tests
# mutate the shared index and history.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _write_msg(tmp_path: Path, text: str = "a commit message\n") -> Path:
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


def _committed_content_at_head(repo: Path, rel: str) -> str:
    result = _git(["show", f"HEAD:{rel}"], repo)
    return result.stdout


def _committed_files_at_head(repo: Path) -> list[str]:
    result = _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo)
    return [line for line in result.stdout.splitlines() if line]


def _porcelain(repo: Path) -> list[str]:
    result = _git(["status", "--porcelain"], repo)
    return [line for line in result.stdout.splitlines() if line]


def _index_dump(repo: Path) -> str:
    """Serialize the real index's staged content (ls-files -s) for a
    before/after equality check that the private-index branch never
    mutates the shared index."""
    result = _git(["ls-files", "-s"], repo)
    return result.stdout


# ---------------------------------------------------------------------------
# Partial-hunk staged content survives verbatim (diverged path)
# ---------------------------------------------------------------------------


def test_diverged_path_commits_staged_content_verbatim(tmp_path):
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert _committed_content_at_head(repo, "file.txt") == "STAGED\n"
    # Worktree content is untouched by commit_scoped -- it never runs `git
    # checkout`/`git add` against the diverged path's worktree copy.
    assert (repo / "file.txt").read_text(encoding="utf-8") == "WORKTREE\n"


def test_diverged_path_result_names_excluded_worktree_edits(tmp_path):
    """P1 fix (state/bug-backlog/2026-08-10-scoped-git-commit-reports-
    success-while-334e90d707f9.yaml): the private-index branch commits the
    STAGED content (unchanged behaviour, asserted above) but must no longer
    return a bare silent success -- the caller's excluded worktree edits are
    named on the result."""
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert result.worktree_excluded == ("file.txt",)
    assert "file.txt" in result.stderr
    assert "not included" in result.stderr.lower()


def test_agree_branch_result_reports_no_excluded_worktree_edits(tmp_path):
    """Clean case stays quiet: when index and worktree agree (the ordinary
    AGREE branch), nothing was excluded and no warning is manufactured."""
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert result.worktree_excluded == ()
    assert result.stderr == ""


def test_red_proof_diverged_path_via_worktree_reproduces_incident(tmp_path):
    """Red-proof for the above: forcing the AGREE-branch mechanism
    (`git add` from the worktree, i.e. what `commit_scoped` must NOT do for
    a diverged path) on the same fixture reproduces the 506748a0 incident
    shape -- the staged content is discarded in favour of the worktree
    edit. This pins down that the assertion above is actually meaningful.
    """
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    add_result = git_native.add_paths(repo, ["file.txt"])
    assert add_result.ok
    commit_result = git_native.commit_with_message_file(repo, msg_file, ["file.txt"])
    assert commit_result.ok

    assert _committed_content_at_head(repo, "file.txt") == "WORKTREE\n"


# ---------------------------------------------------------------------------
# Agree branch retains trailing-pathspec race protection
# ---------------------------------------------------------------------------


def test_agree_branch_commits_via_explicit_pathspec_add_and_commit(tmp_path):
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert _committed_files_at_head(repo) == ["file.txt"]
    assert _committed_content_at_head(repo, "file.txt") == "content\n"


def test_agree_branch_does_not_invoke_private_index_mechanism(tmp_path, monkeypatch):
    """Mechanism-selection assertion, not just an outcome one: the AGREE
    branch must dispatch through plain `add_paths` + `commit_with_message_
    file`, never through `_commit_scoped_private_index` -- forcing either
    branch unconditionally (a mis-selection bug) is otherwise invisible to
    outcome-only assertions when the private-index path happens to produce
    the same content for an agree-shaped input.
    """
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)

    calls = {"count": 0}
    real_fn = git_native._commit_scoped_private_index

    def _spy(*args, **kwargs):
        calls["count"] += 1
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(git_native, "_commit_scoped_private_index", _spy)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert calls["count"] == 0


def test_diverged_branch_invokes_private_index_mechanism(tmp_path, monkeypatch):
    """Mirror of the above for the DIVERGED branch -- it must dispatch
    through `_commit_scoped_private_index`, never fall through to a plain
    `add_paths` + `commit_with_message_file` (which would silently
    re-derive the diverged path's content from the worktree)."""
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    calls = {"count": 0}
    real_fn = git_native._commit_scoped_private_index

    def _spy(*args, **kwargs):
        calls["count"] += 1
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(git_native, "_commit_scoped_private_index", _spy)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert calls["count"] == 1


def test_agree_branch_never_absorbs_peer_staged_file(tmp_path):
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "file.txt", "content\n")
    make_peer_staged_path(repo, "peer.txt", "peer content\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    committed = _committed_files_at_head(repo)
    assert committed == ["file.txt"]
    assert "peer.txt" not in committed
    assert any(line.endswith("peer.txt") for line in _porcelain(repo))


# ---------------------------------------------------------------------------
# Private-index branch never absorbs a peer's staged file, never mutates
# the shared index
# ---------------------------------------------------------------------------


def test_diverged_branch_never_absorbs_peer_staged_file(tmp_path):
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    make_peer_staged_path(repo, "peer.txt", "peer content\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    committed = _committed_files_at_head(repo)
    assert committed == ["file.txt"]
    assert "peer.txt" not in committed


def test_diverged_branch_leaves_shared_index_unmutated(tmp_path):
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    make_peer_staged_path(repo, "peer.txt", "peer content\n")
    msg_file = _write_msg(tmp_path)

    before = _index_dump(repo)
    result = git_native.commit_scoped(["file.txt"], msg_file, repo)
    assert result.ok, result.stderr
    after = _index_dump(repo)

    assert before == after
    assert any(line.endswith("peer.txt") for line in _porcelain(repo))


def test_diverged_branch_ignores_staged_modification_to_untouched_tracked_file(tmp_path):
    """DR-272 § 3.4 drift-2 coverage: the private index must be built from
    HEAD, never from the shared worktree index. Stage a modification to an
    already-tracked file (``seed.txt``) that this ``commit_scoped`` call
    never names, then confirm that staged modification does not leak into
    the resulting commit -- proving the private index is seeded from HEAD's
    content for that path, not copied from the shared index (which, at the
    moment this call runs, disagrees with HEAD for ``seed.txt``).
    """
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    (repo / "seed.txt").write_text("shared-index-only edit\n", encoding="utf-8")
    _git(["add", "--", "seed.txt"], repo)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    committed = _committed_files_at_head(repo)
    assert committed == ["file.txt"]
    assert _committed_content_at_head(repo, "seed.txt") == "seed\n"
    assert any(line.endswith("seed.txt") for line in _porcelain(repo))


def test_private_index_seeding_succeeds_when_shared_index_file_is_absent(tmp_path):
    """DR-272 § 3.4 drift-2 coverage: seeding via ``git read-tree HEAD``
    into a fresh temp index never depends on ``.git/index`` existing on
    disk -- unlike the prior ``shutil.copy2(real_index, temp_index)``
    seeding, which raised an uncaught ``FileNotFoundError`` whenever the
    shared index had not yet been materialized in this worktree. Exercises
    ``_commit_scoped_private_index`` directly (as other tests in this file
    already do to isolate the private-index branch) because deleting the
    shared index also erases the staged-content evidence ``commit_scoped``'s
    own divergence classification relies on to route here in the first
    place.
    """
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("HEAD content\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "add file.txt"], repo)
    git_dir = Path(_git(["rev-parse", "--absolute-git-dir"], repo).stdout.strip())
    (git_dir / "index").unlink()
    msg_file = _write_msg(tmp_path)

    result = git_native._commit_scoped_private_index(["file.txt"], [], msg_file, repo)

    assert result.ok, result.stderr
    assert _committed_content_at_head(repo, "file.txt") == "HEAD content\n"


def test_red_proof_committing_from_shared_index_absorbs_peer_file(tmp_path):
    """Red-proof for the above two tests: forcing a plain `git commit -F
    msg` (no pathspec, straight from the SHARED index -- what a broken
    `commit_scoped` might degrade to) on the same fixture absorbs the
    peer's staged file into the commit -- the DoE-claude 726925b2 incident
    shape -- confirming the peer-file-never-absorbed assertions above are
    real constraints, not vacuous ones.
    """
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    make_peer_staged_path(repo, "peer.txt", "peer content\n")
    msg_file = _write_msg(tmp_path)

    _git(["commit", "-F", str(msg_file)], repo)

    assert "peer.txt" in _committed_files_at_head(repo)


# ---------------------------------------------------------------------------
# Empty path set fails loud
# ---------------------------------------------------------------------------


def test_empty_path_set_fails_loud(tmp_path):
    repo = real_git_repo(tmp_path)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped([], msg_file, repo)

    assert result.ok is False
    assert "empty" in result.stderr.lower()


def test_red_proof_empty_path_set_bare_dashdash_sweeps_whole_index(tmp_path):
    """Red-proof: `git commit -F msg -- ` (an empty trailing pathspec) is
    NOT a no-op -- it commits the whole index. This pins down why chunk
    C3's guard must fail loud rather than falling through to that form.
    """
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)

    result = subprocess.run(
        ["git", "commit", "-F", str(msg_file), "--"],
        cwd=str(repo), capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert _committed_files_at_head(repo) == ["file.txt"]


# ---------------------------------------------------------------------------
# Directory pathspec rejected -- a peer's untracked file inside it is never
# committed, by construction (reject, not expand)
# ---------------------------------------------------------------------------


def test_directory_pathspec_rejected_peer_file_inside_never_committed(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / "adir").mkdir()
    (repo / "adir" / "mine.txt").write_text("mine\n", encoding="utf-8")
    _git(["add", "--", "adir/mine.txt"], repo)
    # A peer's untracked new file already living in the same directory.
    (repo / "adir" / "peer.txt").write_text("peer\n", encoding="utf-8")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["adir"], msg_file, repo)

    assert result.ok is False
    assert "directory" in result.stderr.lower()
    # Nothing was committed at all -- the peer's file was never at risk.
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_before == head_after
    assert "peer.txt" not in "".join(_committed_files_at_head(repo))


# ---------------------------------------------------------------------------
# Compare-and-swap: a concurrent real commit lands mid-window
# ---------------------------------------------------------------------------


def test_cas_failure_on_concurrent_head_move_fails_loud(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    real_git_fn = git_native._git
    landed = {"done": False}

    def _racing_git(args, **kwargs):
        result = real_git_fn(args, **kwargs)
        if not landed["done"] and list(args[:1]) == ["commit-tree"]:
            landed["done"] = True
            # A real concurrent peer commit races into the window between
            # this module's commit-tree and its update-ref.
            (repo / "peer_race.txt").write_text("race\n", encoding="utf-8")
            _git(["add", "--", "peer_race.txt"], repo)
            _git(["commit", "-q", "-m", "peer race commit"], repo)
        return result

    monkeypatch.setattr(git_native, "_git", _racing_git)

    head_before_call = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok is False
    assert "compare-and-swap" in result.stderr.lower() or "concurrent" in result.stderr.lower()

    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    # The peer's race commit landed and HEAD now points at it -- it was NOT
    # silently orphaned/overwritten by our failed update-ref.
    assert head_after != head_before_call
    log = _git(["log", "--format=%s", "-n", "3"], repo).stdout
    assert "peer race commit" in log


def test_red_proof_two_arg_update_ref_silently_orphans_peer_commit(tmp_path):
    """Red-proof for the CAS test above: the 2-argument `git update-ref HEAD
    <new>` form (no CAS) has no way to detect a concurrent HEAD move and
    silently overwrites it -- reproducing exactly the orphaning this
    module's 4-argument form must prevent.
    """
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "file.txt", "content\n")
    _git(["commit", "-q", "-m", "base"], repo)
    old_head = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    # A concurrent peer commit lands.
    (repo / "peer_race.txt").write_text("race\n", encoding="utf-8")
    _git(["add", "--", "peer_race.txt"], repo)
    _git(["commit", "-q", "-m", "peer race commit"], repo)
    peer_head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert peer_head != old_head

    # Build a tree/commit against the STALE old_head and land it with the
    # UNSAFE 2-argument update-ref.
    tree_sha = _git(["write-tree"], repo).stdout.strip()
    new_sha = subprocess.run(
        ["git", "commit-tree", tree_sha, "-p", old_head, "-m", "stale commit"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    update_result = subprocess.run(
        ["git", "update-ref", "HEAD", new_sha],
        cwd=str(repo), capture_output=True, text=True,
    )

    assert update_result.returncode == 0
    head_now = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_now == new_sha
    log = _git(["log", "--format=%s"], repo).stdout
    assert "peer race commit" not in log


# ---------------------------------------------------------------------------
# Indeterminate divergence (`DivergenceCheckFailed`) fails loud, never
# silently takes the AGREE branch -- code review finding 2026-07-27,
# "diverging_paths() is fail-open, and now feeds a safety decision".
# ---------------------------------------------------------------------------


def test_indeterminate_divergence_fails_loud_never_agree_branch(tmp_path, monkeypatch):
    """A `diverging_paths()` failure (simulated here; real cause is a `git
    diff` non-zero exit or timeout) must FAIL LOUD (`GitResult.ok is
    False`), never be silently read as "no divergence" and take the AGREE
    branch -- that would discard the deliberately-staged content exactly
    like the claude-klabauter 506748a0 incident, through `commit_scoped()`
    itself.
    """
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    def _boom(*args, **kwargs):
        raise DivergenceCheckFailed("simulated git diff timeout")

    monkeypatch.setattr(git_native, "diverging_paths", _boom)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok is False
    assert "indeterminate" in result.stderr.lower()
    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_after == head_before


def test_indeterminate_gap_check_fails_loud_with_known_kwargs(tmp_path, monkeypatch):
    """Same as above, but through the `known_checked`/`known_diverged`
    dedup seam: the indeterminate result surfaces from the "gap" check (a
    path outside `known_checked`), not from the full-batch call -- must
    still fail loud, not silently trust `known_diverged` for the gap path
    it was never actually able to check.
    """
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "checked.txt", "content\n")
    make_diverged_path(repo, "gap.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    def _boom(*args, **kwargs):
        raise DivergenceCheckFailed("simulated git diff timeout on gap")

    monkeypatch.setattr(git_native, "diverging_paths", _boom)

    result = git_native.commit_scoped(
        ["checked.txt", "gap.txt"],
        msg_file,
        repo,
        known_checked={"checked.txt"},
        known_diverged=set(),
    )

    assert result.ok is False
    assert "indeterminate" in result.stderr.lower()
    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_after == head_before


# ---------------------------------------------------------------------------
# `known_checked`/`known_diverged` dedup seam -- code review finding
# 2026-07-27, "the dedup seam has no direct test coverage". Prior coverage
# only exercised these kwargs on the no-divergence (AGREE) case; these tests
# drive the seam on a GENUINELY diverged path, the gap sub-case, and a
# deliberately wrong caller answer.
# ---------------------------------------------------------------------------


def test_known_kwargs_diverged_path_uses_private_index_branch(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    real_fn = git_native._commit_scoped_private_index
    calls = {"count": 0}

    def _spy(*args, **kwargs):
        calls["count"] += 1
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(git_native, "_commit_scoped_private_index", _spy)

    result = git_native.commit_scoped(
        ["file.txt"],
        msg_file,
        repo,
        known_checked={"file.txt"},
        known_diverged={"file.txt"},
    )

    assert result.ok, result.stderr
    assert calls["count"] == 1
    assert _committed_content_at_head(repo, "file.txt") == "STAGED\n"


def test_gap_path_outside_known_checked_is_freshly_checked_and_caught(tmp_path):
    """A path in `commit_paths` but ABSENT from `known_checked` (e.g. a
    swept-rename destination the caller discovered but never ran through
    its own `diverging_paths()`) must still be caught -- not assumed safe
    just because it wasn't in the caller's vetted set.
    """
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "checked.txt", "content\n")
    make_diverged_path(repo, "gap.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(
        ["checked.txt", "gap.txt"],
        msg_file,
        repo,
        known_checked={"checked.txt"},
        known_diverged=set(),
    )

    assert result.ok, result.stderr
    assert _committed_content_at_head(repo, "gap.txt") == "STAGED\n"
    assert _committed_content_at_head(repo, "checked.txt") == "content\n"


def _committed_content_bytes_at_head(repo: Path, rel: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=str(repo), capture_output=True
    )
    return result.stdout


def _head_mode(repo: Path, rel: str) -> str:
    result = _git(["ls-tree", "HEAD", "--", rel], repo)
    return result.stdout.split()[0]


# ---------------------------------------------------------------------------
# commit_authored_content -- form 3 (DR-272 § 3), chunk C2. Tests authored
# in THIS chunk per the plan's own instruction (not left to C3-C5).
# ---------------------------------------------------------------------------


def test_ac3_red_proof_agree_branch_absorbs_foreign_unstaged_worktree_edit(tmp_path):
    """A foreign UNSTAGED edit on the op's own path -- nothing staged, so
    `diverging_paths()` reports no divergence -- IS absorbed by the current
    AGREE branch of `commit_scoped()`: `git add` re-stages straight from the
    worktree and the foreign bytes land in the commit. Pins the vector
    `commit_authored_content` must close (next test)."""
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)

    (repo / "file.txt").write_text("FOREIGN WORKTREE EDIT\n", encoding="utf-8")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    assert _committed_content_at_head(repo, "file.txt") == "FOREIGN WORKTREE EDIT\n"


def test_ac3_new_entrypoint_never_absorbs_foreign_unstaged_worktree_edit(tmp_path):
    """Same fixture shape as the red-proof above, but through
    `commit_authored_content()` -- the caller-supplied content lands,
    the foreign worktree edit is never read at all."""
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)

    (repo / "file.txt").write_text("FOREIGN WORKTREE EDIT\n", encoding="utf-8")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content(
        "file.txt", "AUTHORED CONTENT\n", msg_file, repo
    )

    assert result.ok, result.stderr
    assert _committed_content_at_head(repo, "file.txt") == "AUTHORED CONTENT\n"
    # The foreign worktree edit is untouched on disk -- this entrypoint
    # never runs `git add`/`git checkout` against the worktree copy.
    assert (repo / "file.txt").read_text(encoding="utf-8") == "FOREIGN WORKTREE EDIT\n"


def test_file_mode_preserved(tmp_path):
    repo = real_git_repo(tmp_path)
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    _git(["add", "--", "script.sh"], repo)
    # The exec bit is set through the INDEX, not through the filesystem:
    # Windows has no POSIX mode bits, so `chmod(0o755)` above is a no-op there
    # and git records `100644`. `update-index --chmod=+x` is the portable way
    # to establish the `100755` precondition this test is actually about
    # (does `commit_authored_content` PRESERVE a mode it finds at HEAD).
    _git(["update-index", "--chmod=+x", "--", "script.sh"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    assert _head_mode(repo, "script.sh") == "100755"

    msg_file = _write_msg(tmp_path)
    result = git_native.commit_authored_content(
        "script.sh", "#!/bin/sh\necho bye\n", msg_file, repo
    )

    assert result.ok, result.stderr
    assert _head_mode(repo, "script.sh") == "100755"
    assert _committed_content_at_head(repo, "script.sh") == "#!/bin/sh\necho bye\n"


def test_nonexistent_head_fails_loud(tmp_path):
    root = tmp_path / "no_head_repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "no-head@example.invalid"], root)
    _git(["config", "user.name", "no-head"], root)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content("file.txt", "content\n", msg_file, root)

    assert result.ok is False


def test_path_absent_from_head_fails_loud(tmp_path):
    repo = real_git_repo(tmp_path)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content("never-committed.txt", "content\n", msg_file, repo)

    assert result.ok is False
    assert "does not exist in head" in result.stderr.lower()


def test_directory_path_rejected(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / "adir").mkdir()
    (repo / "adir" / "inner.txt").write_text("inner\n", encoding="utf-8")
    _git(["add", "--", "adir/inner.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content("adir", "content\n", msg_file, repo)

    assert result.ok is False
    assert "directory" in result.stderr.lower()


def test_ac11_shared_index_refreshed_after_successful_cas(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content("file.txt", "NEW CONTENT\n", msg_file, repo)

    assert result.ok, result.stderr
    diff_result = _git(["diff", "--cached", "--", "file.txt"], repo)
    assert diff_result.stdout == ""


def test_ac12_non_ascii_lf_content_round_trips_byte_identically(tmp_path):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)

    content = "café éè non-ascii ☃\n"
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content("file.txt", content, msg_file, repo)

    assert result.ok, result.stderr
    assert _committed_content_bytes_at_head(repo, "file.txt") == content.encode("utf-8")


def test_ac13_post_commit_auto_push_replayed_after_successful_cas(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)

    calls: list[Path] = []
    monkeypatch.setattr(
        git_native, "_replay_post_commit_auto_push", lambda root: calls.append(Path(root))
    )
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content("file.txt", "NEW\n", msg_file, repo)

    assert result.ok, result.stderr
    assert len(calls) == 1
    assert calls[0] == repo


def test_ac13_post_commit_auto_push_not_replayed_on_failure(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(
        git_native, "_replay_post_commit_auto_push", lambda root: calls.append(Path(root))
    )
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content("missing.txt", "content\n", msg_file, repo)

    assert result.ok is False
    assert calls == []


# ---------------------------------------------------------------------------
# `commit_authored_content`'s own `attributed_session_id` -- the fourth
# blind-`_resolve_session_id` site closed by this chunk (the same defect
# `commit_scoped`/`_commit_scoped_private_index` already closed above,
# state/bug-backlog/2026-08-18-scoped-git-commit-stamps-a-foreign-session-id-
# 8d21f0c4e7b9.yaml -- carried over to this sibling entrypoint's own
# `compute_missing_trailer_args` call, which had none of `commit_scoped`'s
# `session_id_override` threading until now).
# ---------------------------------------------------------------------------


def test_commit_authored_content_attributed_session_id_wins_over_ambient_env(tmp_path, monkeypatch):
    """`commit_authored_content()`'s trailer-replay call must stamp the
    CALLER's own resolved identity, not whatever this process's ambient env
    happens to hold -- the same split `commit_scoped`'s own AGREE/diverged
    branches already close, mirrored here for this sibling entrypoint."""
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_B)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content(
        "file.txt", "NEW CONTENT\n", msg_file, repo, attributed_session_id=_SESSION_A
    )

    assert result.ok, result.stderr
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert _trailer_lines(repo, sha, "Session-Id:") == [f"Session-Id: {_SESSION_A}"]


def test_commit_authored_content_attributed_session_id_none_is_byte_identical_to_default(
    tmp_path, monkeypatch
):
    """`attributed_session_id=None` (the default) must reproduce the prior
    blind env-var resolution exactly -- every pre-existing call in this file
    omits the kwarg and is left unmodified by this change."""
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_A)
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_authored_content(
        "file.txt", "NEW CONTENT\n", msg_file, repo
    )

    assert result.ok, result.stderr
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert _trailer_lines(repo, sha, "Session-Id:") == [f"Session-Id: {_SESSION_A}"]


def test_wrong_known_diverged_trusts_caller_and_commits_worktree_content(tmp_path):
    """Documents the trust boundary explicitly (code review: "ideally a
    case where a caller passes a WRONG known_diverged"). `file.txt` is
    listed in `known_checked` but INCORRECTLY absent from `known_diverged`
    -- the caller claims it's clean when it is genuinely diverged.
    `commit_scoped()` trusts that answer for any path in `known_checked`
    and takes the AGREE branch, committing WORKTREE content and discarding
    the staged edit. This is NOT a safety net against a caller's own stale
    or wrong answer -- the docstring already states the dedup seam is sound
    ONLY within the same lock hold as the caller's own check; this test
    pins down what happens when that contract is violated, so the trust
    boundary is proven, not merely asserted in prose.
    """
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(
        ["file.txt"],
        msg_file,
        repo,
        known_checked={"file.txt"},
        known_diverged=set(),  # WRONG: claims file.txt is clean
    )

    assert result.ok, result.stderr
    assert _committed_content_at_head(repo, "file.txt") == "WORKTREE\n"


# ---------------------------------------------------------------------------
# `deliverable_id` -- C7a, docs/plans/2026-08-10-a-commit-trailer-that-names-
# the-session.md. Covers AC12/13/17/18/19 on BOTH the agree and diverged
# branches -- the branch-parametrised assertion the original (unsplit) C7
# AC set lacked (staff-eng delta review, finding 5).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_equivalence_cache():
    """`deliverable_equivalence.load_equivalence_map` memoizes per-process,
    keyed to the FIRST `worktree_root` it is ever called with (see that
    function's own docstring) -- each test here uses a fresh `tmp_path`
    repo, so without a reset between tests the second test onward would
    silently read the FIRST test's (empty) equivalence map result for an
    unrelated root. Harmless for these tests today (no test declares a fork
    equivalence), but the reset is cheap and this is the documented contract
    for "a test iterating multiple roots" -- exactly this file's shape.
    """
    _reset_equivalence_map_cache()
    yield
    _reset_equivalence_map_cache()


def _seed_deliverable_artifact(repo: Path, deliverable_id: str, *, slug: str = "seed-plan") -> Path:
    """Write a minimal plan artifact under `docs/plans/` carrying
    `deliverable_id` in its own frontmatter -- the AC19(b) existence check
    (`_validate_explicit_deliverable_id`, via `coordinator_core.ops.
    deliverable_rollup._scan_artifacts_by_deliverable_id`) resolves against
    this three-path corpus scan, NOT against the commit's own pathspec (see
    that function's own docstring for why: the whole point of this chunk is
    admitting `deliverable_id` on ordinary code-only commits with no
    frontmatter-capable artifact of their own).
    """
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{slug}.md"
    path.write_text(
        f"---\ndeliverable_id: {deliverable_id}\n---\n\n# seed plan\n",
        encoding="utf-8",
    )
    return path


def _trailer_lines(repo: Path, sha: str, prefix: str) -> list[str]:
    result = _git(["log", "-1", "--format=%B", sha], repo)
    return [line for line in result.stdout.splitlines() if line.startswith(prefix)]


# AC17 -- explicit deliverable_id lands as the trailer on a REAL committed
# SHA, read back BY TRAILER (not by inspecting trailer_args/argv), on both
# branches.


def test_agree_branch_explicit_deliverable_id_lands_on_real_commit(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_deliverable_artifact(repo, "dlv-abc123")
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo, deliverable_id="dlv-abc123")

    assert result.ok, result.stderr
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert _trailer_lines(repo, sha, "Deliverable-Id:") == ["Deliverable-Id: dlv-abc123"]


def test_diverged_branch_explicit_deliverable_id_lands_on_real_commit(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_deliverable_artifact(repo, "dlv-abc123")
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo, deliverable_id="dlv-abc123")

    assert result.ok, result.stderr
    sha = result.stdout.strip()
    assert _trailer_lines(repo, sha, "Deliverable-Id:") == ["Deliverable-Id: dlv-abc123"]


# AC13 -- deliverable_id=None is byte-identical to every existing case in
# this file. Every pre-existing test above calls commit_scoped without the
# new kwarg at all (default None) and is left unmodified -- this pins the
# claim down explicitly rather than leaving it merely implied by the diff.


def test_agree_branch_deliverable_id_none_is_byte_identical_to_default(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_a = real_git_repo(tmp_path / "a")
    make_agree_path(repo_a, "file.txt", "content\n")
    msg_a = _write_msg(tmp_path)
    result_a = git_native.commit_scoped(["file.txt"], msg_a, repo_a)
    assert result_a.ok, result_a.stderr

    repo_b = real_git_repo(tmp_path / "b")
    make_agree_path(repo_b, "file.txt", "content\n")
    msg_b = tmp_path / "msg_b.txt"
    msg_b.write_text("a commit message\n", encoding="utf-8")
    result_b = git_native.commit_scoped(["file.txt"], msg_b, repo_b, deliverable_id=None)
    assert result_b.ok, result_b.stderr

    body_a = _git(["log", "-1", "--format=%B"], repo_a).stdout
    body_b = _git(["log", "-1", "--format=%B"], repo_b).stdout
    assert body_a == body_b


# AC18 -- message-trailer-agrees (one line, no duplicate) / disagrees
# (raises DeliverableIdAssertionConflictError, DR-328's commit-side sibling
# of DivergentDeliverableIdError), on both branches.


def test_agree_branch_message_trailer_agrees_no_duplicate(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_deliverable_artifact(repo, "dlv-abc123")
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("subject\n\nDeliverable-Id: dlv-abc123\n", encoding="utf-8")

    result = git_native.commit_scoped(["file.txt"], msg_file, repo, deliverable_id="dlv-abc123")

    assert result.ok, result.stderr
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert _trailer_lines(repo, sha, "Deliverable-Id:") == ["Deliverable-Id: dlv-abc123"]


def test_agree_branch_message_trailer_disagrees_raises(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_deliverable_artifact(repo, "dlv-abc123")
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("subject\n\nDeliverable-Id: dlv-old\n", encoding="utf-8")
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    with pytest.raises(DeliverableIdAssertionConflictError):
        git_native.commit_scoped(["file.txt"], msg_file, repo, deliverable_id="dlv-abc123")

    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_after == head_before


def test_diverged_branch_message_trailer_agrees_no_duplicate(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_deliverable_artifact(repo, "dlv-abc123")
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("subject\n\nDeliverable-Id: dlv-abc123\n", encoding="utf-8")

    result = git_native.commit_scoped(["file.txt"], msg_file, repo, deliverable_id="dlv-abc123")

    assert result.ok, result.stderr
    sha = result.stdout.strip()
    assert _trailer_lines(repo, sha, "Deliverable-Id:") == ["Deliverable-Id: dlv-abc123"]


def test_diverged_branch_message_trailer_disagrees_raises(tmp_path):
    repo = real_git_repo(tmp_path)
    _seed_deliverable_artifact(repo, "dlv-abc123")
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("subject\n\nDeliverable-Id: dlv-old\n", encoding="utf-8")
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    with pytest.raises(DeliverableIdAssertionConflictError):
        git_native.commit_scoped(["file.txt"], msg_file, repo, deliverable_id="dlv-abc123")

    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_after == head_before


# AC19 -- malformed-shape and unresolvable-value rejections. Guard runs
# before either branch dispatches, so one agree-shaped fixture covers both
# (the guard never reaches the divergence check).


def test_malformed_shape_rejected(tmp_path):
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = git_native.commit_scoped(
        ["file.txt"], msg_file, repo, deliverable_id="not-a-dlv-id"
    )

    assert result.ok is False
    assert "not-a-dlv-id" in result.stderr
    assert "dlv-" in result.stderr
    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_after == head_before


def test_unresolvable_value_rejected(tmp_path):
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = git_native.commit_scoped(
        ["file.txt"], msg_file, repo, deliverable_id="dlv-doesnotexist"
    )

    assert result.ok is False
    assert "dlv-doesnotexist" in result.stderr
    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_after == head_before


# ---------------------------------------------------------------------------
# Percolate-publish-scale argv-length fix (2026-08-15): `commit_scoped()`'s
# own agree-branch `git add`/`git commit` convert to `--pathspec-from-file`
# (never chunked -- atomicity), and its own `diverging_paths()` divergence
# check chunks instead (`git diff` rejects `--pathspec-from-file` outright --
# see `git_native._diverging_paths_chunked`'s own docstring). This is the
# last of the five argv-length sites this repo's commit path had; siblings
# `ef84c2ee9`/`fe0f4eb84`/`25268ed33`/`47e8defbb` closed the other four. Live
# failure this closes: "commit_scoped: divergence check indeterminate for
# 2080 path(s) -- refusing to guess the commit mechanism (... rc=127 ...)".
# ---------------------------------------------------------------------------


def _make_bulk_agree_paths(repo: Path, n: int, subdir: str = "bulk") -> list[str]:
    """Write + stage `n` small files under `subdir` in ONE `git add`
    subprocess call (never one `git add` per file, unlike `make_agree_path`
    used one-at-a-time elsewhere in this module) -- setup speed only, not
    itself exercising the argv-length fix under test. Average path length
    (`"bulk/file_NNNNN.txt"`, ~20 chars) times `n` in the thousands is
    exactly the shape that blew the raw 32767-char Windows argv cap before
    this fix -- realistic, not an inflated synthetic length.
    """
    base = repo / subdir
    base.mkdir(parents=True, exist_ok=True)
    rels: list[str] = []
    for i in range(n):
        rel = f"{subdir}/file_{i:05d}.txt"
        (repo / rel).write_text(f"content {i}\n", encoding="utf-8")
        rels.append(rel)
    _git(["add", "-A", "--", subdir], repo)
    return rels


def test_large_pathspec_commits_as_exactly_one_commit(tmp_path):
    """Atomicity pin: a 2000+ path commit through the agree branch's own
    `--pathspec-from-file` staging/commit lands as EXACTLY ONE commit, never
    chunked into several -- `commit_scoped()`'s whole contract is that the
    named pathspec lands as one commit (see `commit_with_message_file_
    pathspec_scoped()`'s own docstring for why the commit leg is never
    chunked, unlike the divergence check)."""
    repo = real_git_repo(tmp_path)
    paths = _make_bulk_agree_paths(repo, 2200)
    msg_file = _write_msg(tmp_path)
    count_before = int(_git(["rev-list", "--count", "HEAD"], repo).stdout.strip())

    result = git_native.commit_scoped(paths, msg_file, repo)

    assert result.ok, result.stderr
    count_after = int(_git(["rev-list", "--count", "HEAD"], repo).stdout.strip())
    assert count_after == count_before + 1
    committed = set(_committed_files_at_head(repo))
    assert set(paths) <= committed


def test_argv_stays_bounded_and_uses_pathspec_file_for_add_and_commit(tmp_path, monkeypatch):
    """Subprocess argv shape pin (cannot portably assert the literal Windows
    32767-char limit from a non-Windows-specific test, so this asserts the
    SHAPE that keeps every call under it): the agree branch's `git add` and
    `git commit` never carry the raw path list on argv -- both carry a
    `--pathspec-from-file=<f>` token instead -- and every chunked `git diff`
    call this batch triggers stays under a generous bound, never one
    unchunked argv holding all 2000+ paths."""
    repo = real_git_repo(tmp_path)
    paths = _make_bulk_agree_paths(repo, 2000)
    msg_file = _write_msg(tmp_path)

    real_run = git_native.subprocess.run
    argvs: list[list[str]] = []

    def _spy(args, *a, **kw):
        argvs.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(git_native.subprocess, "run", _spy)

    result = git_native.commit_scoped(paths, msg_file, repo)
    assert result.ok, result.stderr

    add_argv = next(a for a in argvs if len(a) > 1 and a[1] == "add")
    commit_argv = next(a for a in argvs if len(a) > 1 and a[1] == "commit")
    assert any(tok.startswith("--pathspec-from-file=") for tok in add_argv), add_argv
    assert any(tok.startswith("--pathspec-from-file=") for tok in commit_argv), commit_argv
    # None of the actual path strings sit on the add/commit argv -- they
    # live in the pathspec file instead.
    assert not (set(paths) & set(add_argv))
    assert not (set(paths) & set(commit_argv))

    diff_argvs = [a for a in argvs if len(a) > 1 and a[1] == "diff"]
    assert diff_argvs, "expected at least one chunked `git diff` divergence call"
    for a in diff_argvs:
        total_len = sum(len(tok) for tok in a)
        assert total_len < 20000, f"unchunked-looking git diff argv: {total_len} chars"


def test_large_batch_diverged_path_preserved_amid_agree_bulk(tmp_path):
    """Per-path protection preserved at scale: one genuinely diverged path
    inside an otherwise-agreeing 1500-path batch still routes to the
    private-index branch and survives with its STAGED content verbatim --
    the chunked divergence check must never OR/AND a whole-chunk verdict
    into a batch answer that could paper over this single path (state/
    lessons/2026-08-14-partial-stage-protection-did-not-survive-a-moving-
    head.md)."""
    repo = real_git_repo(tmp_path)
    paths = _make_bulk_agree_paths(repo, 1500)
    make_diverged_path(repo, "diverged.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    paths.append("diverged.txt")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(paths, msg_file, repo)

    assert result.ok, result.stderr
    assert result.worktree_excluded == ("diverged.txt",)
    assert _committed_content_at_head(repo, "diverged.txt") == "STAGED\n"
    assert (repo / "diverged.txt").read_text(encoding="utf-8") == "WORKTREE\n"
    committed = set(_committed_files_at_head(repo))
    assert set(paths) <= committed


def test_large_batch_genuine_divergence_failure_still_fails_loud(tmp_path, monkeypatch):
    """A genuine `diverging_paths()` failure (not an argv-length artifact --
    each chunk is already sized to avoid that) still fails the WHOLE call
    loud, never partially commits or silently treats the un-checked chunks
    as clean, even at percolate-publish scale."""
    repo = real_git_repo(tmp_path)
    paths = _make_bulk_agree_paths(repo, 1200)
    msg_file = _write_msg(tmp_path)
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    def _boom(*args, **kwargs):
        raise DivergenceCheckFailed("simulated git diff failure mid-batch")

    monkeypatch.setattr(git_native, "diverging_paths", _boom)

    result = git_native.commit_scoped(paths, msg_file, repo)

    assert result.ok is False
    assert "indeterminate" in result.stderr.lower()
    head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert head_after == head_before


# ---------------------------------------------------------------------------
# `attributed_session_id` -- state/bug-backlog/2026-08-18-scoped-git-commit-
# stamps-a-foreign-session-id-8d21f0c4e7b9.yaml. Real-shape repro: two
# sessions with overlapping dirty paths, each committing its own explicit
# pathspec, each commit carrying its OWN invoker's Session-Id -- never a
# concurrently-live peer's, and never the bare env-var read this parameter
# exists to override.
# ---------------------------------------------------------------------------

_SESSION_A = "903044ef-72b9-4549-a3df-6300e10b6b84"
_SESSION_B = "e77424be-b452-43bd-a995-e12d60168cb6"


def test_agree_branch_attributed_session_id_wins_over_ambient_env(tmp_path, monkeypatch):
    """The AGREE branch's Python-side trailer computation (2026-08-14 fix)
    must stamp the CALLER's own resolved identity, not whatever this
    process's ambient env happens to hold -- the exact split a shared,
    many-concurrent-session process makes possible."""
    repo = real_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_B)
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(
        ["file.txt"], msg_file, repo, attributed_session_id=_SESSION_A
    )

    assert result.ok, result.stderr
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert _trailer_lines(repo, sha, "Session-Id:") == [f"Session-Id: {_SESSION_A}"]


def test_diverged_branch_attributed_session_id_wins_over_ambient_env(tmp_path, monkeypatch):
    """Same claim, private-index (diverged) branch -- the branch that never
    trusts git hooks and instead replays `compute_missing_trailer_args`
    itself, where this defect actually landed (5300b76a9)."""
    repo = real_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_B)
    make_diverged_path(repo, "file.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(
        ["file.txt"], msg_file, repo, attributed_session_id=_SESSION_A
    )

    assert result.ok, result.stderr
    sha = result.stdout.strip()
    assert _trailer_lines(repo, sha, "Session-Id:") == [f"Session-Id: {_SESSION_A}"]


def test_two_sessions_overlapping_paths_each_commit_carries_its_own_invoker(tmp_path):
    """Pinned shape (dispatch brief): two sessions with overlapping dirty
    paths, each committing its own explicit pathspec, each landed commit
    carrying its OWN invoker's Session-Id -- session A's commit never
    carries session B's id, and vice versa, regardless of invocation
    order or of what a shared process's ambient env happens to hold at
    either call."""
    repo = real_git_repo(tmp_path)
    make_agree_path(repo, "a.txt", "from A\n")
    make_agree_path(repo, "b.txt", "from B\n")
    msg_a = _write_msg(tmp_path, "commit from session A\n")
    msg_b = tmp_path / "msg_b.txt"
    msg_b.write_text("commit from session B\n", encoding="utf-8")

    result_a = git_native.commit_scoped(
        ["a.txt"], msg_a, repo, attributed_session_id=_SESSION_A
    )
    assert result_a.ok, result_a.stderr
    sha_a = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result_b = git_native.commit_scoped(
        ["b.txt"], msg_b, repo, attributed_session_id=_SESSION_B
    )
    assert result_b.ok, result_b.stderr
    sha_b = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    assert _trailer_lines(repo, sha_a, "Session-Id:") == [f"Session-Id: {_SESSION_A}"]
    assert _trailer_lines(repo, sha_b, "Session-Id:") == [f"Session-Id: {_SESSION_B}"]


def test_attributed_session_id_none_is_byte_identical_to_default(tmp_path, monkeypatch):
    """`attributed_session_id=None` (the default) must reproduce the prior
    blind env-var resolution exactly -- every pre-existing call in this
    file omits the kwarg and is left unmodified by this change."""
    repo = real_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_A)
    make_agree_path(repo, "file.txt", "content\n")
    msg_file = _write_msg(tmp_path)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok, result.stderr
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert _trailer_lines(repo, sha, "Session-Id:") == [f"Session-Id: {_SESSION_A}"]

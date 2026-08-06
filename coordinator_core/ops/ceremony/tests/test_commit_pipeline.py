"""
coordinator_core.ops.ceremony.tests.test_commit_pipeline

Tests for commit_pipeline.py -- the native stage -> commit -> push critical
section integrating C1 (git_native), C2 (commit_message), and C3
(commit_gates); the C4 chunk of the `wsc_tail` rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Coverage (parity-oracle assertions, per the deleted
`tests/wsc-asic/test-wsc-commit-parity.sh` recovered from
`example-doctrine-repo:85006468^:coordinator/tests/wsc-asic/test-wsc-commit-parity.sh`),
reproduced in an isolated temp-repo fixture mirroring the deleted parity
test's own temp-repo seeding:
  (b) the final commit's tree == the explicit `commit_paths` pathspec --
      nothing outside the pathspec is absorbed.
  (d) a concurrent sibling's OWN already-staged file (outside this
      pipeline's pathspec) is neither committed NOR lost -- it remains
      staged after the pipeline's commit lands.

Plus native additions beyond the recovered oracle (unit-level contract on
the ported seams this chunk introduces):
  explicit_stage classification -- to_stage / swept-rename / swept-delete /
      missing / missing-caller.
  push_with_retry -- no-remote skip (`exit_code == 0`, `skipped=["push:no-
      remote"]`) since these fixtures never configure a remote.
  gate short-circuit -- a failing C3 gate prevents any commit (commit=None,
      commit_failed=True).

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C4 (AC5).
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.commit_pipeline as commit_pipeline_mod
from coordinator_core.git.commit_trailers import compute_missing_trailer_args
from coordinator_core.ops.ceremony.commit_pipeline import (
    StageOutcome,
    commit,
    derive_pushed_tristate,
    explicit_stage,
    run_commit_pipeline,
)


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _committed_files_at_head(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _porcelain(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def _staged_blob(repo: Path, rel_path: str) -> str:
    """The CURRENTLY STAGED content of `rel_path`, read via `git show :rel_path`
    -- never the worktree content."""
    result = subprocess.run(
        ["git", "show", f":{rel_path}"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return result.stdout


def _commit_message_at_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%B", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# run_commit_pipeline -- assertion (b): commit tree == explicit pathspec
# ---------------------------------------------------------------------------


def test_pipeline_assertion_b_commit_tree_matches_explicit_pathspec(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert _committed_files_at_head(repo) == ["tasks/feature/todo.md"]
    # Push skipped (no remote configured in this fixture) -- tri-state None.
    assert result.pushed is None


# ---------------------------------------------------------------------------
# run_commit_pipeline -- a pathspec naming both the deleted source AND the
# created destination of a move must commit that move (2026-08-06 fix, live
# incident: commit `64acc1254`, a move-set of changelog/review-trail files
# into an archive directory, refused with "Deleted-claim NOT staged for
# deletion" for every moved path -- see `commit_gates.
# _parse_name_status_rename_sources`'s own docstring for the root cause).
# ---------------------------------------------------------------------------


def test_pipeline_move_set_commits_end_to_end(tmp_path):
    repo = _init_repo(tmp_path)
    content = "content block\n" * 40  # long/repetitive enough to trip git's rename detector
    srcs = [f"week-changelog/f{i}.md" for i in range(4)]
    dsts = [f"archive/week-changelog/f{i}.md" for i in range(4)]
    for src in srcs:
        _seed_file(repo, src, content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    for src, dst in zip(srcs, dsts):
        src_path = repo / src
        src_path.unlink()
        dst_path = repo / dst
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_text(content, encoding="utf-8")

    all_paths = srcs + dsts
    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="archive changelog files",
        stage_paths=all_paths,
        caller_paths=set(all_paths),
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert set(_committed_files_at_head(repo)) == set(dsts)
    for src in srcs:
        assert not (repo / src).exists()


# ---------------------------------------------------------------------------
# run_commit_pipeline -- assertion (d): sibling staged file not absorbed
# ---------------------------------------------------------------------------


def test_pipeline_assertion_d_sibling_staged_file_not_absorbed(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # A concurrent sibling session's own staged file, entirely outside this
    # pipeline's pathspec.
    _seed_file(repo, "sibling/scratch.md", "sibling content")
    _git(["add", "--", "sibling/scratch.md"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None

    committed = _committed_files_at_head(repo)
    assert committed == ["tasks/feature/todo.md"]
    assert "sibling/scratch.md" not in committed

    # The sibling's staged file is neither committed nor lost -- still staged.
    status_lines = _porcelain(repo)
    assert any(
        line.startswith("A") and line.endswith("sibling/scratch.md")
        for line in status_lines
    )


def test_pipeline_assertion_d_sibling_staged_deletion_not_absorbed(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "sibling/gone.md", "sibling content")
    _git(["add", "--", "README.md", "sibling/gone.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # Sibling session stages its own deletion, outside this pipeline's scope.
    _git(["rm", "-q", "sibling/gone.md"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    committed = _committed_files_at_head(repo)
    assert committed == ["tasks/feature/todo.md"]

    # The sibling's own staged deletion remains staged (not swept into this
    # commit, not resurrected).
    status_lines = _porcelain(repo)
    assert any(
        line.startswith("D") and line.endswith("sibling/gone.md")
        for line in status_lines
    )


# ---------------------------------------------------------------------------
# Gate short-circuit -- a failing C3 gate prevents any commit
# ---------------------------------------------------------------------------


def test_pipeline_dirty_tree_gate_peer_path_outside_scope_does_not_block(tmp_path):
    """2026-07-22 regression: an unattributable dirty path OUTSIDE the
    caller's own `gate_paths` (a live peer session's file on a shared
    branch -- the routine case, not the exception) must NOT trip the gate.
    Prior to the `gate_paths` scoping fix, this was the exact shape that
    made `ceremony.wsc_tail`'s commit gesture unusable on a shared branch
    (~33 peer paths reported, none inside the caller's own stage_paths).
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # A peer session's in-flight file -- unattributable in absolute terms,
    # but outside this caller's own pathspec.
    _seed_file(repo, "peer-session-file.txt", "peer's in-flight work")
    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert result.dirty_gate is not None
    assert result.dirty_gate.passed is True
    assert _committed_files_at_head(repo) == ["tasks/feature/todo.md"]

    # The peer's file is neither committed nor disturbed -- still untracked.
    status_lines = _porcelain(repo)
    assert any(line.endswith("peer-session-file.txt") for line in status_lines)


def test_pipeline_empty_pathspec_with_peer_file_is_benign_noop_not_blocked(tmp_path):
    """THE regression test for the original 2026-07-22 incident, and for the
    2026-07-22 sentinel correction that followed it. A caller with nothing
    of its own to stage or delete (empty `stage_paths` + empty
    `deleted_paths`, e.g. a `/workstream-complete` invocation with no local
    changes on a shared branch) must get a benign no-op, NEVER
    `commit_failed`, regardless of how many unattributable peer files sit
    in the tree. `explicit_stage([], None)` reports no failure (`stage.
    failed == []`) so the pipeline does not early-return at the staging
    step; `gate_paths` then computes to `[]` -- under the FIRST (rejected)
    cut of this fix, an empty `gate_paths` meant "unfiltered", which would
    have run `dirty_tree_gate` over the whole tree and reproduced the
    incident exactly. The corrected fix short-circuits on `commit_paths ==
    []` BEFORE either gate runs at all.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # An unattributable peer file, no owning handoff, no stage_paths/
    # deleted_paths naming it or anything else.
    _seed_file(repo, "peer-session-file.txt", "peer's in-flight work")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: no-op",
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.commit is None
    assert result.committed_sha is None
    assert result.deletion_gate is None
    assert result.dirty_gate is None


def test_pipeline_dirty_tree_gate_failure_short_circuits_before_commit(tmp_path):
    """An unattributable path INSIDE the caller's own `gate_paths` must
    still trip the gate -- scoping narrows the gate's business, it does not
    blind it to the caller's own inconsistent state. Modeled via a
    `deleted_paths` claim for a tracked file the caller removed from disk
    but never staged for deletion: `gate_paths` (compute_gate_paths) folds
    in `deleted_paths` regardless of stage.staged_paths, so this also
    exercises the pipeline wiring picking up more than just the staged set.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "tasks/feature/stale.md", "stale content")
    _git(["add", "--", "README.md", "tasks/feature/stale.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "tasks" / "feature" / "stale.md").unlink()  # deleted on disk, never staged
    _seed_file(repo, "tasks/feature/todo.md", "content")

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        deleted_paths=["tasks/feature/stale.md"],
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md", "tasks/feature/stale.md"},
    )

    assert result.commit_failed is True
    assert result.commit is None
    assert result.push is None
    assert result.committed_sha is None
    assert result.dirty_gate is not None
    assert result.dirty_gate.passed is False
    assert "tasks/feature/stale.md" in result.dirty_gate.unattributable

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == head_before


# ---------------------------------------------------------------------------
# explicit_stage -- native unit additions
# ---------------------------------------------------------------------------


def test_explicit_stage_no_paths_is_benign_noop(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    outcome = explicit_stage(repo, [])
    assert outcome == StageOutcome(exit_code=0, skipped=["stage:no-paths-provided"])


def test_explicit_stage_stages_existing_path(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "new/file.md", "content")
    outcome = explicit_stage(repo, ["new/file.md"], caller_paths={"new/file.md"})
    assert outcome.exit_code == 0
    assert outcome.staged_paths == ["new/file.md"]
    assert outcome.missing_caller_paths == []


def test_explicit_stage_swept_rename_forwarded(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "old-name.md", "renamed content")
    _git(["add", "--", "old-name.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _git(["mv", "old-name.md", "new-name.md"], repo)

    outcome = explicit_stage(repo, ["old-name.md"], caller_paths={"old-name.md"})
    assert outcome.exit_code == 0
    assert outcome.swept_renames == [("old-name.md", "new-name.md")]
    assert outcome.missing_caller_paths == []
    assert any(s.startswith("swept:old-name.md->new-name.md") for s in outcome.skipped)


def test_explicit_stage_swept_deleted_caller_path_is_included_not_missing(tmp_path):
    """2026-08-04 fix (defect B, live incident -- a `git rm`-staged deletion
    named in the caller's own pathspec was reported `empty-commit-set`
    rather than committed). Before the fix, this branch was misclassified
    identically to a PEER's own unrelated staged deletion (a swept, benign,
    excluded-from-`staged_paths` shape) purely because the deletion happened
    to already be staged -- even though the caller named this exact path.
    The caller's own already-staged deletion belongs in the commit set
    without a fresh `git add` (nothing left to add), exactly like a diverged
    path belongs without being re-added.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "caller-owned.md", "content")
    _git(["add", "--", "caller-owned.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _git(["rm", "-q", "caller-owned.md"], repo)

    outcome = explicit_stage(repo, ["caller-owned.md"], caller_paths={"caller-owned.md"})
    assert outcome.exit_code == 0
    assert outcome.missing_caller_paths == []
    assert outcome.staged_paths == ["caller-owned.md"]
    assert outcome.deletion_paths == ["caller-owned.md"]
    assert "already-staged-deleted:caller-owned.md" in outcome.skipped
    # Already staged -- this call issues no fresh `git add` for it.
    assert "caller-owned.md" not in outcome.acted


def test_explicit_stage_swept_deleted_generated_path_benign(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "generated.md", "content")
    _git(["add", "--", "generated.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _git(["rm", "-q", "generated.md"], repo)

    outcome = explicit_stage(repo, ["generated.md"])
    assert outcome.exit_code == 0
    assert outcome.missing_caller_paths == []
    assert "swept-deleted:generated.md" in outcome.skipped


def test_explicit_stage_genuinely_missing_caller_path_escalates(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    outcome = explicit_stage(repo, ["never/existed.md"], caller_paths={"never/existed.md"})
    assert outcome.exit_code == 2
    assert outcome.missing_caller_paths == ["never/existed.md"]
    assert "missing-caller:never/existed.md" in outcome.skipped


def test_explicit_stage_genuinely_missing_generated_path_benign(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    outcome = explicit_stage(repo, ["never/existed.md"])
    assert outcome.exit_code == 0
    assert outcome.missing_caller_paths == []
    assert "missing:never/existed.md" in outcome.skipped


# ---------------------------------------------------------------------------
# explicit_stage -- pipe-in-rename-path fallback (code-reviewer 2026-07-08
# Finding 6; ported from the retired `test_wsc_commit.py` onto this live
# `explicit_stage()` seam -- see `commit_pipeline.py`'s own docstring for
# the identical "|" fallback branch this now exercises directly).
# ---------------------------------------------------------------------------


def _commit_via_index(root: Path, rel_path: str, content: str = "data\n") -> None:
    """Commit *content* at *rel_path* via git plumbing, without ever creating
    *rel_path* as a real file on disk.

    NTFS forbids '|' (and other reserved characters) in filenames, and git's
    own `core.protectNTFS` guard rejects such paths by default on Windows even
    at the index level -- the guard exists to stop working-tree checkouts from
    silently colliding on case/reserved-char variants, which is irrelevant
    here since this path is never checked out. Disabling it locally lets the
    test exercise a genuinely '|'-containing git path (as can occur via a
    cross-platform-authored history) without requiring an illegal Windows
    filename to exist on disk.
    """
    _git(["config", "core.protectNTFS", "false"], root)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(root), input=content, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{rel_path}"], root)
    _git(["commit", "-m", f"add {rel_path}"], root)


def _stage_rename_via_index(root: Path, old_rel_path: str, new_rel_path: str, content: str = "data\n") -> None:
    """Stage a rename from *old_rel_path* to *new_rel_path* without requiring
    *old_rel_path* to exist as a real file on disk (see `_commit_via_index`).

    Removes *old_rel_path* from the index (`git rm --cached`, index-only --
    no working-tree file required) and stages *new_rel_path* as a real file
    with matching content, so `git diff --cached --find-renames` detects the
    pair as an R-classified rename exactly as a real `git mv` would.
    """
    _git(["rm", "--cached", "--", old_rel_path], root)
    _seed_file(root, new_rel_path, content)
    _git(["add", "--", new_rel_path], root)


def test_explicit_stage_pipe_in_rename_path_not_forwarded(tmp_path):
    """A '|'-containing rename path is NOT forwarded as a swept_rename pair.

    Review: code-reviewer 2026-07-08 Finding 6 -- f"{old}|{new}" is ambiguous
    to the example-doctrine-repo script's pipe-split '--swept-rename' parser when either side
    contains a literal '|'.  A path with '|' must fall back to
    skip-classification (missing:<p>) rather than being forwarded as a
    malformed flag, and the fallback must be logged.
    """
    repo = _init_repo(tmp_path)
    weird_name = "inbox/wei|rd.md"
    _commit_via_index(repo, weird_name, "memo content\n")
    _seed_file(repo, "real.md", "real content\n")
    _git(["add", "--", "real.md"], repo)
    _git(["commit", "-q", "-m", "seed real.md"], repo)

    _stage_rename_via_index(repo, weird_name, "archive/weird.md", "memo content\n")
    _seed_file(repo, "real.md", "updated real\n")

    outcome = explicit_stage(repo, [weird_name, "real.md"])

    assert outcome.exit_code == 0, f"expected exit_code 0; got {outcome}"
    assert outcome.swept_renames == [], (
        f"'|'-containing rename path must NOT be forwarded as a swept_rename pair; "
        f"got {outcome.swept_renames}"
    )
    missing_entries = [s for s in outcome.skipped if weird_name in s]
    assert len(missing_entries) == 1, (
        f"'|'-containing path should fall back to missing-classification skip; "
        f"got skipped={outcome.skipped}"
    )
    assert missing_entries[0].startswith("missing:"), (
        f"expected a 'missing:' skip entry; got {missing_entries[0]!r}"
    )


def test_explicit_stage_pipe_rename_escalated_when_caller_supplied(tmp_path):
    """A '|'-containing swept-rename path IS escalated when caller-supplied.

    Guards Finding 6's pipe-fallback branch on the caller-named side. The
    ambiguous pair is still never forwarded as a `--swept-rename` value (that
    is Finding 6's actual requirement, covered by the sibling test above) --
    but a path the CALLER named, which this call then declines to commit, must
    surface in `missing_caller_paths` rather than being absorbed into the
    benign swept bucket.

    Supersedes the pre-`927a68fdd` expectation, which asserted a bare
    "missing:" / exit_code=0 here. That was the latent bug fixed by
    `927a68fdd` ("a dropped path is never silent"): a caller's own path fell
    out of the decline report purely because it also collided with a
    '|'-containing rename, so the ceremony reported success while silently not
    committing what the caller asked for.
    """
    repo = _init_repo(tmp_path)
    weird_name = "inbox/wei|rd.md"
    _commit_via_index(repo, weird_name, "memo content\n")
    _seed_file(repo, "real.md", "real content\n")
    _git(["add", "--", "real.md"], repo)
    _git(["commit", "-q", "-m", "seed real.md"], repo)

    _stage_rename_via_index(repo, weird_name, "archive/weird.md", "memo content\n")
    _seed_file(repo, "real.md", "updated real\n")

    outcome = explicit_stage(repo, [weird_name, "real.md"], caller_paths={weird_name})

    assert outcome.exit_code == 2, (
        f"a caller-named path this call declines to commit must escalate; got {outcome}"
    )
    assert outcome.missing_caller_paths == [weird_name], (
        f"the caller's own dropped path must be reported, not silently absorbed "
        f"into the swept bucket; got {outcome.missing_caller_paths}"
    )
    entries = [s for s in outcome.skipped if weird_name in s]
    assert len(entries) == 1, f"expected a skip entry; got {outcome.skipped}"
    assert entries[0].startswith("missing-caller:"), (
        f"expected a 'missing-caller:' skip entry; got {entries[0]!r}"
    )
    assert outcome.swept_renames == [], (
        f"Finding 6 still holds: the ambiguous pair must never be forwarded as a "
        f"swept_rename; got {outcome.swept_renames}"
    )


# ---------------------------------------------------------------------------
# explicit_stage -- divergence-aware (C4 "second job")
# ---------------------------------------------------------------------------


def test_explicit_stage_diverged_path_preserved_not_readded(tmp_path):
    """A path with deliberate partial-hunk staging (staged != worktree, and
    staged != HEAD) is left OUT of the `git add` batch -- its staged content
    survives `explicit_stage()` verbatim, never overwritten by the newer
    worktree content sitting on top of it."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "docs/notes.md", "seed\n")
    _git(["add", "--", "docs/notes.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "docs/notes.md", "STAGED HUNK\n")
    _git(["add", "--", "docs/notes.md"], repo)
    _seed_file(repo, "docs/notes.md", "LATER EDIT\n")  # further, unstaged, worktree edit

    outcome = explicit_stage(repo, ["docs/notes.md"], caller_paths={"docs/notes.md"})

    assert outcome.exit_code == 0
    assert outcome.staged_paths == ["docs/notes.md"]
    assert "docs/notes.md" not in outcome.acted
    assert "diverged:docs/notes.md" in outcome.skipped
    assert _staged_blob(repo, "docs/notes.md") == "STAGED HUNK\n"
    assert (repo / "docs/notes.md").read_text(encoding="utf-8") == "LATER EDIT\n"


def test_explicit_stage_red_proof_without_divergence_check_clobbers(tmp_path, monkeypatch):
    """Red-proof for the test above: with `diverging_paths()` forced to
    report no divergence (the pre-fix behaviour, which never consulted it at
    all), the deliberately-staged hunk IS clobbered by the unconditional
    `git add` -- proving the assertion above is a real regression guard."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "docs/notes.md", "seed\n")
    _git(["add", "--", "docs/notes.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "docs/notes.md", "STAGED HUNK\n")
    _git(["add", "--", "docs/notes.md"], repo)
    _seed_file(repo, "docs/notes.md", "LATER EDIT\n")

    monkeypatch.setattr(
        commit_pipeline_mod, "diverging_paths",
        lambda paths, cwd=None, timeout=2.0, fail_loud=False: [],
    )

    outcome = explicit_stage(repo, ["docs/notes.md"], caller_paths={"docs/notes.md"})

    assert outcome.exit_code == 0
    assert outcome.acted == ["docs/notes.md"]
    assert _staged_blob(repo, "docs/notes.md") == "LATER EDIT\n"


def test_explicit_stage_mixed_diverged_and_safe_paths(tmp_path):
    """A realistic mixed batch: one diverged path (preserved verbatim,
    skipped from `git add`) alongside one non-diverged path (staged
    normally) in the SAME call."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "docs/diverged.md", "seed\n")
    _seed_file(repo, "docs/safe.md", "seed\n")
    _git(["add", "--", "docs/diverged.md", "docs/safe.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "docs/diverged.md", "STAGED\n")
    _git(["add", "--", "docs/diverged.md"], repo)
    _seed_file(repo, "docs/diverged.md", "EDITED\n")
    _seed_file(repo, "docs/safe.md", "safe content\n")

    outcome = explicit_stage(
        repo, ["docs/diverged.md", "docs/safe.md"],
        caller_paths={"docs/diverged.md", "docs/safe.md"},
    )

    assert outcome.exit_code == 0
    assert set(outcome.staged_paths) == {"docs/diverged.md", "docs/safe.md"}
    assert outcome.acted == ["docs/safe.md"]
    assert _staged_blob(repo, "docs/diverged.md") == "STAGED\n"
    assert _staged_blob(repo, "docs/safe.md") == "safe content\n"


def test_explicit_stage_indeterminate_divergence_fails_loud_never_readds(tmp_path, monkeypatch):
    """Code review finding 2026-07-27: `explicit_stage()`'s own `git add`
    decision reads the identical `diverging_paths()` predicate
    `git_native.commit_scoped()` uses to pick the commit mechanism, one
    layer up. An indeterminate result (`git diff` failure/timeout) must
    fail this call loud (`StageOutcome.exit_code != 0`, populated
    `failed`), never be silently read as "nothing diverged" and re-add a
    genuinely diverged path -- that would destroy deliberately-staged
    content before `commit_scoped()` downstream ever gets a chance to
    observe and preserve it. `explicit_stage()`'s own negative-spec says it
    never raises, so the exception must be caught internally."""
    from coordinator_core.git.divergence import DivergenceCheckFailed

    repo = _init_repo(tmp_path)
    _seed_file(repo, "docs/notes.md", "seed\n")
    _git(["add", "--", "docs/notes.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "docs/notes.md", "STAGED HUNK\n")
    _git(["add", "--", "docs/notes.md"], repo)
    _seed_file(repo, "docs/notes.md", "LATER EDIT\n")

    def _boom(paths, cwd=None, timeout=2.0, fail_loud=False):
        raise DivergenceCheckFailed("simulated git diff timeout")

    monkeypatch.setattr(commit_pipeline_mod, "diverging_paths", _boom)

    outcome = explicit_stage(repo, ["docs/notes.md"], caller_paths={"docs/notes.md"})

    assert outcome.exit_code != 0
    assert outcome.failed
    assert "indeterminate" in outcome.failed[0].lower()
    # Nothing was staged/re-added -- the deliberately-staged hunk is exactly
    # as it was before this call.
    assert _staged_blob(repo, "docs/notes.md") == "STAGED HUNK\n"


# ---------------------------------------------------------------------------
# commit() -- routed through git_native.commit_scoped() (C4)
# ---------------------------------------------------------------------------


def test_commit_diverged_path_lands_via_private_index_branch(tmp_path):
    """`commit()` now delegates the mechanism choice to `commit_scoped()`
    (C3); on a diverged `commit_paths` set it must still land a commit whose
    tree carries the DELIBERATELY-STAGED content, never the worktree
    content on top of it."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "docs/diverged.md", "seed\n")
    _git(["add", "--", "docs/diverged.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "docs/diverged.md", "STAGED HUNK\n")
    _git(["add", "--", "docs/diverged.md"], repo)
    _seed_file(repo, "docs/diverged.md", "LATER EDIT\n")

    outcome = commit(repo, message="chore: land diverged hunk\n", commit_paths=["docs/diverged.md"])

    assert outcome.exit_code == 0
    assert outcome.committed_sha is not None
    committed_blob = subprocess.run(
        ["git", "show", "HEAD:docs/diverged.md"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert committed_blob == "STAGED HUNK\n"
    # Worktree is untouched by the commit mechanism either way.
    assert (repo / "docs/diverged.md").read_text(encoding="utf-8") == "LATER EDIT\n"


def test_commit_trailer_output_byte_identical_across_agree_and_diverged_branches(tmp_path, monkeypatch):
    """AC7: `commit_scoped()`'s private-index branch replays the
    `prepare-commit-msg` hook's Session-Id/Deliverable-Id resolution via
    `compute_missing_trailer_args()` + `interpret-trailers --in-place`
    (AC18, landed in `git_native.py`'s `_commit_scoped_private_index` --
    this chunk does not re-implement it, see this chunk's own "do not
    over-claim" scope note). The agree branch instead relies on the git
    HOOK to run that same replay -- a real hook is not installed in this
    throwaway test repo, so this test performs the hook's job explicitly
    (mirroring what `coordinator/bin/coordinator-prepare-commit-msg` does
    in a real repo) using the SAME shared `compute_missing_trailer_args()`
    function, then asserts the two branches land byte-identical trailer
    output -- proving the shared-function replay is what actually delivers
    AC7's parity, not an assumption."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "22222222-2222-2222-2222-222222222222")

    message = (
        "chore: land trailers\n"
        "\n"
        "Nature: chore\n"
        "Plan: docs/plans/example.md\n"
        "Plan-Id: pln-00000000000000000000000000000000\n"
    )

    (tmp_path / "agree").mkdir()
    (tmp_path / "diverged").mkdir()
    agree_repo = _init_repo(tmp_path / "agree")
    _seed_file(agree_repo, "docs/agree.md", "seed\n")
    _git(["add", "--", "docs/agree.md"], agree_repo)
    _git(["commit", "-q", "-m", "seed"], agree_repo)
    _seed_file(agree_repo, "docs/agree.md", "agree content\n")

    # Simulate the prepare-commit-msg hook the agree branch's real `git
    # commit -F` would run in a production repo: write the message to a
    # temp file, replay the hook's trailer resolution, then commit with
    # THAT file's (possibly hook-amended) content.
    hook_msg_file = tmp_path / "agree-hook-msg.txt"
    hook_msg_file.write_text(message, encoding="utf-8")
    trailer_args = compute_missing_trailer_args(hook_msg_file, agree_repo)
    if trailer_args:
        subprocess.run(
            ["git", "interpret-trailers", "--in-place", *trailer_args, str(hook_msg_file)],
            cwd=str(agree_repo), check=True, capture_output=True, text=True,
        )
    agree_message = hook_msg_file.read_text(encoding="utf-8")
    agree_outcome = commit(agree_repo, message=agree_message, commit_paths=["docs/agree.md"])

    diverged_repo = _init_repo(tmp_path / "diverged")
    _seed_file(diverged_repo, "docs/diverged.md", "seed\n")
    _git(["add", "--", "docs/diverged.md"], diverged_repo)
    _git(["commit", "-q", "-m", "seed"], diverged_repo)
    _seed_file(diverged_repo, "docs/diverged.md", "STAGED HUNK\n")
    _git(["add", "--", "docs/diverged.md"], diverged_repo)
    _seed_file(diverged_repo, "docs/diverged.md", "LATER EDIT\n")
    # No manual hook replay here -- `commit_scoped()`'s private-index branch
    # does this itself (AC18), which is exactly what this test verifies.
    diverged_outcome = commit(diverged_repo, message=message, commit_paths=["docs/diverged.md"])

    assert agree_outcome.exit_code == 0
    assert diverged_outcome.exit_code == 0
    agree_head_message = _commit_message_at_head(agree_repo)
    diverged_head_message = _commit_message_at_head(diverged_repo)
    assert agree_head_message == diverged_head_message
    assert "Session-Id: 22222222-2222-2222-2222-222222222222" in agree_head_message
    assert "Plan-Id: pln-00000000000000000000000000000000" in agree_head_message


# ---------------------------------------------------------------------------
# derive_pushed_tristate
# ---------------------------------------------------------------------------


def test_derive_pushed_tristate_false_when_push_never_attempted():
    assert derive_pushed_tristate(None) is False


def test_pipeline_no_op_when_nothing_stageable(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: nothing to do",
        stage_paths=["never/existed.md"],
    )
    assert result.commit_failed is False
    assert result.commit is None
    assert result.committed_sha is None


# ---------------------------------------------------------------------------
# Post-stage rollback -- session fb5fa766, 2026-07-31 incident: every
# post-stage failure exit previously returned without unstaging, leaving
# `explicit_stage()`'s own `git add` residue sitting at index state `A ` for
# the next bare `git commit` on a shared branch to absorb.
# ---------------------------------------------------------------------------


def test_pipeline_gate_failure_leaves_a_clean_index(tmp_path):
    """A dirty-tree gate failure must roll back exactly what THIS call staged
    -- the caller's own file must return to `??` (untracked), never sit
    staged (`A `) after a failed pipeline run."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "tasks/feature/stale.md", "stale content")
    _git(["add", "--", "README.md", "tasks/feature/stale.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "tasks" / "feature" / "stale.md").unlink()  # deleted on disk, never staged
    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        deleted_paths=["tasks/feature/stale.md"],
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md", "tasks/feature/stale.md"},
    )

    assert result.commit_failed is True
    status_lines = _porcelain(repo)
    assert any(line.strip() == "?? tasks/feature/todo.md" for line in status_lines)
    assert not any(
        line.startswith("A") and line.endswith("todo.md") for line in status_lines
    )


def test_pipeline_gate_failure_rollback_never_touches_a_sibling_staged_path(tmp_path):
    """The scoped rollback must be limited to `stage.acted` -- a concurrent
    sibling EM's own already-staged file, outside this call's pathspec,
    must survive a gate-failure rollback untouched."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "tasks/feature/stale.md", "stale content")
    _git(["add", "--", "README.md", "tasks/feature/stale.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "tasks" / "feature" / "stale.md").unlink()
    _seed_file(repo, "tasks/feature/todo.md", "content")

    # A sibling EM's own staged file, entirely outside this pipeline's scope.
    _seed_file(repo, "sibling/scratch.md", "sibling content")
    _git(["add", "--", "sibling/scratch.md"], repo)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        deleted_paths=["tasks/feature/stale.md"],
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md", "tasks/feature/stale.md"},
    )

    assert result.commit_failed is True
    status_lines = _porcelain(repo)
    assert any(
        line.startswith("A") and line.endswith("sibling/scratch.md") for line in status_lines
    )


def test_pipeline_commit_subprocess_failure_leaves_a_clean_index(tmp_path, monkeypatch):
    """A genuine post-stage `git commit` failure (simulated here via a
    monkeypatched `commit_scoped()` -- real staging still runs, only the
    commit MECHANISM is faked) must also roll back this call's own staged
    residue, and must never touch a sibling's own staged file."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "tasks/feature/anchor.md", "anchor content")
    _git(["add", "--", "README.md", "tasks/feature/anchor.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    # A sibling EM's own staged file, entirely outside this pipeline's scope.
    _seed_file(repo, "sibling/scratch.md", "sibling content")
    _git(["add", "--", "sibling/scratch.md"], repo)

    from coordinator_core.ops.ceremony.git_native import GitResult

    monkeypatch.setattr(
        commit_pipeline_mod.git_native,
        "commit_scoped",
        lambda *a, **kw: GitResult(returncode=1, stdout="", stderr="simulated commit failure"),
    )

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is True
    assert result.committed_sha is None
    status_lines = _porcelain(repo)
    assert any(line.strip() == "?? tasks/feature/todo.md" for line in status_lines)
    assert not any(
        line.startswith("A") and line.endswith("todo.md") for line in status_lines
    )
    assert any(
        line.startswith("A") and line.endswith("sibling/scratch.md") for line in status_lines
    )


def test_pipeline_empty_commit_set_noop_rolls_back_quietly(tmp_path):
    """`git commit` exits 1 on an empty commit set -- the ordinary
    already-committed no-op (unchanged content re-staged then committed
    again) arrives through the SAME `commit_outcome.exit_code != 0` path as
    a genuine failure. The rollback must stay quiet: `commit_outcome.stderr`
    must remain exactly `exit_code=1` (never decorated with rollback noise
    -- see `test_scoped_git_commit_cli.py::TestRefusalReporting`'s loud-vs-
    quiet rendering contract) and the index must end up clean."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "tasks/feature/todo.md", "content")
    _git(["add", "--", "README.md", "tasks/feature/todo.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    # No further edits -- todo.md on disk is byte-identical to HEAD.

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: already committed",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is True
    assert result.committed_sha is None
    assert result.commit is not None
    assert result.commit.stderr == "exit_code=1"
    assert result.diagnostics == ["exit_code=1"]
    assert _porcelain(repo) == []


def test_stage_add_paths_partial_failure_residue_is_reconciled_into_acted(
    tmp_path, monkeypatch
):
    """code-reviewer Finding 1 (fa1aeeeb9187 review), fixed 2026-07-31:
    `explicit_stage()`'s `StageOutcome.acted` used to default to `[]` on
    ANY `git add` subprocess failure, which only stayed safe if `git add --
    a b` were atomic on failure -- i.e. never partially staged `a` before
    erroring on `b`. It is NOT atomic in general (a mixed batch CAN stage
    some paths before erroring on another). Originally reproduced via a
    `.gitignore`-blocked path in the batch -- that specific TRIGGER is now
    pre-filtered out of `to_stage` entirely by the 2026-08-03 ignored-path
    fix (see `explicit_stage`'s own docstring), so this test now drives the
    same non-atomicity via a monkeypatched `git_native.add_paths` that
    genuinely stages the first path before reporting a failure for the
    whole call -- the reconciliation mechanism itself (not its original
    trigger) is what this test covers. `explicit_stage()` reconciles its
    failure-branch `acted` against real index state scoped to its own
    `to_stage` batch (`git diff --cached --name-only -- <to_stage>`) instead
    of assuming `[]`, so this residue is now visible to a caller's rollback
    bookkeeping (e.g. `run_commit_pipeline`'s `finally`, scoped to
    `stage.acted`) rather than silently surviving as staged-and-abandoned.
    `explicit_stage()` itself never unstages on failure; reconciliation
    reports, it does not roll back (rollback is the caller's job, driven by
    this reconciled `acted`).
    """
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "normal.md", "normal content")
    _seed_file(repo, "other.md", "other content")

    real_add_paths = git_native.add_paths

    def _fake_add_paths(cwd, paths):
        # Genuinely stage the first path (mirroring the non-atomic partial
        # batch shape), then report a failure for the whole call.
        real_add_paths(cwd, [paths[0]])
        return GitResult(returncode=1, stdout="", stderr="simulated non-atomic add failure")

    monkeypatch.setattr(git_native, "add_paths", _fake_add_paths)

    outcome = explicit_stage(repo, ["normal.md", "other.md"], caller_paths=set())

    assert outcome.failed, "expected a genuine git-add failure"
    assert outcome.acted == ["normal.md"], (
        "expected the genuinely-partially-staged residue reconciled into "
        f"`acted`, got {outcome.acted!r}"
    )

    status_lines = _porcelain(repo)
    assert any(
        line.startswith("A") and line.endswith("normal.md") for line in status_lines
    ), f"expected normal.md to remain staged (reconciliation reports, never unstages): {status_lines}"
    assert not any(
        line.startswith("A") and line.endswith("other.md") for line in status_lines
    ), f"other.md must never have been staged: {status_lines}"


def test_stage_add_paths_partial_failure_residue_reconciles_non_ascii_path(
    tmp_path, monkeypatch
):
    """code-reviewer Finding 4: `git diff --cached --name-only` (no `-z`)
    C-quotes a path containing non-ASCII bytes (e.g. wraps `café.md` as
    `"caf\\303\\251.md"`), which used to silently fail the plain-string `p
    in residue` membership test in the residue-reconciliation branch --
    under-reporting `acted` for exactly the paths this reconciliation exists
    to catch. `diff_cached_name_only(..., nul_separated=True)` (`-z`, never
    quoted) fixes this. Proves a non-ASCII genuinely-staged residue path is
    reconciled into `acted` (see the sibling test above for why this now
    drives the underlying `git add` failure via monkeypatch rather than a
    `.gitignore`-blocked co-path, now pre-filtered before `to_stage`).
    """
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    non_ascii_name = "café.md"
    _seed_file(repo, non_ascii_name, "non-ascii content")
    _seed_file(repo, "other.md", "other content")

    real_add_paths = git_native.add_paths

    def _fake_add_paths(cwd, paths):
        real_add_paths(cwd, [paths[0]])
        return GitResult(returncode=1, stdout="", stderr="simulated non-atomic add failure")

    monkeypatch.setattr(git_native, "add_paths", _fake_add_paths)

    outcome = explicit_stage(repo, [non_ascii_name, "other.md"], caller_paths=set())

    assert outcome.failed, "expected a genuine git-add failure"
    assert outcome.acted == [non_ascii_name], (
        "expected the non-ASCII residue path to be reconciled into `acted` "
        f"despite git's C-quoting, got {outcome.acted!r}"
    )

    # `git status --porcelain` C-quotes the non-ASCII name too (`core.
    # quotePath` default) -- assert on the staged-add marker rather than an
    # exact-byte path match; the `outcome.acted` assertion above is the one
    # proving the reconciliation itself is byte-correct.
    status_lines = _porcelain(repo)
    assert any(
        line.startswith("A") and "caf" in line for line in status_lines
    ), f"expected the non-ASCII path to remain staged: {status_lines}"
    assert not any(
        line.startswith("A") and line.endswith("other.md") for line in status_lines
    ), f"other.md must never have been staged: {status_lines}"


def test_stage_add_paths_partial_failure_residue_check_itself_fails_closed(
    tmp_path, monkeypatch
):
    """code-reviewer Finding 3: the residue-reconciliation `else` branch
    (the post-`git add`-failure `git diff --cached --name-only` check
    itself fails, e.g. against an unborn/empty HEAD) is documented as
    failing closed -- report no residue rather than guess -- but nothing
    previously drove it. Monkeypatches `git_native.diff_cached_name_only`
    to return a failing `GitResult` after a genuine `git add` failure
    (itself simulated via a monkeypatched `add_paths` -- see the sibling
    tests above for why this no longer uses a `.gitignore`-blocked co-path),
    and asserts (a) `explicit_stage()` reports `acted == []` and both
    `failed` entries (the original `git add` failure plus the
    indeterminate-reconciliation entry), and (b) through
    `run_commit_pipeline`, `staged_this_call` ends up empty so the `finally`
    correctly no-ops rather than attempting an unscoped rollback.
    """
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "normal.md", "normal content")
    _seed_file(repo, "other.md", "other content")

    real_add_paths = git_native.add_paths

    def _fake_add_paths(cwd, paths):
        real_add_paths(cwd, [paths[0]])
        return GitResult(returncode=1, stdout="", stderr="simulated non-atomic add failure")

    monkeypatch.setattr(git_native, "add_paths", _fake_add_paths)
    monkeypatch.setattr(
        git_native,
        "diff_cached_name_only",
        lambda *a, **kw: GitResult(returncode=1, stdout="", stderr="simulated diff failure"),
    )

    outcome = explicit_stage(repo, ["normal.md", "other.md"], caller_paths=set())

    assert outcome.acted == []
    assert len(outcome.failed) == 2
    assert outcome.failed[0].startswith("git add:")
    assert "indeterminate" in outcome.failed[1]

    # normal.md genuinely got staged by the underlying `git add` before the
    # simulated failure -- the reconciliation check failing must not be
    # confused with "nothing was staged".
    status_lines = _porcelain(repo)
    assert any(
        line.startswith("A") and line.endswith("normal.md") for line in status_lines
    ), f"expected normal.md to remain staged despite the failed reconciliation check: {status_lines}"

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: reconciliation check itself fails",
        stage_paths=["normal.md", "other.md"],
        caller_paths=set(),
    )

    assert result.commit_failed is True
    assert result.committed_sha is None
    assert result.stage.acted == []
    # The `finally` rolls back exactly `staged_this_call` (== `stage.acted`
    # at the point of capture) -- empty here, so it must no-op rather than
    # unstage anything, including the genuinely-staged (but unreconciled)
    # `normal.md` residue.
    status_lines = _porcelain(repo)
    assert any(
        line.startswith("A") and line.endswith("normal.md") for line in status_lines
    ), f"finally must not have touched normal.md (empty staged_this_call, no-op): {status_lines}"


# ---------------------------------------------------------------------------
# Pre-stage directory-pathspec guard -- session fb5fa766, 2026-07-31
# incident: `explicit_stage()` previously staged a directory pathspec FIRST,
# and only `commit_scoped()` (further down the pipeline) refused it,
# leaving `git add`-ed residue `reset_paths()` deliberately does not clean
# up (see that function's own docstring). The guard below refuses BEFORE
# `explicit_stage()` ever runs, so a directory pathspec is never staged at
# all -- the staged-and-abandoned state becomes unreachable for this input.
# ---------------------------------------------------------------------------


def test_pipeline_directory_pathspec_leaves_index_completely_clean(tmp_path):
    """The reported incident shape: an EM passes a directory pathspec
    straight into the pipeline. Nothing must ever be staged for it -- the
    index must show no `A ` entry at all -- while the pipeline still
    reports `commit_failed` plus a diagnostic naming the cause."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "notes/alpha.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: notes",
        stage_paths=["notes/"],  # directory, not a file
        caller_paths={"notes/"},
    )

    assert result.commit_failed is True
    assert result.committed_sha is None
    assert any("directory pathspec" in d for d in result.diagnostics)
    status_lines = _porcelain(repo)
    assert not any(line.startswith("A") for line in status_lines)
    assert status_lines == ["?? notes/"]


def test_pipeline_mixed_file_and_directory_pathspec_stages_neither(tmp_path):
    """A batch mixing a real file with a directory refuses as a WHOLE -- the
    real file must not be staged either, since the guard runs before any
    staging happens for the batch."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")
    _seed_file(repo, "notes/alpha.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: mixed batch",
        stage_paths=["tasks/feature/todo.md", "notes/"],
        caller_paths={"tasks/feature/todo.md", "notes/"},
    )

    assert result.commit_failed is True
    assert result.committed_sha is None
    assert any("directory pathspec" in d for d in result.diagnostics)
    status_lines = _porcelain(repo)
    assert not any(line.startswith("A") for line in status_lines)
    # `git status --porcelain` collapses an entirely-untracked directory into
    # one `?? tasks/` line rather than listing the file inside it.
    assert any(line.strip() == "?? tasks/" for line in status_lines)
    assert any(line.strip() == "?? notes/" for line in status_lines)


# ---------------------------------------------------------------------------
# explicit_stage / run_commit_pipeline -- ignored-path pre-filter (2026-08-03
# fix, live `safe-commit-offer` incident: a gitignored path in the same
# batch as real dirty files failed the WHOLE `git add`, and the failure
# report gave a bare `exit_code=1` with no diagnosis).
# ---------------------------------------------------------------------------


def test_explicit_stage_ignored_untracked_caller_path_escalates_not_fatal(tmp_path):
    """An untracked, `.gitignore`-blocked caller path is skipped with a
    NAMED reason and drives the degraded (not fatal) `exit_code == 2` --
    never reaches `git add`, never fails the batch."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, ".gitignore", "ignored_dir/\n")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", ".gitignore", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "ignored_dir/cache.md", "secret")

    outcome = explicit_stage(
        repo, ["ignored_dir/cache.md"], caller_paths={"ignored_dir/cache.md"}
    )
    assert outcome.exit_code == 2
    assert outcome.failed == []
    assert outcome.ignored_caller_paths == ["ignored_dir/cache.md"]
    assert outcome.missing_caller_paths == []
    assert "ignored-caller:ignored_dir/cache.md" in outcome.skipped
    assert outcome.acted == []
    assert outcome.staged_paths == []


def test_explicit_stage_ignored_untracked_generated_path_benign(tmp_path):
    """A non-caller ignored path (not in `caller_paths`) is a benign skip,
    tagged distinctly from the caller-supplied case, and never drives
    `exit_code`."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, ".gitignore", "ignored_dir/\n")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", ".gitignore", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "ignored_dir/cache.md", "secret")

    outcome = explicit_stage(repo, ["ignored_dir/cache.md"])
    assert outcome.exit_code == 0
    assert outcome.ignored_caller_paths == []
    assert "ignored:ignored_dir/cache.md" in outcome.skipped


def test_explicit_stage_tracked_path_matching_gitignore_is_still_staged(tmp_path):
    """A path that is ALREADY TRACKED does not get PRE-FILTERED as "ignored"
    even once `.gitignore` starts matching its directory --
    `ignored_caller_paths` must stay empty, and its content must genuinely
    land staged, never silently dropped. Empirically, `git add` itself still
    prints a non-fatal ignore warning (and a non-zero exit) for an
    EXPLICITLY-named already-tracked path whose DIRECTORY now matches
    `.gitignore` -- but it stages the change anyway; `explicit_stage()`'s own
    pre-existing residue reconciliation (not this fix) is what recovers the
    genuinely-staged content from that noisy-but-non-fatal git quirk. Pre-
    filtering this path out (treating it as "ignored") would be the actual
    regression this test guards against -- it never happens, because
    `check_ignore()`'s index-aware default never reports a tracked path as
    ignored, so the pre-filter never applies the ignore classification to it
    at all."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "tracked_dir/kept.md", "orig")
    _git(["add", "--", "README.md", "tracked_dir/kept.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, ".gitignore", "tracked_dir/\n")
    _seed_file(repo, "tracked_dir/kept.md", "changed")

    outcome = explicit_stage(repo, ["tracked_dir/kept.md"], caller_paths={"tracked_dir/kept.md"})
    assert outcome.ignored_caller_paths == []
    assert outcome.acted == ["tracked_dir/kept.md"]
    assert _staged_blob(repo, "tracked_dir/kept.md") == "changed"


def test_explicit_stage_check_ignore_failure_fails_open_not_fatal(tmp_path, monkeypatch):
    """The documented `ignore_result.returncode not in (0, 1)` branch: an
    indeterminate `check_ignore` answer must fail open (treat nothing as
    ignored for THIS pre-filter call) rather than pre-filtering the path
    out as `ignored_caller_paths` -- it must fall through to the ordinary
    `git add` path instead, which then catches (and reports) the genuinely
    ignored-untracked path on its own merits, per the docstring's own
    "a genuinely ignored path still gets caught below by `git add` itself"
    guarantee."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, ".gitignore", "ignored_dir/\n")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", ".gitignore", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "ignored_dir/cache.md", "secret")

    monkeypatch.setattr(
        git_native,
        "check_ignore",
        lambda *a, **kw: GitResult(returncode=128, stdout="", stderr="simulated git error"),
    )

    outcome = explicit_stage(
        repo, ["ignored_dir/cache.md"], caller_paths={"ignored_dir/cache.md"}
    )
    assert outcome.ignored_caller_paths == [], (
        "an indeterminate check_ignore answer must never masquerade as a "
        f"confirmed 'ignored' pre-filter classification: {outcome.skipped!r}"
    )
    assert not any(s.startswith("ignored") for s in outcome.skipped)
    # The path was never staged (bare `git add` on a genuinely
    # ignored-untracked path fails on its own merits) -- surfaced as a
    # `git add` failure, not a silent drop.
    assert outcome.acted == []
    assert outcome.failed


def test_pipeline_ignored_and_missing_paths_do_not_fail_the_batch_and_commit_lands(tmp_path):
    """The exact live-incident shape: a batch mixing real dirty files, one
    untracked `.gitignore`-blocked path, and one genuinely-absent path must
    NOT fail the whole commit -- the real files land, with an accurate
    `committed`/`sha` report."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, ".gitignore", "ignored_dir/\n")
    for name in ("a.txt", "b.txt"):
        _seed_file(repo, name, "orig")
    _git(["add", "--", ".gitignore", "a.txt", "b.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "a.txt", "changed")
    _seed_file(repo, "b.txt", "changed")
    _seed_file(repo, "ignored_dir/cache.md", "secret")

    stage_paths = ["a.txt", "b.txt", "ignored_dir/cache.md", "never/existed.md"]
    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="safe-commit-offer: rescue real files",
        stage_paths=stage_paths,
        caller_paths=set(stage_paths),
    )

    assert result.commit_failed is False
    assert result.committed_sha is not None
    assert result.diagnostics == []
    assert _committed_files_at_head(repo) == ["a.txt", "b.txt"]
    assert _porcelain(repo) == []
    assert result.stage.ignored_caller_paths == ["ignored_dir/cache.md"]
    assert result.stage.missing_caller_paths == ["never/existed.md"]


def test_pipeline_only_unstageable_paths_reports_clean_noop_not_failure(tmp_path):
    """A group consisting ENTIRELY of an ignored path plus an absent path
    (nothing real to stage) is a clean, benign no-op -- never a failure."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, ".gitignore", "ignored_dir/\n")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", ".gitignore", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "ignored_dir/cache.md", "secret")

    stage_paths = ["ignored_dir/cache.md", "never/existed.md"]
    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="safe-commit-offer: nothing real to stage",
        stage_paths=stage_paths,
        caller_paths=set(stage_paths),
    )

    assert result.commit_failed is False
    assert result.committed_sha is None
    assert result.diagnostics == []
    # A gitignored untracked file never shows up in plain `git status
    # --porcelain` output (only `--ignored` surfaces it) -- the tree is
    # clean from git's own point of view.
    assert _porcelain(repo) == []


def test_stage_failure_report_never_a_bare_exit_code(tmp_path, monkeypatch):
    """When a genuine `git add` failure leaves `stderr` empty, the composed
    `failed` reason names the attempted paths rather than a bare
    `exit_code=N` -- the live-incident symptom this closes."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "dirty.txt", "changed content")

    real_add_paths = git_native.add_paths

    def _fake_add_paths(cwd, paths):
        # Simulate a genuine `git add` failure with an EMPTY stderr (the
        # confirmed-live shape for some git failure modes, e.g. the
        # "nothing to commit" sibling case on the commit step).
        return git_native.GitResult(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(git_native, "add_paths", _fake_add_paths)
    try:
        outcome = explicit_stage(repo, ["dirty.txt"], caller_paths={"dirty.txt"})
    finally:
        monkeypatch.setattr(git_native, "add_paths", real_add_paths)

    assert outcome.failed
    reason = outcome.failed[0]
    assert "exit_code=1" not in reason or "dirty.txt" in reason
    assert "dirty.txt" in reason


def test_commit_failure_bare_exit_code_preserved_for_downstream_quiet_rendering(
    tmp_path, monkeypatch
):
    """`commit()`'s failure branch deliberately keeps the bare `exit_code=N`
    shape (never `_reason_from_git_result()`) when `stderr` is empty --
    `coordinator/bin/scoped-git-commit`'s renderer relies on exactly this
    bare, unprefixed shape to render the benign already-committed no-op
    quietly rather than as a loud refusal (see `commit()`'s own comment)."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    real_commit_scoped = git_native.commit_scoped

    def _fake_commit_scoped(paths, msg_file, cwd, **kwargs):
        return git_native.GitResult(
            returncode=1, stdout="no changes added to commit\n", stderr=""
        )

    monkeypatch.setattr(git_native, "commit_scoped", _fake_commit_scoped)
    try:
        outcome = commit(repo, message="test", commit_paths=["README.md"])
    finally:
        monkeypatch.setattr(git_native, "commit_scoped", real_commit_scoped)

    assert outcome.exit_code != 0
    assert outcome.stderr == "exit_code=1"

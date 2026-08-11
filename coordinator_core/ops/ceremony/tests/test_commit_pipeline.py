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

import dataclasses
import subprocess
import uuid
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.commit_pipeline as commit_pipeline_mod
from coordinator_core.git.commit_trailers import compute_missing_trailer_args
from coordinator_core.ops.ceremony.commit_pipeline import (
    StageOutcome,
    commit,
    condense_git_diagnostic,
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


def _trailer_value_at_head(repo: Path, key: str) -> str:
    """The value git's OWN trailer parser extracts for `key` at HEAD.

    Review: staff-eng R1 F3, state/review-trail/2026-08-08-landed-commit-
    close-review/r1-w1.md -- a raw substring check on the commit message
    (`"Plan-Id: ..." in message`) passes whether or not the key is actually
    inside the LAST paragraph git recognises as trailers; this reads
    `%(trailers:key=...,valueonly)`, the same predicate a real consumer
    (`commit-trailer-producer-contract.md` SS2.1) uses, so a message that
    demotes the key to body prose reports empty here even though the raw
    string is still present somewhere in the message.
    """
    result = subprocess.run(
        ["git", "log", "-1", f"--format=%(trailers:key={key},valueonly)", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


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


def _seed_handoff(repo: Path, rel_path: str, frontmatter_body: str) -> None:
    _seed_file(
        repo,
        rel_path,
        f"---\nstatus: open\n{frontmatter_body}---\n\n# handoff\n",
    )


def test_pipeline_carry_gate_failure_short_circuits_before_commit(tmp_path):
    """The C1 carry_gate, wired at the gate seam alongside its two
    siblings: a staged `state/handoffs/*.md` whose `carried_items` declare
    undeclared state (here: a terminal `blocked` disposition with no
    `disposition_detail`) REFUSES the commit -- HEAD unmoved, the
    per-item violation reaches `diagnostics` verbatim (AC1, AC3)."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    # Seed the directory as tracked (a placeholder) so `git status
    # --porcelain` reports the individual file path below rather than
    # collapsing an entirely-untracked directory into a single "dirname/"
    # porcelain line (mirrors `test_dirty_tree_gate_known_concurrent_owner_
    # skipped`'s own seeding, same rationale).
    _seed_file(repo, "state/handoffs/.keep", "x")
    _git(["add", "--", "README.md", "state/handoffs/.keep"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-defective.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    description: stuck\n"
        "    disposition: blocked\n",
    )

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["state/handoffs/2026-08-10-defective.md"],
        caller_paths={"state/handoffs/2026-08-10-defective.md"},
    )

    assert result.commit_failed is True
    assert result.commit is None
    assert result.push is None
    assert result.committed_sha is None
    assert result.carry_gate is not None
    assert result.carry_gate.passed is False
    assert any("disposition_detail" in line for line in result.carry_gate.diagnostics)
    assert any("disposition_detail" in line for line in result.diagnostics)

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == head_before

    status_lines = _porcelain(repo)
    assert any(
        line.strip() == "?? state/handoffs/2026-08-10-defective.md" for line in status_lines
    )


def test_pipeline_carry_gate_well_formed_handoff_commits(tmp_path):
    """AC4: a staged handoff with well-formed `carried_items` commits
    normally -- the carry gate passes and is not the reason for any
    failure."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-clean.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    description: still open\n"
        "    disposition: carried\n",
    )

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["state/handoffs/2026-08-10-clean.md"],
        caller_paths={"state/handoffs/2026-08-10-clean.md"},
    )

    assert result.commit_failed is False
    assert result.committed_sha is not None
    assert result.carry_gate is not None
    assert result.carry_gate.passed is True
    assert result.carry_gate.skipped is False


def test_pipeline_carry_gate_skipped_when_no_handoff_staged(tmp_path):
    """AC5: a commit staging no `state/handoffs/*.md` path is unaffected --
    the carry gate reports `skipped=True`, and the commit proceeds exactly
    as it did before this gate existed."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "docs/notes.md", "ordinary content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: feature",
        stage_paths=["docs/notes.md"],
        caller_paths={"docs/notes.md"},
    )

    assert result.commit_failed is False
    assert result.committed_sha is not None
    assert result.carry_gate is not None
    assert result.carry_gate.passed is True
    assert result.carry_gate.skipped is True


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
    # W1 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md): each
    # call mints its OWN `Commit-Token:` trailer, so the two landed messages
    # can never be byte-identical including that line -- strip it from both
    # before the parity comparison, but assert each message got its own
    # (distinct) token line, proving the mechanism ran on both branches.
    agree_token_lines = [ln for ln in agree_head_message.splitlines() if ln.startswith("Commit-Token: ")]
    diverged_token_lines = [ln for ln in diverged_head_message.splitlines() if ln.startswith("Commit-Token: ")]
    assert len(agree_token_lines) == 1
    assert len(diverged_token_lines) == 1
    assert agree_token_lines != diverged_token_lines
    # Byte parity modulo the token line (Review: staff-eng R1 F3,
    # state/review-trail/2026-08-08-landed-commit-close-review/r1-w1.md):
    # strip ONLY the `Commit-Token:` line itself, in place -- every other
    # byte, including blank-line placement, must match. A blank-line-
    # insensitive content-set comparison (the pre-fix shape) cannot detect a
    # token append that splits the trailer block, because a split moves a
    # blank line, not a content line -- exactly the F1 defect this rewrite
    # exists to keep caught.
    agree_message_no_token = "\n".join(
        ln for ln in agree_head_message.splitlines() if not ln.startswith("Commit-Token: ")
    )
    diverged_message_no_token = "\n".join(
        ln for ln in diverged_head_message.splitlines() if not ln.startswith("Commit-Token: ")
    )
    assert agree_message_no_token == diverged_message_no_token
    assert "Session-Id: 22222222-2222-2222-2222-222222222222" in agree_head_message
    assert "Plan-Id: pln-00000000000000000000000000000000" in agree_head_message
    # Real trailer-parse assertions (F1/F3): a raw substring check passes
    # even when the key has been demoted to body prose by a split trailer
    # block -- ask git's OWN parser, the same predicate a real consumer
    # (commit-trailer-producer-contract.md SS2.1) uses.
    for repo in (agree_repo, diverged_repo):
        assert _trailer_value_at_head(repo, "Nature") == "chore"
        assert _trailer_value_at_head(repo, "Plan-Id") == "pln-00000000000000000000000000000000"
    assert _trailer_value_at_head(agree_repo, "Session-Id") == "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# C10/C11 (docs/plans/2026-08-07-excise-the-ceremony-lock.md) --
# `commit()`'s call into `commit_scoped()` no longer threads a precomputed
# divergence pair (AC7), and `committed_sha` is never a blind post-commit
# `git rev-parse HEAD` (AC8).
# ---------------------------------------------------------------------------


def test_commit_ac7_passes_no_known_checked_diverged_pair_to_commit_scoped(
    tmp_path, monkeypatch
):
    """AC7: `commit()`'s call into `git_native.commit_scoped()` must pass no
    `known_checked`/`known_diverged` pair at all -- C1 removes the locked
    critical section that pair's soundness rested on, so `commit_scoped()`
    must always derive divergence fresh for every path in `commit_paths`."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "README.md", "changed")

    real_commit_scoped = git_native.commit_scoped
    captured_kwargs = {}

    def _spy_commit_scoped(paths, msg_file, cwd, **kwargs):
        captured_kwargs.update(kwargs)
        return real_commit_scoped(paths, msg_file, cwd, **kwargs)

    monkeypatch.setattr(git_native, "commit_scoped", _spy_commit_scoped)
    try:
        outcome = commit(repo, message="chore: ac7\n", commit_paths=["README.md"])
    finally:
        monkeypatch.setattr(git_native, "commit_scoped", real_commit_scoped)

    assert outcome.exit_code == 0
    # S1 Finding 7: assert the KEYS are absent, not merely that they resolve
    # to None -- AC7 says "passes no pair", and `.get(...) is None` would
    # pass identically if `commit()` explicitly threaded `known_checked=None`.
    assert "known_checked" not in captured_kwargs
    assert "known_diverged" not in captured_kwargs


def test_commit_ac8_private_index_branch_sha_is_commit_scoped_stdout_verbatim(tmp_path, monkeypatch):
    """AC8: on the private-index branch, `committed_sha` is `commit_scoped()`'s
    own CAS-verified `stdout` -- verified against a SPY-CAPTURED stdout, not
    merely against `rev-parse HEAD` (S1 Finding 2: the prior form of this
    test compared to `rev-parse HEAD` only, which passes identically even if
    `commit()` re-derived the sha some other way -- it never proved the
    docstring's actual claim)."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "docs/diverged.md", "seed\n")
    _git(["add", "--", "docs/diverged.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "docs/diverged.md", "STAGED HUNK\n")
    _git(["add", "--", "docs/diverged.md"], repo)
    _seed_file(repo, "docs/diverged.md", "LATER EDIT\n")

    real_commit_scoped = git_native.commit_scoped
    captured: dict = {}

    def _spy_commit_scoped(paths, msg_file, cwd, **kwargs):
        result = real_commit_scoped(paths, msg_file, cwd, **kwargs)
        captured["stdout"] = result.stdout.strip()
        return result

    monkeypatch.setattr(git_native, "commit_scoped", _spy_commit_scoped)
    try:
        outcome = commit(repo, message="chore: ac8 diverged\n", commit_paths=["docs/diverged.md"])
    finally:
        monkeypatch.setattr(git_native, "commit_scoped", real_commit_scoped)

    assert outcome.exit_code == 0
    assert captured.get("stdout"), "spy never captured commit_scoped()'s stdout"
    assert outcome.committed_sha == captured["stdout"]


def test_commit_ac8_agree_branch_sha_resolved_via_bounded_message_match(tmp_path, monkeypatch):
    """AC8 (S1 Finding 2, rewritten): `committed_sha` is resolved by matching
    this call's own message against a `pre_sha..HEAD` range even when a REAL
    peer commit lands in the window BETWEEN this call's own commit landing
    and `commit()`'s post-commit verification -- the exact window a blind
    `git rev-parse HEAD` would misread. The prior form of this test landed
    its peer commit only AFTER `commit()` had already returned, so it passed
    identically against the pre-C11 blind-rev-parse implementation and proved
    nothing about the window; this spies on `git_native.commit_scoped` to
    land the peer commit from inside the real call, before `commit()`'s own
    verification runs."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "a.txt", "seed")
    _seed_file(repo, "b.txt", "seed")
    _git(["add", "--", "a.txt", "b.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "a.txt", "changed")

    real_commit_scoped = git_native.commit_scoped

    def _spy_commit_scoped(paths, msg_file, cwd, **kwargs):
        result = real_commit_scoped(paths, msg_file, cwd, **kwargs)
        # A REAL peer commit, landed inside the window between this call's
        # own commit landing and `commit()`'s post-commit sha verification.
        _seed_file(repo, "b.txt", "peer changed")
        _git(["add", "--", "b.txt"], repo)
        _git(["commit", "-q", "-m", "peer commit landed inside the window"], repo)
        return result

    monkeypatch.setattr(git_native, "commit_scoped", _spy_commit_scoped)
    try:
        outcome = commit(repo, message="chore: ac8 agree\n", commit_paths=["a.txt"])
    finally:
        monkeypatch.setattr(git_native, "commit_scoped", real_commit_scoped)

    assert outcome.exit_code == 0

    # HEAD is now the PEER's commit -- a blind `git rev-parse HEAD` taken at
    # this point would misreport the peer's sha as this call's own.
    peer_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert outcome.committed_sha != peer_sha

    this_call_sha = subprocess.run(
        ["git", "log", "--format=%H", "--", "a.txt"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.splitlines()[0]
    assert outcome.committed_sha == this_call_sha


def test_commit_ac4_real_peer_containing_this_calls_subject_is_not_ambiguous(
    tmp_path, monkeypatch
):
    """W1/AC4 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md):
    a REAL peer commit whose message merely *contains* this call's own
    subject verbatim -- landed inside `pre_sha..HEAD`, touching a path
    inside this call's own `commit_paths`, INSIDE the verification window
    (spying on `git_native.commit_scoped`, never after `commit()` returns) --
    must NOT produce a second candidate now that the match target is the
    minted `Commit-Token:` trailer rather than a subject substring. This is
    exactly the shape that used to collide (see the deleted
    `..._identical_subject_peer_is_ambiguous` test this replaces) -- proving
    this goes red against the reverted subject-match code is the point:
    reverting the token-match back to a subject-substring `git log --grep`
    reproduces the exact 2-candidate ambiguity this test asserts against."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "a.txt", "seed")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    subject = "chore: identical subject collision"
    _seed_file(repo, "a.txt", "this call's own change")

    real_commit_scoped = git_native.commit_scoped

    def _spy_commit_scoped(paths, msg_file, cwd, **kwargs):
        result = real_commit_scoped(paths, msg_file, cwd, **kwargs)
        # A REAL peer commit sharing this call's own subject verbatim,
        # touching the same path (inside this call's own `commit_paths`),
        # landed BEFORE `commit()`'s own post-commit verification runs.
        _seed_file(repo, "a.txt", "peer content overwrite")
        _git(["add", "--", "a.txt"], repo)
        _git(["commit", "-q", "-m", subject], repo)
        return result

    monkeypatch.setattr(git_native, "commit_scoped", _spy_commit_scoped)
    # Review: staff-eng R1 F5 -- `monkeypatch` already undoes its own patches
    # at teardown; no manual `finally` restore needed here.
    outcome = commit(repo, message=f"{subject}\n", commit_paths=["a.txt"])

    assert outcome.exit_code == 0
    assert outcome.committed_sha is not None
    assert outcome.landed is True

    # Newest-first log: the peer's own commit landed AFTER this call's own
    # commit (see `_spy_commit_scoped` above), so it is line [0] and this
    # call's own commit is line [1].
    log_lines = subprocess.run(
        ["git", "log", "--format=%H", "--", "a.txt"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    peer_sha, this_call_sha = log_lines[0], log_lines[1]
    assert outcome.committed_sha != peer_sha
    assert outcome.committed_sha == this_call_sha


def test_commit_token_appears_exactly_once_in_landed_commit_message(tmp_path):
    """W1: the minted `Commit-Token:` trailer appears exactly once in the
    landed commit's message -- proves the trailer is actually written to the
    committed message, not merely used as an in-memory match target."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "README.md", "changed")

    outcome = commit(repo, message="chore: token trailer\n", commit_paths=["README.md"])

    assert outcome.exit_code == 0
    head_message = _commit_message_at_head(repo)
    assert head_message.count("Commit-Token: ") == 1


def test_commit_landed_true_on_head_unresolvable_verification_failure(tmp_path, monkeypatch):
    """W1: the unborn-branch HEAD-unresolvable verification-failure path
    sets `landed=True` with `committed_sha=None` -- the commit genuinely
    landed, only its sha could not be confirmed."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")

    real_rev_parse_head = git_native.rev_parse_head
    calls = {"n": 0}

    def _fake_rev_parse_head(root):
        calls["n"] += 1
        if calls["n"] == 1:
            # Pre-commit HEAD probe -- genuinely unborn.
            return real_rev_parse_head(root)
        # Post-commit HEAD probe (unborn-branch fast path) -- forced
        # unresolvable to exercise the verification-failure branch even
        # though `git commit` genuinely created the root commit.
        return git_native.GitResult(returncode=1, stdout="", stderr="fatal: forced")

    monkeypatch.setattr(git_native, "rev_parse_head", _fake_rev_parse_head)
    # Review: staff-eng R1 F5 -- `monkeypatch` already undoes its own patches
    # at teardown; no manual `finally` restore needed here.
    outcome = commit(repo, message="chore: root commit\n", commit_paths=["README.md"])

    assert outcome.exit_code != 0
    assert outcome.committed_sha is None
    assert outcome.landed is True
    # The commit genuinely landed -- HEAD moved even though this call could
    # not confirm it via the (forced-failing) probe. Review: staff-eng R1 F4
    # -- `head_sha != ""` after a `check=True` rev-parse is near-tautological
    # (the subprocess would already have raised on an unborn branch); assert
    # something real: exactly one commit exists (the root commit this call's
    # own `git commit` created, not a phantom/pre-existing one).
    rev_count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert rev_count == "1"


def test_commit_ac8_agree_branch_fails_loud_when_no_message_match_found(tmp_path, monkeypatch):
    """AC8 fail-loud path: when the bounded `git rev-list`/`--grep` search
    finds NO commit matching this call's own message in `pre_sha..HEAD`
    (simulated here via a monkeypatched `git_native.log_grep`), `commit()`
    must return a non-zero `exit_code` and `committed_sha=None` -- never
    fall back to a bare `rev-parse HEAD` that could report an unrelated
    commit as this call's own."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "README.md", "changed")

    monkeypatch.setattr(
        git_native,
        "log_grep",
        lambda *a, **kw: git_native.GitResult(returncode=0, stdout="", stderr=""),
    )

    outcome = commit(repo, message="chore: ac8 no-match\n", commit_paths=["README.md"])

    assert outcome.exit_code != 0
    assert outcome.committed_sha is None
    assert outcome.landed is True
    # The commit genuinely landed (verification failure, not a commit
    # failure) -- HEAD moved even though this call could not confirm it.
    # Review: staff-eng R1 F4 -- assert something real (two commits: seed +
    # this call's own), not a near-tautological `head_sha != ""`.
    rev_count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert rev_count == "2"


def test_commit_ac8_agree_branch_fails_loud_on_ambiguous_message_match(tmp_path, monkeypatch):
    """AC8 fail-loud path, ambiguous variant: more than one candidate in
    `pre_sha..HEAD` matching this call's message is also refused rather than
    guessing which one is this call's own commit."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "README.md", "changed")

    monkeypatch.setattr(
        git_native,
        "log_grep",
        lambda *a, **kw: git_native.GitResult(
            returncode=0, stdout="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n", stderr=""
        ),
    )

    outcome = commit(repo, message="chore: ac8 ambiguous\n", commit_paths=["README.md"])

    assert outcome.exit_code != 0
    assert outcome.committed_sha is None
    assert outcome.landed is True


def test_commit_landed_false_on_ordinary_empty_commit_set_noop(tmp_path):
    """W1/AC3: the ordinary "nothing to commit" empty-commit-set exit 1 --
    `commit_paths` names a file with no actual staged/worktree change --
    must keep `landed=False`. Getting this backwards converts a harmless
    no-op into a phantom "commit landed" report; this is the single
    discrimination the whole plan turns on."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()

    # No change to README.md since the seed commit -- `commit_scoped()`'s
    # underlying `git commit` genuinely has nothing to commit.
    outcome = commit(repo, message="chore: nothing changed\n", commit_paths=["README.md"])

    assert outcome.exit_code != 0
    assert outcome.committed_sha is None
    assert outcome.landed is False

    post_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert post_head == pre_head


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
    assert result.sha_unverified is False
    assert _porcelain(repo) == []


# ---------------------------------------------------------------------------
# W2 -- run_commit_pipeline honours the third state: `landed=True` with a
# non-zero exit_code (sha verification failed, but `git commit` really did
# create a commit). docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md
# ---------------------------------------------------------------------------


def _monkeypatch_commit_landed_but_unverified(monkeypatch, real_commit):
    """Wrap the real `commit()` so it actually lands a commit (real history
    change), then reports the outcome as `landed=True`/`exit_code=1`/
    `committed_sha=None` -- exactly the shape `commit()`'s own
    verification-failure returns produce, without needing to fabricate a
    genuinely ambiguous token match. Proves the pipeline-level branch, not
    `commit()`'s own discriminator (W1 already covers that; a REAL
    verification failure reaching this branch is separately covered by
    `test_pipeline_landed_but_unverified_via_genuine_commit_token_match_
    failure` below, R2 nitpick/testing)."""

    def _wrapped(*args, **kwargs):
        outcome = real_commit(*args, **kwargs)
        assert outcome.exit_code == 0 and outcome.committed_sha is not None, (
            "test fixture assumption broken: the real commit() call must "
            "succeed cleanly before this wrapper re-reports it as unverified"
        )
        return dataclasses.replace(
            outcome,
            committed_sha=None,
            exit_code=1,
            landed=True,
            stderr="simulated: sha verification failed",
        )

    monkeypatch.setattr(commit_pipeline_mod, "commit", _wrapped)


def test_pipeline_landed_but_unverified_reports_not_failed_and_pushes(tmp_path, monkeypatch):
    """AC1: a commit that lands with an unresolvable sha returns
    `commit_failed=False`, `sha_unverified=True`, `committed_sha=None`, and
    the push step still runs (here: the no-remote skip path, since this
    fixture never configures a remote -- `push` is not None, proving
    `push_with_retry()` was actually invoked rather than skipped).

    Red proof: reverting the `commit_outcome.landed` split in
    `run_commit_pipeline` (treating every non-zero `exit_code` as a plain
    failure, as before W2) makes this assert `commit_failed is True` and
    `push is None` instead -- this test goes red against that reversion."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    real_commit = commit_pipeline_mod.commit
    _monkeypatch_commit_landed_but_unverified(monkeypatch, real_commit)

    on_committed_calls = []

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: unverified landing",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
        on_committed=on_committed_calls.append,
    )

    assert result.commit_failed is False
    assert result.committed_sha is None
    assert result.sha_unverified is True
    assert result.integrity_breach is False
    assert result.push is not None
    assert on_committed_calls == []
    # The commit really did land (real history changed via the wrapped
    # real `commit()` call), so the working tree must now be clean --
    # proving the `finally` rollback did NOT run.
    assert _porcelain(repo) == []
    assert "tasks/feature/todo.md" in _committed_files_at_head(repo)


def test_pipeline_landed_but_unverified_skips_rollback_not_by_coincidence(
    tmp_path, monkeypatch
):
    """AC2: the `finally` rollback does not run on the landed-but-unverified
    path. Proven two ways: directly, by spying on `git_native.reset_paths`
    and asserting it was never called; and independently, by asserting the
    working tree is clean and the commit that landed is really at HEAD --
    if the rollback HAD run, `git reset` would have reverted
    `staged_this_call` back toward HEAD's prior state, which on this path
    (unlike the ordinary empty-commit-set no-op) does NOT already coincide
    with the post-commit tree, since HEAD moved. Reverting `landed = True`
    on this branch (so the `finally` treats it like an ordinary failure)
    makes `reset_calls == []` fail directly -- a reset call would be
    issued even though the file is still tracked in history from the
    wrapped real commit."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    real_commit = commit_pipeline_mod.commit
    _monkeypatch_commit_landed_but_unverified(monkeypatch, real_commit)

    reset_calls = []
    real_reset_paths = commit_pipeline_mod.git_native.reset_paths

    def _spy_reset_paths(*args, **kwargs):
        reset_calls.append((args, kwargs))
        return real_reset_paths(*args, **kwargs)

    monkeypatch.setattr(commit_pipeline_mod.git_native, "reset_paths", _spy_reset_paths)

    run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: unverified landing rollback check",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    # The `reset_paths` spy is the direct proof (nothing rolled back). The
    # tree-clean + HEAD assertions below are the SECOND, independent proof
    # the docstring promises: if the `finally` rollback had run, it would
    # have reset `staged_this_call` back toward HEAD's PRIOR state, which on
    # this path does not already coincide with the post-commit tree (unlike
    # the ordinary empty-commit-set no-op) since HEAD moved.
    assert reset_calls == []
    assert _porcelain(repo) == []
    assert "tasks/feature/todo.md" in _committed_files_at_head(repo)


def test_pipeline_landed_but_unverified_deferred_push_mode_skips_push(tmp_path, monkeypatch):
    """R2 finding 1, state/review-trail/2026-08-08-landed-commit-close-
    review/r2-w2.md: the landed-but-unverified branch's `push_mode !=
    PUSH_MODE_SYNC` early return was untested -- a future edit moving
    `landed = True` below it would leave the sync-path tests green while
    rolling back a landed commit's staged paths in deferred mode. Asserts
    `pushed is None`, `push is None`, `sha_unverified is True`, and (via the
    `reset_paths` spy) that no rollback was issued.

    Red proof: commenting out this branch's early `return` (so it falls
    through to the sync push call below) makes `result.push` non-None and
    `reset_calls` unchanged but `pushed` a real bool instead of `None` --
    confirmed by temporary local revert, see run-report."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    real_commit = commit_pipeline_mod.commit
    _monkeypatch_commit_landed_but_unverified(monkeypatch, real_commit)

    reset_calls = []
    real_reset_paths = commit_pipeline_mod.git_native.reset_paths

    def _spy_reset_paths(*args, **kwargs):
        reset_calls.append((args, kwargs))
        return real_reset_paths(*args, **kwargs)

    monkeypatch.setattr(commit_pipeline_mod.git_native, "reset_paths", _spy_reset_paths)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: unverified landing deferred push",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
        push_mode=commit_pipeline_mod.PUSH_MODE_DEFERRED,
    )

    assert result.commit_failed is False
    assert result.sha_unverified is True
    assert result.committed_sha is None
    assert result.pushed is None
    assert result.push is None
    assert result.integrity_breach is False
    assert reset_calls == []
    assert _porcelain(repo) == []
    assert "tasks/feature/todo.md" in _committed_files_at_head(repo)


def test_pipeline_landed_but_unverified_failed_push_reports_integrity_breach(
    tmp_path, monkeypatch
):
    """R2 finding 0 + finding 2, state/review-trail/2026-08-08-landed-commit-
    close-review/r2-w2.md: a FAILED push (not merely a no-remote skip) on
    the landed-but-unverified sync path must report `integrity_breach=True`
    -- a durable, unpushed, unnameable commit on a shared branch is the
    worst outcome this plan exists to surface. Pre-fix this reported
    `integrity_breach=False` unconditionally on this branch (`committed_sha
    is None` made the old `committed_sha is not None and pushed is False`
    formula always False here regardless of `pushed`).

    Red proof: reverting `integrity_breach=(pushed is False)` back to the
    unconditional `integrity_breach=False` on this branch makes this test's
    final assertion fail -- confirmed by temporary local revert, see
    run-report."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    real_commit = commit_pipeline_mod.commit
    _monkeypatch_commit_landed_but_unverified(monkeypatch, real_commit)

    failing_push_outcome = commit_pipeline_mod.PushOutcome(
        exit_code=1, failed=["push: simulated rejection, retries exhausted"]
    )
    monkeypatch.setattr(
        commit_pipeline_mod, "push_with_retry", lambda root, **kw: failing_push_outcome
    )

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: unverified landing failed push",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False
    assert result.sha_unverified is True
    assert result.committed_sha is None
    assert result.push is failing_push_outcome
    assert result.pushed is False
    assert result.diagnostics and any(
        "simulated rejection" in d for d in result.diagnostics
    )
    assert result.integrity_breach is True


def test_pipeline_landed_but_unverified_via_genuine_commit_token_match_failure(
    tmp_path, monkeypatch
):
    """R2 nitpick/testing, state/review-trail/2026-08-08-landed-commit-close-
    review/r2-w2.md: every other landed-but-unverified test above drives
    `run_commit_pipeline` through `_monkeypatch_commit_landed_but_unverified`,
    which fabricates the `landed=True`/`exit_code=1`/`committed_sha=None`
    shape via `dataclasses.replace()` on an already-succeeded `commit()`
    call -- nothing in those tests proves a REAL `commit()` verification
    failure (an actually zero-or-ambiguous `Commit-Token:` match) reaches
    this pipeline branch with `landed=True`. This test closes that gap: it
    lets `commit_pipeline_mod.commit()` run completely for real, and forces
    ONLY the token-match `git log --grep` call
    (`git_native.log_grep`) it makes internally to report zero candidates --
    the same technique `test_commit_ac8_agree_branch_fails_loud_when_no_
    message_match_found` already uses to prove `commit()`'s OWN
    discriminator (W1); this test proves the PIPELINE branch consumes that
    real discriminator correctly, end to end.

    Red proof: reverting the `commit_outcome.landed` split in
    `run_commit_pipeline` (treating every non-zero `exit_code` as a plain
    failure, as before W2) makes `result.commit_failed` True and
    `result.sha_unverified` False instead -- confirmed by temporary local
    revert, see run-report."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    monkeypatch.setattr(
        git_native,
        "log_grep",
        lambda *a, **kw: git_native.GitResult(returncode=0, stdout="", stderr=""),
    )

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: genuine token match failure",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    # The real `git commit` genuinely landed (history changed) even though
    # `commit()`'s own token-match verification found zero candidates.
    assert result.commit_failed is False
    assert result.sha_unverified is True
    assert result.committed_sha is None
    assert result.commit is not None
    assert result.commit.landed is True
    assert _porcelain(repo) == []
    assert "tasks/feature/todo.md" in _committed_files_at_head(repo)


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


# ---------------------------------------------------------------------------
# push_with_retry -- branch policy (C1, 2026-08-08, docs/plans/2026-08-08-
# the-push-leg-that-never-asked-which-branch.md): consult
# auto_push.branch_gate() before ever calling git_native.push(), so a
# non-work/* branch (main included) is declined rather than silently
# pushed. All three tests configure a remote via a monkeypatched
# `git_native.remote()` so execution actually reaches the new predicate
# instead of short-circuiting on the pre-existing no-remote skip.
# ---------------------------------------------------------------------------


def test_push_with_retry_declines_non_work_branch(tmp_path, monkeypatch):
    """A policy decline on a non-`work/*` branch (e.g. `main`) must return
    `exit_code=0`, carry `push:branch-policy` plus the verbatim
    `branch_gate` message, and never call `git_native.push`."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    # _init_repo leaves the repo on whatever init.defaultBranch names; force
    # a known non-work/* name so this test does not depend on box config.
    _git(["branch", "-m", "main"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    push_calls = []
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: push_calls.append(a) or GitResult(returncode=0, stdout="", stderr="")
    )

    outcome = commit_pipeline_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:branch-policy"]
    assert outcome.message
    assert "main" in outcome.message
    assert push_calls == []


def test_push_with_retry_branch_policy_decline_prints_gate_message_to_stderr(tmp_path, monkeypatch, capsys):
    """AC6: a policy decline must print `branch_gate`'s message verbatim to
    stderr AT the moment of the decline -- asserted against the exact string
    `branch_gate` returned, not a substring authored in this test."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult
    from coordinator_core.hooks import auto_push

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "main"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: GitResult(returncode=0, stdout="", stderr="")
    )

    _, expected_message = auto_push.branch_gate("main")

    capsys.readouterr()
    outcome = commit_pipeline_mod.push_with_retry(repo)
    captured = capsys.readouterr()

    assert outcome.message == expected_message
    assert captured.err.strip() == expected_message


def test_push_with_retry_unresolvable_branch_declines_not_pushes(tmp_path, monkeypatch):
    """A detached HEAD (or any `resolve_branch` failure) must decline under
    a DISTINCT `push:branch-unresolvable` marker rather than silently
    proceeding to push a branch it cannot name."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult
    from coordinator_core.hooks import auto_push

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    monkeypatch.setattr(commit_pipeline_mod, "resolve_branch", lambda repo_root: None)
    push_calls = []
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: push_calls.append(a) or GitResult(returncode=0, stdout="", stderr="")
    )

    outcome = commit_pipeline_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:branch-unresolvable"]
    assert outcome.message is None
    assert push_calls == []


def test_push_with_retry_unresolvable_branch_prints_its_own_decline_line(tmp_path, monkeypatch, capsys):
    """AC6: an unresolvable branch has no `branch_gate` message to carry
    (`PushOutcome.message` is `None`) -- this decline must still print an
    operator-visible line, authored here rather than carried from the gate,
    naming the unresolvable-branch condition."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    monkeypatch.setattr(commit_pipeline_mod, "resolve_branch", lambda repo_root: None)
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: GitResult(returncode=0, stdout="", stderr="")
    )

    capsys.readouterr()
    outcome = commit_pipeline_mod.push_with_retry(repo)
    captured = capsys.readouterr()

    assert outcome.skipped == ["push:branch-unresolvable"]
    assert "unresolvable" in captured.err.lower() or "could not be resolved" in captured.err.lower()
    assert captured.err.strip() != ""


def test_push_with_retry_work_branch_still_pushes(tmp_path, monkeypatch, capsys):
    """A `work/*` branch must push exactly as before the branch predicate
    was added -- the gate is a pass-through, not a new obstacle, for the
    doctrine-compliant case. Amended (C3, AC6) to additionally assert NO
    decline line is printed on this path -- push_with_retry's stderr is not
    silent in general any more (the two decline arms print), so this test
    now pins the successful-push case specifically, rather than relying on
    module-wide stderr silence."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    push_calls = []
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: push_calls.append(a) or GitResult(returncode=0, stdout="", stderr="")
    )

    capsys.readouterr()
    outcome = commit_pipeline_mod.push_with_retry(repo)
    captured = capsys.readouterr()

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert outcome.skipped == []
    assert len(push_calls) == 1
    assert captured.err == ""


# ---------------------------------------------------------------------------
# _emit_push_policy_line -- C3, AC6/AC14: single owner of every push-policy
# operator line. The "override-exercised" arm is not called by any C3
# production call site (C4a wires it) -- these tests call it directly, per
# the C3 dispatch brief's instruction to author it as a real, tested,
# callable arm ahead of that call site landing.
# ---------------------------------------------------------------------------


def test_emit_push_policy_line_override_exercised_names_branch_and_reason(capsys):
    """The override arm, called directly, must print a line containing the
    branch, an unambiguous "overridden" signal, and the supplied reason."""
    capsys.readouterr()
    commit_pipeline_mod._emit_push_policy_line(
        "override-exercised", branch="main", reason="hotfix: prod outage"
    )
    captured = capsys.readouterr()

    assert "main" in captured.err
    assert "overridden" in captured.err.lower()
    assert "hotfix: prod outage" in captured.err
    assert captured.out == ""


def test_emit_push_policy_line_override_exercised_without_reason(capsys):
    """A caller-supplied reason is optional -- the line must still name the
    branch and the override, just without a reason clause."""
    capsys.readouterr()
    commit_pipeline_mod._emit_push_policy_line("override-exercised", branch="release/1.0")
    captured = capsys.readouterr()

    assert "release/1.0" in captured.err
    assert "overridden" in captured.err.lower()


# ---------------------------------------------------------------------------
# push_with_retry / run_commit_pipeline -- allow_protected_branch (C4a,
# AC8/AC14/AC15): the argument is threaded and its skip-the-gate mechanism
# is real, but it is inert for every EXISTING caller because none passes
# `True` -- default behaviour (both the decline and the work/* pass-through
# tests above) is asserted byte-identical, unchanged by this chunk.
#
# C7a correction (2026-08-08): the surrounding comment previously said a
# literal `main`-push assertion was deliberately deferred to a C4b-scoped
# test, on the theory that C4a's override was accepted-but-inert. Verified
# against HEAD at C7a time: C4a's executor implemented the override as
# FULLY FUNCTIONAL -- `push_with_retry`'s `should_push`/`allow_protected_
# branch` branch (this module, `push_with_retry`) already skips the gate
# and pushes when `True`, unconditionally, not gated behind any later
# default-flip chunk. `test_push_with_retry_literal_main_override_pushes`
# below asserts the real behaviour rather than the deferred one the stale
# comment described.
# ---------------------------------------------------------------------------


def test_push_with_retry_literal_main_override_pushes(tmp_path, monkeypatch, capsys):
    """AC8/AC14 (C7a): `allow_protected_branch=True` on the literal `main`
    branch actually pushes -- not merely a synthetic non-`work/*` name.
    Verified against HEAD at C7a dispatch time: C4a's override is fully
    functional (unconditional on `allow_protected_branch`, not gated behind
    a later default-flip chunk) -- see the correction note above this test."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "main"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    push_calls = []
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: push_calls.append(a) or GitResult(returncode=0, stdout="", stderr="")
    )

    capsys.readouterr()
    outcome = commit_pipeline_mod.push_with_retry(
        repo,
        allow_protected_branch=True,
        protected_branch_override_reason="merging-to-main Step 10 item 5",
    )
    captured = capsys.readouterr()

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert len(push_calls) == 1
    assert "main" in captured.err
    assert "overridden" in captured.err.lower()


def test_push_with_retry_default_declines_non_work_branch_unchanged(tmp_path, monkeypatch):
    """AC15: with `allow_protected_branch` accepted but not passed, a
    non-`work/*` branch still declines exactly as it did before this chunk
    -- the new keyword-only arguments are byte-identical no-ops for every
    caller that does not pass them."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "release/1.0"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    push_calls = []
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: push_calls.append(a) or GitResult(returncode=0, stdout="", stderr="")
    )

    outcome = commit_pipeline_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.skipped == ["push:branch-policy"]
    assert push_calls == []


def test_push_with_retry_override_exercised_skips_gate_and_prints(tmp_path, monkeypatch, capsys):
    """AC8/AC14: `allow_protected_branch=True` on a branch the gate would
    have declined skips `branch_gate` entirely, lets the push proceed, and
    prints the `override-exercised` line naming the branch and the supplied
    reason -- the override path is never silent."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "release/1.0"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    push_calls = []
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: push_calls.append(a) or GitResult(returncode=0, stdout="", stderr="")
    )

    capsys.readouterr()
    outcome = commit_pipeline_mod.push_with_retry(
        repo,
        allow_protected_branch=True,
        protected_branch_override_reason="release-notes bookkeeping",
    )
    captured = capsys.readouterr()

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert len(push_calls) == 1
    assert "release/1.0" in captured.err
    assert "overridden" in captured.err.lower()
    assert "release-notes bookkeeping" in captured.err


def test_push_with_retry_override_on_work_branch_prints_nothing(tmp_path, monkeypatch, capsys):
    """A `work/*` branch would have passed the gate regardless -- passing
    `allow_protected_branch=True` here overrides nothing, so no
    override-exercised line prints (an override line on every `work/*`
    push would be noise, not signal)."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: GitResult(returncode=0, stdout="", stderr="")
    )

    capsys.readouterr()
    outcome = commit_pipeline_mod.push_with_retry(repo, allow_protected_branch=True)
    captured = capsys.readouterr()

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert captured.err == ""


def test_run_commit_pipeline_accepts_override_keywords_without_error(tmp_path, monkeypatch):
    """AC8/AC15: `run_commit_pipeline` accepts both new keyword-only
    arguments and threads them through without altering its own signature
    contract for existing positional/keyword callers."""
    import inspect

    sig = inspect.signature(commit_pipeline_mod.run_commit_pipeline)
    assert "allow_protected_branch" in sig.parameters
    assert sig.parameters["allow_protected_branch"].default is False
    assert sig.parameters["allow_protected_branch"].kind == inspect.Parameter.KEYWORD_ONLY
    assert "protected_branch_override_reason" in sig.parameters
    assert sig.parameters["protected_branch_override_reason"].default is None
    assert (
        sig.parameters["protected_branch_override_reason"].kind
        == inspect.Parameter.KEYWORD_ONLY
    )


def test_run_commit_pipeline_no_claude_klabauter_caller_passes_override():
    """AC8: grep-evidenced -- no `run_commit_pipeline` call site in this
    repo passes `allow_protected_branch`. Adding a pass-through here would
    be exactly how the default gets routed around; this pins the negative
    space so a future caller doing so is a deliberate, reviewable diff."""
    pipeline_module_path = Path(commit_pipeline_mod.__file__).resolve()
    repo_root = pipeline_module_path.parents[3]
    hits = []
    for path in repo_root.rglob("*.py"):
        if "tests" in path.parts or path.resolve() == pipeline_module_path:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "run_commit_pipeline(" in text and "allow_protected_branch" in text:
            hits.append(str(path))
    assert hits == []


# ---------------------------------------------------------------------------
# _is_push_reject -- 2026-08-07 fix: converge onto auto_push.classify_error()
# instead of a bare-substring marker tuple, so a GH013 push-protection
# refusal (which still contains "failed to push some refs") is never
# misclassified as a rebase-recoverable non-fast-forward reject.
# ---------------------------------------------------------------------------


def test_is_push_reject_gh013_push_protection_is_not_retried():
    """A GitHub push-protection / branch-protection refusal must NOT drive
    the fetch+rebase+re-push cycle -- no rebase can ever fix it."""
    stderr_text = (
        "remote: error: GH013: Repository rule violations found for refs/heads/work/x.\n"
        "remote: - Push cannot contain secrets\n"
        "! [remote rejected] work/x -> work/x (push declined due to repository rule violations)\n"
        "error: failed to push some refs to 'origin'\n"
    )
    assert commit_pipeline_mod._is_push_reject(stderr_text) is False


def test_is_push_reject_genuine_non_fast_forward_is_retried():
    """A genuine non-fast-forward reject IS rebase-recoverable and must
    still drive the fetch+rebase+re-push cycle."""
    stderr_text = (
        "! [rejected] work/x -> work/x (non-fast-forward)\n"
        "error: failed to push some refs to 'origin'\n"
        "hint: Updates were rejected because the tip of your current branch is behind\n"
    )
    assert commit_pipeline_mod._is_push_reject(stderr_text) is True


def test_push_with_retry_gh013_fails_loud_without_fetch_or_rebase(tmp_path, monkeypatch):
    """End-to-end: `push_with_retry` must surface a GH013 rejection as a
    hard failure on the FIRST attempt, never spinning the fetch+rebase
    cycle (asserted by the fetch/rebase spies never being called).

    Checked out onto a `work/*` branch (2026-08-08 C1 amendment): `git init`
    lands on whatever `init.defaultBranch` names -- `main` on most boxes --
    and this test predates the branch-policy gate C1 adds to
    `push_with_retry()`. Left on the default branch, the new gate would
    decline the push before it ever reached the mocked `git_native.push`,
    which would make this test pass for the wrong reason (a policy decline
    looks like a benign early return, not the GH013 hard-failure this test
    actually exercises). `work/*` keeps the branch predicate a pass-through
    so the GH013 behaviour under test is what's asserted.
    """
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    gh013_stderr = (
        "remote: error: GH013: Repository rule violations found.\n"
        "! [remote rejected] work/x -> work/x (push declined due to repository rule violations)\n"
        "error: failed to push some refs to 'origin'\n"
    )
    monkeypatch.setattr(
        git_native, "push", lambda *a, **kw: GitResult(returncode=1, stdout="", stderr=gh013_stderr)
    )

    fetch_calls = []
    monkeypatch.setattr(git_native, "fetch", lambda *a, **kw: fetch_calls.append(a) or GitResult(returncode=0, stdout="", stderr=""))
    rebase_calls = []
    monkeypatch.setattr(
        commit_pipeline_mod, "_rebase_onto_fetched_ref",
        lambda *a, **kw: rebase_calls.append(a) or (0, ""),
    )

    outcome = commit_pipeline_mod.push_with_retry(repo)

    assert outcome.exit_code != 0
    assert fetch_calls == []
    assert rebase_calls == []


# ---------------------------------------------------------------------------
# _rebase_onto_fetched_ref -- 2026-08-07 fix: a dirty worktree must surface
# a distinctly-diagnosable reason, not an opaque `git rebase: <raw stderr>`.
# `git rebase --onto` refuses outright on a dirty worktree, and on a
# shared-fleet box the worktree is essentially never clean.
# ---------------------------------------------------------------------------


def test_rebase_onto_fetched_ref_dirty_worktree_names_the_real_cause(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # Dirty the worktree -- an uncommitted, unstaged change.
    _seed_file(repo, "README.md", "dirty change, never staged")

    exit_code, reason = commit_pipeline_mod._rebase_onto_fetched_ref(repo, "HEAD")

    assert exit_code != 0
    assert "uncommitted changes" in reason
    assert "git rebase:" not in reason


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


def test_explicit_stage_untrack_of_gitignored_path_is_not_declined(tmp_path):
    """2026-08-11 fix (example-doctrine-repo-em memo `cross-repo/inbox/2026-08-11-doe-
    claude-em-two-gaps-that-let-machine-local-files-stay-tracked.md` § 2): a
    `git rm --cached` untrack of a path `.gitignore` now matches must PASS,
    not decline with `"excluded by .gitignore"` -- being gitignored is the
    PRECONDITION of a legitimate untrack, not a violation. The file's content
    stays on disk after `--cached` (unlike a real `git rm`), which is exactly
    the case the old `exists()`-first ordering misclassified as an ordinary
    ignored ADD."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "settings.json", "machine-local content")
    _git(["add", "--", "README.md", "settings.json"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, ".gitignore", "settings.json\n")
    _git(["rm", "-q", "--cached", "settings.json"], repo)
    assert (repo / "settings.json").exists(), "sanity: --cached leaves content on disk"

    outcome = explicit_stage(repo, ["settings.json"], caller_paths={"settings.json"})
    assert outcome.exit_code == 0
    assert outcome.ignored_caller_paths == []
    assert outcome.missing_caller_paths == []
    assert outcome.staged_paths == ["settings.json"]
    assert outcome.deletion_paths == ["settings.json"]
    assert "already-staged-deleted:settings.json" in outcome.skipped
    assert not any(s.startswith("ignored") for s in outcome.skipped)


def test_explicit_stage_add_of_gitignored_path_still_declines(tmp_path):
    """The A/M-against-gitignored-path case is UNCHANGED by the untrack fix
    above: a genuinely untracked, `.gitignore`-blocked path (never staged as
    a deletion) still declines with the same `"ignored-caller:<p>"` /
    `ignored_caller_paths` reason -- `swept_delete` never contains it, so the
    reordering in `explicit_stage()` does not touch this branch at all."""
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
    assert outcome.ignored_caller_paths == ["ignored_dir/cache.md"]
    assert "ignored-caller:ignored_dir/cache.md" in outcome.skipped
    assert outcome.staged_paths == []


def test_explicit_stage_mixed_untrack_and_add_gitignored_paths_partition_correctly(tmp_path):
    """A single pathspec naming BOTH a staged untrack of a now-gitignored
    path and a genuinely untracked gitignored path partitions correctly: the
    untrack lands in `staged_paths`/`deletion_paths` with no ignore-decline,
    the untracked add still declines via `ignored_caller_paths` -- exactly
    the discriminator the memo asks for, exercised together in one call."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _seed_file(repo, "settings.json", "machine-local content")
    _git(["add", "--", "README.md", "settings.json"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, ".gitignore", "settings.json\nignored_dir/\n")
    _git(["rm", "-q", "--cached", "settings.json"], repo)
    _seed_file(repo, "ignored_dir/cache.md", "secret")

    outcome = explicit_stage(
        repo,
        ["settings.json", "ignored_dir/cache.md"],
        caller_paths={"settings.json", "ignored_dir/cache.md"},
    )
    assert outcome.exit_code == 2  # driven by the still-declined add, not the untrack
    assert outcome.ignored_caller_paths == ["ignored_dir/cache.md"]
    assert outcome.deletion_paths == ["settings.json"]
    assert "settings.json" in outcome.staged_paths
    assert "already-staged-deleted:settings.json" in outcome.skipped
    assert "ignored-caller:ignored_dir/cache.md" in outcome.skipped


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


def test_ends_with_trailer_block_false_for_subject_only_type_text_message():
    """A subject-only `type: text` message ("fix: a thing\\n") is "Key:
    value" shaped but is the message's FIRST (and only) paragraph, never a
    trailer block -- `_ends_with_trailer_block` must return False so
    `commit()`'s token mint starts its own paragraph (blank line before the
    trailer) instead of joining the subject line into one two-line
    paragraph, which git's trailer parser then refuses to parse at all
    (verified below via `git interpret-trailers --parse`, not just the
    predicate's boolean -- this is the shape that slipped through review)."""
    from coordinator_core.ops.ceremony.commit_pipeline import _ends_with_trailer_block

    subject_only = "fix: a thing\n"
    assert _ends_with_trailer_block(subject_only) is False

    token_line = "Commit-Token: deadbeefcafe"
    if _ends_with_trailer_block(subject_only):
        out_msg = subject_only + token_line + "\n"
    else:
        out_msg = subject_only + "\n" + token_line + "\n"

    parsed = _parse_trailers_via_git(out_msg)
    assert "Commit-Token" in parsed


def test_ends_with_trailer_block_false_for_conventional_commit_subjects():
    """Broader sweep of subject-only shapes that are all "Key: value"
    shaped at the LINE level but are the message's only paragraph -- none
    of them are trailer blocks."""
    from coordinator_core.ops.ceremony.commit_pipeline import _ends_with_trailer_block

    assert _ends_with_trailer_block("wsc: subject only") is False
    assert _ends_with_trailer_block("fix: a thing\n") is False
    assert _ends_with_trailer_block("docs: stop describing X\n") is False
    assert _ends_with_trailer_block("plan(x): y\n") is False
    assert _ends_with_trailer_block("Just a plain subject\n") is False
    # A genuine trailer block (subject + blank line + trailer) still True.
    assert _ends_with_trailer_block("sub\n\nSession-Id: a\n") is True


def test_token_join_normalizes_trailing_newlines_on_a_trailer_block():
    """A message ending in a trailer block AND a trailing blank line must
    still have the `Commit-Token:` trailer joined to that block, with no
    blank line between.

    Regression: `commit()`'s join branch used to read
    `base = message if message.endswith("\\n") else message + "\\n"`, which
    keeps BOTH newlines when the message ends "\\n\\n" -- so the branch whose
    whole purpose is to avoid a paragraph break introduced one anyway.

    That shape is not exotic, it is the `-F <file>` path:
    `scoped-git-commit` passes the message file's whole text (trailing
    newline included) as `subject`, and `compose_message()` returns
    `subject + "\\n"`. Landed instances: b1e0881d39a7 and 3301a8d1f68c, whose
    `Deliverable-Id:` trailers read EMPTY to
    `%(trailers:key=Deliverable-Id,valueonly)` and so cannot be joined to
    their plan by `close_out_and_stamp`.
    """
    from coordinator_core.ops.ceremony.commit_message import _ends_with_trailer_block

    message = "subject\n\nprose.\n\nDeliverable-Id: dlv-abc123\n\n"
    assert _ends_with_trailer_block(message) is True

    base = message.rstrip("\n") + "\n"
    composed = base + "Commit-Token: deadbeefcafe\n"

    assert "dlv-abc123\nCommit-Token: " in composed
    assert "dlv-abc123\n\nCommit-Token: " not in composed
    # The last paragraph -- git's trailer block -- carries BOTH trailers.
    last_paragraph = composed.rstrip("\n").split("\n\n")[-1].splitlines()
    assert "Deliverable-Id: dlv-abc123" in last_paragraph
    assert "Commit-Token: deadbeefcafe" in last_paragraph


# ---------------------------------------------------------------------------
# C2 -- push_status canonical vocabulary, and integrity_breach re-derived off
# it rather than off `pushed is False` (docs/plans/2026-08-08-the-push-leg-
# that-never-asked-which-branch.md, C2).
# ---------------------------------------------------------------------------


def test_derive_pushed_tristate_none_on_branch_policy_decline():
    """AC9/AC4: `derive_pushed_tristate` must return `None` -- not `True`,
    the pre-C2 known-wrong behaviour C1's docstring flagged -- on a
    `push:branch-policy` decline.

    Red proof: reverting the new `push:branch-policy`/`push:branch-
    unresolvable` branch in `derive_pushed_tristate` back to the pre-C2
    shape (falls through to `return True`) makes this assert `True`
    instead."""
    decline = commit_pipeline_mod.PushOutcome(
        exit_code=0,
        skipped=["push:branch-policy"],
        message="coordinator-auto-push: skipping main (not a work/* branch; doctrine: work/* only)",
    )
    assert commit_pipeline_mod.derive_pushed_tristate(decline) is None


def test_derive_pushed_tristate_none_on_unresolvable_branch_decline():
    """Same as above, for the distinct `push:branch-unresolvable` marker."""
    decline = commit_pipeline_mod.PushOutcome(exit_code=0, skipped=["push:branch-unresolvable"])
    assert commit_pipeline_mod.derive_pushed_tristate(decline) is None


def test_derive_push_status_mapping():
    """`derive_push_status` maps each `PushOutcome` shape onto the
    canonical vocabulary named in `commit_pipeline.py`'s module comment."""
    mod = commit_pipeline_mod
    assert (
        mod.derive_push_status(mod.PushOutcome(exit_code=0, skipped=["push:branch-policy"]))
        == mod.PUSH_STATUS_DECLINED
    )
    assert (
        mod.derive_push_status(
            mod.PushOutcome(exit_code=0, skipped=["push:branch-unresolvable"])
        )
        == mod.PUSH_STATUS_DECLINED
    )
    assert (
        mod.derive_push_status(mod.PushOutcome(exit_code=0, skipped=["push:no-remote"]))
        == mod.PUSH_STATUS_NO_REMOTE
    )
    assert (
        mod.derive_push_status(mod.PushOutcome(exit_code=0, acted=["push"]))
        == mod.PUSH_STATUS_PUSHED
    )
    assert (
        mod.derive_push_status(mod.PushOutcome(exit_code=1, failed=["git push: rejected"]))
        == mod.PUSH_STATUS_FAILED
    )
    assert mod.derive_push_status(None) == mod.PUSH_STATUS_NOT_ATTEMPTED


def test_pipeline_normal_path_branch_policy_decline_push_status_and_no_breach(
    tmp_path, monkeypatch
):
    """AC4/AC9, normal (non-`sha_unverified`) path: a branch-policy decline
    must report `push_status="declined"`, `pushed=None` (never the pre-C2
    `True`), and `integrity_breach=False` -- a decline is not a breach.

    Red proof: reverting `derive_pushed_tristate` to its pre-C2 shape (no
    `push:branch-policy`/`push:branch-unresolvable` branch) makes the
    `pushed is None` assertion below fail -- it falls through to `True`
    instead, which is the exact known-wrong behaviour C1's docstring
    flagged."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    decline = commit_pipeline_mod.PushOutcome(
        exit_code=0,
        skipped=["push:branch-policy"],
        message="coordinator-auto-push: skipping main (not a work/* branch; doctrine: work/* only)",
    )
    monkeypatch.setattr(commit_pipeline_mod, "push_with_retry", lambda root, **kw: decline)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: declined push, normal path",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.commit_failed is False
    assert result.committed_sha is not None
    assert result.push_status == commit_pipeline_mod.PUSH_STATUS_DECLINED
    assert result.pushed is None
    assert result.integrity_breach is False


def test_pipeline_normal_path_unresolvable_branch_decline_push_status_and_no_breach(
    tmp_path, monkeypatch
):
    """Same as above for `push:branch-unresolvable`."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    decline = commit_pipeline_mod.PushOutcome(exit_code=0, skipped=["push:branch-unresolvable"])
    monkeypatch.setattr(commit_pipeline_mod, "push_with_retry", lambda root, **kw: decline)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: declined push, unresolvable branch",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.push_status == commit_pipeline_mod.PUSH_STATUS_DECLINED
    assert result.pushed is None
    assert result.integrity_breach is False


def test_pipeline_normal_path_push_failed_reports_push_status_failed_and_breach(
    tmp_path, monkeypatch
):
    """A genuine push failure on the normal path must still report
    `push_status="push-failed"` and `integrity_breach=True` -- C2 must not
    weaken the genuine-failure case while fixing the decline case."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    failing_push_outcome = commit_pipeline_mod.PushOutcome(
        exit_code=1, failed=["push: simulated rejection, retries exhausted"]
    )
    monkeypatch.setattr(
        commit_pipeline_mod, "push_with_retry", lambda root, **kw: failing_push_outcome
    )

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: failed push, normal path",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.push_status == commit_pipeline_mod.PUSH_STATUS_FAILED
    assert result.pushed is False
    assert result.integrity_breach is True


def test_pipeline_landed_but_unverified_branch_policy_decline_no_breach(tmp_path, monkeypatch):
    """AC4/AC9, `sha_unverified` path: a branch-policy decline must report
    `push_status="declined"`, `pushed=None`, and `integrity_breach=False` --
    same rule as the normal path, at the OTHER return site
    (`integrity_breach=(push_status == PUSH_STATUS_FAILED)`).

    Red proof: reverting that site's predicate back to `(pushed is False)`
    would report `integrity_breach=False` for the WRONG reason (`pushed` was
    `True` pre-C2) -- the load-bearing assertion here is `push_status` and
    `pushed` themselves, not `integrity_breach` alone."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    real_commit = commit_pipeline_mod.commit
    _monkeypatch_commit_landed_but_unverified(monkeypatch, real_commit)

    decline = commit_pipeline_mod.PushOutcome(
        exit_code=0,
        skipped=["push:branch-policy"],
        message="coordinator-auto-push: skipping main (not a work/* branch; doctrine: work/* only)",
    )
    monkeypatch.setattr(commit_pipeline_mod, "push_with_retry", lambda root, **kw: decline)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: declined push, unverified sha path",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.sha_unverified is True
    assert result.committed_sha is None
    assert result.push_status == commit_pipeline_mod.PUSH_STATUS_DECLINED
    assert result.pushed is None
    assert result.integrity_breach is False


def test_pipeline_landed_but_unverified_push_failed_still_reports_breach(tmp_path, monkeypatch):
    """Companion regression guard to the pre-existing
    `test_pipeline_landed_but_unverified_failed_push_reports_integrity_breach`
    -- re-asserts `push_status="push-failed"` alongside the pre-existing
    `integrity_breach is True` assertion, so a future edit collapsing
    `push_status` derivation cannot silently regress while that older test
    (which does not know about `push_status`) stays green."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "tasks/feature/todo.md", "content")

    real_commit = commit_pipeline_mod.commit
    _monkeypatch_commit_landed_but_unverified(monkeypatch, real_commit)

    failing_push_outcome = commit_pipeline_mod.PushOutcome(
        exit_code=1, failed=["push: simulated rejection, retries exhausted"]
    )
    monkeypatch.setattr(
        commit_pipeline_mod, "push_with_retry", lambda root, **kw: failing_push_outcome
    )

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: unverified landing failed push, push_status",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    assert result.push_status == commit_pipeline_mod.PUSH_STATUS_FAILED
    assert result.integrity_breach is True


def _parse_trailers_via_git(message: str) -> list[str]:
    """Writes `message` to a temp file and asks git's own trailer parser
    what it recognises -- ground truth for whether a trailer got orphaned,
    not a re-implementation of the predicate's branch logic."""
    import tempfile

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-intentional-last-resort
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, newline="\n"
    ) as fh:
        fh.write(message)
        path = fh.name
    try:
        result = subprocess.run(
            ["git", "interpret-trailers", "--parse", path],
            capture_output=True,
            text=True,
            creationflags=no_window,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    return [ln.split(":", 1)[0] for ln in result.stdout.splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# push_with_retry -- pushed-range reporting (C3b, AC7, 2026-08-08,
# docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md): on a
# landed push, `PushOutcome`/`PipelineResult` now report the commit count
# and `<old-sha>..<new-sha>` range this call actually pushed, from real
# `git_native.rev_parse`/`rev_list_count` reads against a real local bare
# remote -- not mocked git output, since the whole point under test is what
# the real rev-parse/rev-list calls resolve to.
# ---------------------------------------------------------------------------


def _init_bare_remote(tmp_path: Path) -> Path:
    bare = tmp_path / "bare.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    return bare


def test_push_with_retry_landed_push_reports_count_and_range(tmp_path):
    """A push that lands with a pre-existing upstream tracking ref must
    report a real `<old>..<new>` range and a correct commit count."""
    bare = _init_bare_remote(tmp_path)
    repo = _init_repo(tmp_path)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)
    old_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    _seed_file(repo, "README.md", "second change")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "second"], repo)
    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    outcome = commit_pipeline_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert outcome.pushed_range == f"{old_sha}..{new_sha}"
    assert outcome.pushed_count == 1


def test_push_with_retry_first_push_no_upstream_reports_unknown_not_zero(tmp_path, monkeypatch):
    """A first push with no resolvable upstream tracking ref must still
    land, but report the count/range as the documented explicit-unknown
    sentinel -- never omitted, never zero. `rev_parse_upstream` is
    monkeypatched to the real "no upstream configured" failure shape
    (`git`'s own bare `git push` requires `-u`/an already-tracked branch to
    land at all -- production's `git_native.push()` never passes `-u`, so a
    genuine from-scratch first push is exercised at the `rev_parse_upstream`
    boundary here rather than by omitting `-u` from a real push, which
    would fail for an unrelated reason)."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    bare = _init_bare_remote(tmp_path)
    repo = _init_repo(tmp_path)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)

    monkeypatch.setattr(
        git_native,
        "rev_parse_upstream",
        lambda *a, **kw: GitResult(returncode=128, stdout="", stderr="fatal: no upstream configured"),
    )

    outcome = commit_pipeline_mod.push_with_retry(repo)

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    assert outcome.pushed_range is None
    assert outcome.pushed_count is None


def test_push_with_retry_decline_issues_no_rev_parse_call(tmp_path, monkeypatch):
    """A branch-policy decline must never pay for the AC7 rev-parse reads --
    that information a decline will never report (Finding 5 ordering)."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "main"], repo)

    monkeypatch.setattr(
        git_native, "remote", lambda *a, **kw: GitResult(returncode=0, stdout="origin\n", stderr="")
    )
    rev_parse_upstream_calls = []
    monkeypatch.setattr(
        git_native,
        "rev_parse_upstream",
        lambda *a, **kw: rev_parse_upstream_calls.append(a) or GitResult(returncode=1, stdout="", stderr=""),
    )
    rev_parse_calls = []
    monkeypatch.setattr(
        git_native,
        "rev_parse",
        lambda *a, **kw: rev_parse_calls.append(a) or GitResult(returncode=1, stdout="", stderr=""),
    )

    outcome = commit_pipeline_mod.push_with_retry(repo)

    assert outcome.skipped == ["push:branch-policy"]
    assert rev_parse_upstream_calls == []
    assert rev_parse_calls == []


def test_push_with_retry_no_remote_issues_no_rev_parse_call(tmp_path, monkeypatch):
    """A no-remote skip must likewise never pay for the AC7 rev-parse reads."""
    from coordinator_core.ops.ceremony import git_native
    from coordinator_core.ops.ceremony.git_native import GitResult

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    rev_parse_upstream_calls = []
    monkeypatch.setattr(
        git_native,
        "rev_parse_upstream",
        lambda *a, **kw: rev_parse_upstream_calls.append(a) or GitResult(returncode=1, stdout="", stderr=""),
    )

    outcome = commit_pipeline_mod.push_with_retry(repo)

    assert outcome.skipped == ["push:no-remote"]
    assert rev_parse_upstream_calls == []


def test_push_with_retry_rebase_retry_range_excludes_concurrent_commit(tmp_path):
    """A push that lands only after a reject-triggered fetch+rebase retry
    must report a range covering ONLY the commits this call itself pushed --
    NOT the commits a concurrent peer pushed to the same branch in between,
    which already reached the remote via that peer's own push."""
    bare = _init_bare_remote(tmp_path)

    repo = _init_repo(tmp_path)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)

    # A concurrent peer clones the same remote, adds its own commit, and
    # pushes it -- advancing origin/work/x past what `repo` has fetched.
    peer = tmp_path / "peer"
    _git(["clone", "-q", "--branch", "work/x", str(bare), str(peer)], tmp_path)
    _git(["config", "user.email", "peer@t.example"], peer)
    _git(["config", "user.name", "peer"], peer)
    _seed_file(peer, "PEER.md", "peer content")
    _git(["add", "--", "PEER.md"], peer)
    _git(["commit", "-q", "-m", "peer commit"], peer)
    peer_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(peer), capture_output=True, text=True, check=True
    ).stdout.strip()
    _git(["push", "-q", "origin", "work/x"], peer)

    # Back in `repo`, unaware of the peer's push, add this call's own
    # commit -- pushing it will be rejected (non-fast-forward) first.
    _seed_file(repo, "README.md", "own change")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "own commit"], repo)

    outcome = commit_pipeline_mod.push_with_retry(repo)

    # `own_new_sha` is read AFTER `push_with_retry` -- the rebase step
    # rewrites this commit onto the peer's tip, so its sha post-rebase
    # differs from the pre-rebase sha captured above.
    own_new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    assert outcome.exit_code == 0
    assert outcome.acted == ["push"]
    # The reported range's lower bound must be the peer's commit (the tip
    # this call fetched right before rebasing), never the pre-loop tip from
    # before the peer's push -- and its count must be 1 (this call's own
    # commit only), never 2 (which would double-count the peer's).
    assert outcome.pushed_range == f"{peer_sha}..{own_new_sha}"
    assert outcome.pushed_count == 1


def test_pipeline_landed_push_populates_pushed_range_and_count(tmp_path):
    """`run_commit_pipeline`'s own `PipelineResult` (not just the inner
    `PushOutcome`) must carry `pushed_range`/`pushed_count` through on a
    landed push, plus a diagnostics line naming what was pushed."""
    bare = _init_bare_remote(tmp_path)
    repo = _init_repo(tmp_path)
    _git(["checkout", "-q", "-b", "work/x"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["push", "-q", "-u", "origin", "work/x"], repo)
    old_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: C3b pushed-range",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )
    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    assert result.push_status == commit_pipeline_mod.PUSH_STATUS_PUSHED
    assert result.pushed_range == f"{old_sha}..{new_sha}"
    assert result.pushed_count == 1
    assert any("push landed" in d for d in result.diagnostics)


# ---------------------------------------------------------------------------
# C7b (docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md) --
# `run_commit_pipeline`'s new `deliverable_id` kwarg threads through
# `commit()` to `git_native.commit_scoped()` and lands as this commit's
# `Deliverable-Id:` trailer. Read the trailer OFF THE LANDED COMMIT (AC16),
# never off `trailer_args`/argv in isolation -- the same discipline
# `test_commit_scoped.py`'s own AC17 coverage uses.
# ---------------------------------------------------------------------------


def test_pipeline_deliverable_id_round_trips_onto_landed_commit_trailer(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # `_validate_explicit_deliverable_id` (C7a) resolves an explicit
    # `deliverable_id` against a `docs/plans/` frontmatter artifact carrying
    # the same id -- not against this commit's own pathspec (see that
    # function's own docstring, and `test_commit_scoped.py`'s
    # `_seed_deliverable_artifact` sibling helper).
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "seed-plan.md").write_text(
        "---\ndeliverable_id: dlv-c7btest\n---\n\n# seed plan\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed plan artifact"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: C7b deliverable_id round-trip",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
        deliverable_id="dlv-c7btest",
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None
    assert _trailer_value_at_head(repo, "Deliverable-Id") == "dlv-c7btest"


def test_pipeline_op_scope_gate_failure_short_circuits_before_commit(tmp_path):
    """The op-scope-coverage gate, wired at the gate seam alongside its three
    siblings: a staged `_registry_map.py` registering an op with no matching
    `op_scopes._OP_KEY_SCOPE` entry REFUSES the commit -- HEAD unmoved."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    registry_relpath = "coordinator_core/ops/_registry_map.py"
    op_scopes_relpath = "coordinator_core/op_scopes.py"

    _seed_file(
        repo,
        registry_relpath,
        "from typing import Dict\n\n"
        'OP_MODULE_MAP: Dict[str, str] = {\n'
        '    "known.op": "some.module",\n'
        '    "unclassified.op": "some.module",\n'
        "}\n",
    )
    # `op_scopes.py` need not itself be staged -- read from the worktree
    # regardless (matches `op_scope_coverage_gate`'s own docstring).
    _seed_file(
        repo,
        op_scopes_relpath,
        "from typing import Dict\n\n"
        '_OP_KEY_SCOPE: Dict[str, str] = {\n'
        '    "known.op": "none",\n'
        "}\n",
    )

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="feat: register an op",
        stage_paths=[registry_relpath],
        caller_paths={registry_relpath},
    )

    assert result.commit_failed is True
    assert result.commit is None
    assert result.push is None
    assert result.committed_sha is None
    assert result.op_scope_gate is not None
    assert result.op_scope_gate.passed is False
    assert any("unclassified.op" in line for line in result.op_scope_gate.diagnostics)
    assert any("unclassified.op" in line for line in result.diagnostics)

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == head_before


def test_pipeline_op_scope_gate_clean_registry_edit_commits(tmp_path):
    """An ordinary commit that touches `_registry_map.py` but leaves it fully
    covered by `_OP_KEY_SCOPE` passes -- the gate must not fire on a clean
    registry."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    registry_relpath = "coordinator_core/ops/_registry_map.py"
    op_scopes_relpath = "coordinator_core/op_scopes.py"

    _seed_file(
        repo,
        registry_relpath,
        "from typing import Dict\n\n"
        'OP_MODULE_MAP: Dict[str, str] = {\n'
        '    "known.op": "some.module",\n'
        "}\n",
    )
    _seed_file(
        repo,
        op_scopes_relpath,
        "from typing import Dict\n\n"
        '_OP_KEY_SCOPE: Dict[str, str] = {\n'
        '    "known.op": "none",\n'
        "}\n",
    )

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="feat: register a fully-classified op",
        stage_paths=[registry_relpath],
        caller_paths={registry_relpath},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.op_scope_gate is not None
    assert result.op_scope_gate.passed is True
    assert result.committed_sha is not None


class TestCondenseGitDiagnostic:
    """Regression guard for the 2026-08-10 incident: four consecutive
    `scoped-git-commit` refusals reported nothing but CRLF line-ending
    warnings, hiding the `detect-staged-rollback` pre-commit BLOCK that was
    the real cause. Head-truncating git's output is what lost the diagnosis.
    """

    def test_advisory_lines_are_dropped_when_a_real_diagnosis_exists(self):
        blob = (
            "warning: in the working copy of 'a.md', LF will be replaced by CRLF\n"
            "warning: in the working copy of 'b.md', LF will be replaced by CRLF\n"
            "fatal: pathspec 'c.md' did not match any files\n"
        )
        assert condense_git_diagnostic(blob) == "fatal: pathspec 'c.md' did not match any files"

    def test_hint_lines_are_advisory_too(self):
        blob = "hint: use --force\nerror: failed to push some refs\n"
        assert condense_git_diagnostic(blob) == "error: failed to push some refs"

    def test_all_advisory_blob_is_preserved_not_emptied(self):
        blob = "warning: LF will be replaced by CRLF\n"
        assert condense_git_diagnostic(blob) == "warning: LF will be replaced by CRLF"

    def test_empty_stays_empty_so_the_bare_exit_code_fallback_still_fires(self):
        assert condense_git_diagnostic("") == ""
        assert condense_git_diagnostic("   \n  ") == ""

    def test_oversized_output_keeps_the_tail_where_the_verdict_lands(self):
        body = "\n".join(f"  offending path {i}" for i in range(400))
        blob = f"detect-staged-rollback: BLOCKED\n{body}\npre-commit: BLOCKED -- gate [staged-rollback]"
        condensed = condense_git_diagnostic(blob, limit=200)
        assert condensed.startswith("...(truncated) ")
        assert condensed.endswith("pre-commit: BLOCKED -- gate [staged-rollback]")

    def test_a_pre_commit_block_survives_a_large_crlf_warning_preamble(self):
        preamble = "\n".join(
            f"warning: in the working copy of 'archive/f{i}.md', "
            "LF will be replaced by CRLF the next time Git touches it"
            for i in range(20)
        )
        blob = f"{preamble}\ndetect-staged-rollback: BLOCKED -- 20 staged path(s)"
        assert condense_git_diagnostic(blob) == "detect-staged-rollback: BLOCKED -- 20 staged path(s)"


def test_make_pipeline_result_covers_every_field():
    """The drift guard for `fixtures/pipeline_result.py`'s `_FIELD_DEFAULTS`.

    That helper exists to retire a defect class: `PipelineResult` gained
    `carry_gate` (6a4d013d2) and `op_scope_gate` (b7b650bc6) on consecutive
    days, and every hand-built test double had to be swept by hand both
    times. A newly-added REQUIRED field still raises inside the helper, which
    is loud and correct. A newly-added field carrying a DEFAULT would not --
    the double would silently take a value nobody in this package chose. This
    test is what fires on that case, and it fails in the same edit that adds
    the field rather than in an unrelated assertion much later.
    """
    from .fixtures.pipeline_result import _FIELD_DEFAULTS

    declared = {f.name for f in dataclasses.fields(commit_pipeline_mod.PipelineResult)}
    # `diagnostics` is owned by the helper's own per-call fresh-list handling,
    # never by the shared default map -- see `make_pipeline_result`.
    assert set(_FIELD_DEFAULTS) | {"diagnostics"} == declared, (
        "PipelineResult's fields and fixtures/pipeline_result.py's "
        "_FIELD_DEFAULTS have drifted -- add the new field to _FIELD_DEFAULTS "
        "with a deliberate default so ceremony test doubles do not silently "
        "inherit one"
    )


# ---------------------------------------------------------------------------
# state/bug-backlog/2026-08-11-run-commit-pipeline-reports-a-concurrent-
# 0a91ea7dc77b.yaml (P1) -- `resolve_post_push_sha` must adopt a post-push
# re-read only when it can VERIFY the re-read names our own commit (by tree
# identity with the caller's already-verified `pre_push_sha`), never on a
# bare "the read succeeded" basis. Direct unit coverage of the helper, plus
# one `run_commit_pipeline` integration test exercising the actual call
# site's wiring.
# ---------------------------------------------------------------------------


def _rev_parse_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


class TestResolvePostPushSha:
    def test_ordinary_landed_push_head_matches_pre_push_sha(self, tmp_path):
        """No rebase, no race: HEAD after push IS the pre-push sha -- adopted
        trivially (the common, non-racy case)."""
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)
        sha = _rev_parse_head(repo)

        resolved = commit_pipeline_mod.resolve_post_push_sha(repo, sha)

        assert resolved == sha

    def test_peer_lands_in_window_keeps_our_own_pre_push_sha(self, tmp_path):
        """Ordinary push, peer lands in the window: HEAD has moved to a
        FOREIGN commit (different tree) by the time of the re-read -- must
        NOT be adopted; the caller's own known-good pre-push sha is kept."""
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)
        our_sha = _rev_parse_head(repo)

        # Simulate a peer's commit landing on this shared branch after our
        # push completed but before this call's re-read runs.
        _seed_file(repo, "PEER.md", "peer content")
        _git(["add", "--", "PEER.md"], repo)
        _git(["commit", "-q", "-m", "peer commit\n\nSession-Id: peer-session-id"], repo)
        peer_sha = _rev_parse_head(repo)
        assert peer_sha != our_sha

        resolved = commit_pipeline_mod.resolve_post_push_sha(repo, our_sha)

        assert resolved == our_sha
        assert resolved != peer_sha

    def test_empty_peer_commit_in_window_keeps_our_own_pre_push_sha(self, tmp_path):
        """The case tree-equality alone cannot see. An empty commit inherits
        its parent's tree verbatim, so a peer's no-op commit landing on top of
        ours has a tree IDENTICAL to ours -- a tree-only check would read that
        as a rebase and adopt the stranger's sha. Ancestry discriminates it:
        our commit is still reachable from the new tip, which a real rebase
        (which rewrites it) would never leave true."""
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)
        our_sha = _rev_parse_head(repo)

        _git(["commit", "-q", "--allow-empty", "-m", "peer no-op\n\nSession-Id: peer-session-id"], repo)
        peer_sha = _rev_parse_head(repo)
        assert peer_sha != our_sha
        # The premise of this test: the trees really are identical, so the
        # tree check cannot be what saves us here.
        def _tree_of(sha: str) -> str:
            return subprocess.run(
                ["git", "rev-parse", f"{sha}^{{tree}}"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        assert _tree_of(our_sha) == _tree_of(peer_sha)

        resolved = commit_pipeline_mod.resolve_post_push_sha(repo, our_sha)

        assert resolved == our_sha
        assert resolved != peer_sha

    def test_rebase_retry_adopts_the_rewritten_sha(self, tmp_path):
        """A genuine reject-triggered `rebase --onto`: HEAD names a NEW sha
        (different parent) carrying the IDENTICAL tree as our pre-push
        commit -- this is the case the re-read exists for, and must still be
        adopted."""
        repo = _init_repo(tmp_path)
        _seed_file(repo, "README.md", "seed")
        _git(["add", "--", "README.md"], repo)
        _git(["commit", "-q", "-m", "seed"], repo)
        base_sha = _rev_parse_head(repo)

        _seed_file(repo, "feature.md", "our change")
        _git(["add", "--", "feature.md"], repo)
        _git(["commit", "-q", "-m", "our commit"], repo)
        our_sha = _rev_parse_head(repo)
        our_tree = subprocess.run(
            ["git", "rev-parse", f"{our_sha}^{{tree}}"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        # The fetched remote tip `push_with_retry`'s rebase lands on. It must
        # be a SIBLING of our commit, branching off `base_sha` -- never a
        # descendant of it. A push is rejected precisely because the remote
        # advanced WITHOUT our commit, so a remote tip that already contained
        # ours could not have produced the reject this rebase exists to
        # recover from. Built via `commit-tree` off `base_sha` directly rather
        # than by committing on top of HEAD, which would silently make our own
        # commit an ancestor of the rewritten one and misrepresent the shape.
        other_blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=str(repo), input="unrelated fetched content", capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "read-tree", base_sha],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{other_blob},OTHER.md"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        other_tree = subprocess.run(
            ["git", "write-tree"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()
        new_base_sha = subprocess.run(
            ["git", "commit-tree", other_tree, "-p", base_sha, "-m", "unrelated fetched commit"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert new_base_sha != base_sha
        # The property that makes this a rebase and not a peer landing on top.
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor", our_sha, new_base_sha],
            cwd=str(repo), capture_output=True, text=True,
        ).returncode != 0
        subprocess.run(
            ["git", "read-tree", our_sha], cwd=str(repo), capture_output=True, text=True, check=True
        )

        # A rebase --onto this new base reapplies the identical diff -- same
        # tree, new sha, new parent. Reproduce that shape directly via
        # `commit-tree` rather than driving a real rebase, since only the
        # RESULT shape (same tree, different sha/parent) matters here.
        new_parent_result = subprocess.run(
            ["git", "commit-tree", our_tree, "-p", new_base_sha, "-m", "our commit"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        rewritten_sha = new_parent_result.stdout.strip()
        assert rewritten_sha != our_sha
        _git(["update-ref", "HEAD", rewritten_sha], repo)
        assert _rev_parse_head(repo) == rewritten_sha

        resolved = commit_pipeline_mod.resolve_post_push_sha(repo, our_sha)

        assert resolved == rewritten_sha

    def test_reread_failure_falls_back_to_pre_push_sha(self, tmp_path):
        """The post-push `rev-parse HEAD` itself failing (e.g. an unborn
        branch) must fall back to the caller's pre-push value, exactly as
        before this fix -- never downgrade a known-good value to None."""
        repo = _init_repo(tmp_path)
        # No commits at all -- `git rev-parse HEAD` fails (unborn branch).

        resolved = commit_pipeline_mod.resolve_post_push_sha(repo, "deadbeef" * 5)

        assert resolved == "deadbeef" * 5

    def test_none_pre_push_sha_passes_through_unchanged(self, tmp_path):
        repo = _init_repo(tmp_path)
        assert commit_pipeline_mod.resolve_post_push_sha(repo, None) is None


def test_pipeline_post_push_peer_race_keeps_own_committed_sha(tmp_path, monkeypatch):
    """End-to-end regression for the filed defect: `run_commit_pipeline`
    must report ITS OWN `committed_sha`, never a peer's, even though the
    post-push leg still re-reads HEAD after a landed push. `push_with_retry`
    is monkeypatched to simulate a peer's commit landing on the shared
    branch in exactly the window between "push landed" and "re-read HEAD" --
    without this fix, `final_committed_sha` would silently become the
    peer's sha."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    def fake_push_with_retry(root):
        _seed_file(root, "PEER.md", "peer content")
        _git(["add", "--", "PEER.md"], root)
        _git(["commit", "-q", "-m", "peer commit\n\nSession-Id: peer-session-id"], root)
        return commit_pipeline_mod.PushOutcome(
            exit_code=0, acted=["push"], skipped=[], failed=[], message=None,
            pushed_range=None, pushed_count=None,
        )

    monkeypatch.setattr(commit_pipeline_mod, "push_with_retry", fake_push_with_retry)

    result = run_commit_pipeline(
        repo,
        session_id=_unique_session_id(),
        subject="workstream-complete: peer race regression",
        stage_paths=["tasks/feature/todo.md"],
        caller_paths={"tasks/feature/todo.md"},
    )

    own_sha = result.commit.committed_sha
    peer_sha = _rev_parse_head(repo)

    assert result.push_status == commit_pipeline_mod.PUSH_STATUS_PUSHED
    assert own_sha is not None
    assert own_sha != peer_sha
    assert result.committed_sha == own_sha

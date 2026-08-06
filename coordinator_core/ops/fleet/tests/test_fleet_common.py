"""
Tests for coordinator_core.ops.fleet._common — shared envelope helpers, param validation,
D3 repo_root check, main_worktree_root derivation, and archive_and_commit git helper.

Coverage:
  (a) Envelope exit_code derivation:
      - 0: all acted, failed[] empty.
      - 2: DETERMINATE-PARTIAL — failed[] non-empty; git status --porcelain CLEAN and
           git log -1 (--no-renames) shows exactly the moved SUBSET; the failed item
           leaves NO staged residue (AC6).
      - 1: setup error → standard echoed envelope, empty arrays, no top-level reason field.
  (b) mode-unknown → exit_code:1 fail-closed (version-skew guard, contract §4).
  (c) candidate_ids absent on dry_run:false → exit_code:1.
  (d) D3 check: cosmetic repo_root diff (/var↔/private/var, trailing slash) NOT a mismatch;
      genuine mismatch → exit_code:1 reason "repo_root-mismatch".
  (e) archive_and_commit uses a private GIT_INDEX_FILE — a concurrent git add -A on the main
      index during the op does not absorb the op's staged paths; commit is both-sides
      (src+dst) scoped pathspec.

Import guard: coordinator_core.ops.fleet.prune_bugs imported at module level so that
@register_op fires before any test relying on registry state.  Floor assertion: ≥1 fleet.*
op registered.

Harness: asyncio.run() in sync test fns — no pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op for fleet.prune_closed_bugs. ----
import coordinator_core.ops.fleet.prune_bugs  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet._common import (
    Move,
    _make_git_env,
    archive_and_commit,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    validate_params,
)


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _git_log_names_no_renames(fleet_repo, n: int = 1):
    """Return file paths touched in the last n commits with rename-detection disabled.

    Uses --no-renames so that a git-mv rename is shown as BOTH the source (deleted)
    and the destination (added) as separate entries — unlike the default --name-only
    behaviour which collapses a rename into only the destination path.
    Required for AC4/DR-211 D3 both-sides pathspec verification.
    Mirrors the same helper in test_archive_handoffs.py.
    """
    result = subprocess.run(
        ["git", "log", f"-{n}", "--no-renames", "--name-only", "--format="],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    return [
        line
        for line in result.stdout.decode(errors="replace").strip().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_fleet_ops_floor():
    """At least one fleet.* op must be registered after the import guard fires."""
    fleet_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
    assert len(fleet_ops) >= 1, (
        "Import guard must register at least one fleet.* op; "
        f"registered: {sorted(fleet_ops)}"
    )


# ---------------------------------------------------------------------------
# (a) Envelope exit_code derivation
# ---------------------------------------------------------------------------


def test_build_act_result_exit_code_0_all_acted():
    """build_act_result with all acted and no failed → exit_code:0."""
    result = build_act_result(
        "already-terminal",
        acted=[{"id": "state/bug-backlog/foo.yaml", "archived": True}],
        skipped=[],
        failed=[],
    )
    assert result["exit_code"] == 0
    assert result["dry_run"] is False
    assert result["mode"] == "already-terminal"
    assert result["candidates"] == []
    assert result["failed"] == []


def test_build_act_result_exit_code_0_with_skipped():
    """build_act_result with acted + skipped and no failed → exit_code:0."""
    result = build_act_result(
        "already-terminal",
        acted=[{"id": "state/bug-backlog/a.yaml", "archived": True}],
        skipped=[{"id": "state/bug-backlog/b.yaml", "reason": "already-archived"}],
        failed=[],
    )
    assert result["exit_code"] == 0


def test_build_act_result_exit_code_2_determinate_partial(fleet_repo):
    """DETERMINATE-PARTIAL: failed[] non-empty → exit_code:2.

    Calls archive_and_commit directly with one valid tracked move and one failing
    move (source path does not exist → git mv fails).

    Asserts (AC6):
    - exit_code == 2 (determinate-partial, not indeterminate-retry)
    - git status --porcelain CLEAN — failed item leaves NO staged residue
    - git log -1 (--no-renames) shows exactly the moved SUBSET (good src + good dst)
    - Failed item's paths do NOT appear in git log
    """
    # Seed one tracked file for the successful move
    good_path = fleet_repo.seed_bug("2026-04-10-good-bug.yaml", "closed")
    good_id = fleet_repo.repo_rel(good_path)
    good_dst = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / good_path.name
    good_dst_rel = good_dst.relative_to(fleet_repo.root).as_posix()

    # Nonexistent source → git mv fails (file not on disk, not in git index)
    bad_src = fleet_repo.root / "state" / "bug-backlog" / "nonexistent-ghost.yaml"
    bad_dst = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / "nonexistent-ghost.yaml"
    bad_id = "state/bug-backlog/nonexistent-ghost.yaml"
    bad_dst_rel = bad_dst.relative_to(fleet_repo.root).as_posix()

    moves = [
        Move(src=good_path, dst=good_dst, candidate_id=good_id),
        Move(src=bad_src, dst=bad_dst, candidate_id=bad_id),
    ]

    acted, failed = _run(archive_and_commit(
        fleet_repo.root,
        moves,
        "archive(bug-backlog): partial-failure test",
    ))

    # Verify acted/failed split
    assert len(acted) >= 1, f"At least one item must be acted; got acted={acted}"
    assert len(failed) >= 1, f"Failed list must be non-empty; got failed={failed}"
    assert acted[0]["id"] == good_id
    assert acted[0]["archived"] is True

    # Build the envelope to verify exit_code:2
    result = build_act_result("already-terminal", acted=acted, skipped=[], failed=failed)
    assert result["exit_code"] == 2, (
        f"DETERMINATE-PARTIAL must produce exit_code:2; "
        f"acted={acted}, failed={failed}"
    )

    # git status --porcelain must be CLEAN — failed item leaves no staged residue (AC6).
    assert fleet_repo.git_status_clean(), (
        "git status --porcelain must be clean after partial failure — "
        "the failed item must leave no staged residue in the main index (AC6)"
    )

    # git log -1 (--no-renames) must show exactly the moved subset
    log_names = _git_log_names_no_renames(fleet_repo, 1)
    assert good_id in log_names, (
        f"Successful move src must appear in git log; got {log_names}"
    )
    assert good_dst_rel in log_names, (
        f"Successful move dst must appear in git log; got {log_names}"
    )
    # Failed item must NOT appear
    assert bad_id not in log_names, (
        f"Failed item src must NOT appear in git log; got {log_names}"
    )
    assert bad_dst_rel not in log_names, (
        f"Failed item dst must NOT appear in git log; got {log_names}"
    )


def test_build_setup_error_standard_envelope():
    """exit_code:1 setup error → standard echoed envelope, empty arrays, no reason field.

    The frozen wire envelope is NOT expanded on setup errors (contract §3.2).
    Human-readable reason is logged daemon-side; cockpit branches on exit_code:1 alone.
    """
    result = build_setup_error_result("already-terminal", True, "test error reason")
    assert result["exit_code"] == 1
    assert result["mode"] == "already-terminal"
    assert result["dry_run"] is True
    assert result["candidates"] == []
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    # Frozen envelope: no reason/error top-level field
    assert "reason" not in result, (
        "exit_code:1 must NOT add a 'reason' top-level wire field"
    )
    assert "error" not in result, (
        "exit_code:1 must NOT add an 'error' top-level wire field"
    )


def test_build_setup_error_preserves_mode_dry_run():
    """exit_code:1 envelope echoes mode and dry_run (even if they are None)."""
    result = build_setup_error_result(None, None, "mode-missing")
    assert result["exit_code"] == 1
    assert result["mode"] is None
    assert result["dry_run"] is None


# ---------------------------------------------------------------------------
# (b) mode-unknown → exit_code:1 fail-closed (version-skew guard, contract §4)
# ---------------------------------------------------------------------------


def test_mode_unknown_returns_setup_error():
    """Unknown mode → validate_params returns exit_code:1 envelope (fail-closed)."""
    result = validate_params({
        "mode": "not-a-real-mode",
        "dry_run": True,
    })
    assert isinstance(result, dict), (
        "Unknown mode must return a dict (setup-error envelope), not a tuple"
    )
    assert result["exit_code"] == 1
    # Standard echoed shape — empty arrays, no reason field
    assert result["candidates"] == []
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    assert "reason" not in result


def test_mode_none_returns_setup_error():
    """mode=None is not 'already-terminal' → exit_code:1 (fail-closed)."""
    result = validate_params({"mode": None, "dry_run": True})
    assert isinstance(result, dict)
    assert result["exit_code"] == 1


def test_mode_empty_string_returns_setup_error():
    """mode='' is not 'already-terminal' → exit_code:1 (fail-closed)."""
    result = validate_params({"mode": "", "dry_run": True})
    assert isinstance(result, dict)
    assert result["exit_code"] == 1


def test_mode_valid_returns_tuple():
    """mode='already-terminal' with valid params → returns (mode, dry_run, candidate_ids)."""
    result = validate_params({
        "mode": "already-terminal",
        "dry_run": True,
    })
    assert isinstance(result, tuple), "Valid params must return a tuple, not a dict"
    mode, dry_run, candidate_ids = result
    assert mode == "already-terminal"
    assert dry_run is True


# ---------------------------------------------------------------------------
# (c) candidate_ids absent on dry_run:false → exit_code:1
# ---------------------------------------------------------------------------


def test_candidate_ids_absent_on_act_returns_setup_error():
    """dry_run:false with candidate_ids absent → exit_code:1 (contract §3.1 :238-241)."""
    result = validate_params({
        "mode": "already-terminal",
        "dry_run": False,
        # candidate_ids key absent
    })
    assert isinstance(result, dict)
    assert result["exit_code"] == 1


def test_candidate_ids_null_on_act_returns_setup_error():
    """dry_run:false with candidate_ids=None → exit_code:1."""
    result = validate_params({
        "mode": "already-terminal",
        "dry_run": False,
        "candidate_ids": None,
    })
    assert isinstance(result, dict)
    assert result["exit_code"] == 1


def test_candidate_ids_empty_on_act_returns_setup_error():
    """dry_run:false with candidate_ids=[] → exit_code:1 (no 'act on all' fallback)."""
    result = validate_params({
        "mode": "already-terminal",
        "dry_run": False,
        "candidate_ids": [],
    })
    assert isinstance(result, dict)
    assert result["exit_code"] == 1


def test_candidate_ids_optional_on_dry_run():
    """dry_run:true with candidate_ids absent → validation succeeds (no candidate_ids required)."""
    result = validate_params({
        "mode": "already-terminal",
        "dry_run": True,
        # candidate_ids absent — permitted on preview
    })
    assert isinstance(result, tuple)
    mode, dry_run, candidate_ids = result
    assert mode == "already-terminal"
    assert dry_run is True
    assert candidate_ids is None


def test_candidate_ids_present_on_act_ok():
    """dry_run:false with non-empty candidate_ids → validation succeeds (returns tuple)."""
    result = validate_params({
        "mode": "already-terminal",
        "dry_run": False,
        "candidate_ids": ["state/bug-backlog/foo.yaml"],
    })
    assert isinstance(result, tuple)
    _, _, ids = result
    assert ids == ["state/bug-backlog/foo.yaml"]


# ---------------------------------------------------------------------------
# (d) D3 repo_root consistency check
# ---------------------------------------------------------------------------


def test_d3_same_repo_no_mismatch(fleet_repo):
    """D3: params.repo_root = fleet_repo.root (same repo worktree) → None (no mismatch)."""
    mismatch = check_repo_root(str(fleet_repo.root), fleet_repo.common_dir)
    assert mismatch is None, (
        f"Same-repo param_root must not produce a mismatch; got: {mismatch!r}"
    )


def test_d3_trailing_slash_not_mismatch(fleet_repo):
    """D3: trailing slash on param_root is cosmetic — NOT a genuine mismatch."""
    param_with_slash = str(fleet_repo.root) + "/"
    mismatch = check_repo_root(param_with_slash, fleet_repo.common_dir)
    assert mismatch is None, (
        f"Trailing slash must not be treated as a mismatch; got: {mismatch!r}"
    )


def test_d3_symlink_resolution_not_mismatch(tmp_path):
    """D3: symlink to the same repo worktree is NOT a genuine mismatch.

    Path.resolve() normalises both sides — the symlinked path resolves to the same
    canonical git common dir.  Covers /var↔/private/var on macOS and equivalent
    symlink differences.
    """
    from coordinator_core.lifecycle import git_common_dir

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args), cwd=str(repo_root), capture_output=True, check=True
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "test@test.com")
    _git("config", "user.name", "Test")
    _git("config", "commit.gpgsign", "false")
    _git("commit", "--allow-empty", "-m", "initial")

    common_dir = git_common_dir(repo_root)

    # Create a symlink pointing to repo_root
    symlink = tmp_path / "repo-link"
    os.symlink(str(repo_root), str(symlink))

    mismatch = check_repo_root(str(symlink), common_dir)
    assert mismatch is None, (
        f"Symlink to the same repo must not produce a mismatch; got: {mismatch!r}"
    )


def test_d3_genuine_mismatch_returns_reason(fleet_repo, tmp_path):
    """D3: param_root pointing to a different git repo → returns 'repo_root-mismatch' reason."""
    other_repo = tmp_path / "other"
    other_repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args), cwd=str(other_repo), capture_output=True, check=True
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "o@o.com")
    _git("config", "user.name", "Other")
    _git("config", "commit.gpgsign", "false")
    _git("commit", "--allow-empty", "-m", "init")

    # Handler is in fleet_repo; param_root says other_repo → genuine mismatch
    mismatch = check_repo_root(str(other_repo), fleet_repo.common_dir)
    assert mismatch is not None, "Different repos must produce a mismatch reason"
    assert "repo_root-mismatch" in mismatch, (
        f"Mismatch reason must contain 'repo_root-mismatch'; got: {mismatch!r}"
    )


def test_d3_none_param_root_no_check():
    """D3: param_root=None → check_repo_root returns None (optional param absent, no check)."""
    mismatch = check_repo_root(None, Path("/some/dummy/.git"))
    assert mismatch is None, "Absent param_root must not trigger a mismatch check"


# ---------------------------------------------------------------------------
# (e) archive_and_commit: private GIT_INDEX_FILE isolation + both-sides pathspec
# ---------------------------------------------------------------------------


def test_archive_and_commit_private_index_isolation(fleet_repo):
    """archive_and_commit uses a private GIT_INDEX_FILE — main index is not touched.

    Proof:
    1. Stage an untracked sentinel into the MAIN index before calling archive_and_commit.
    2. Run archive_and_commit for a tracked bug (uses private index).
    3. Assert: the sentinel is STILL staged in the main index after the op
       (archive_and_commit did not consume or modify main-index staging).
    4. Assert: the archived bug appears in git log (private-index commit succeeded).
    5. Assert: the sentinel does NOT appear in the fleet commit (isolation).

    This confirms: a concurrent 'git add -A' on the main index cannot absorb the
    fleet op's staging, and the fleet op's staging cannot absorb main-index content.
    """
    # Seed a tracked bug to archive
    bug_path = fleet_repo.seed_bug("2026-04-01-isolation-bug.yaml", "closed")
    bug_id = fleet_repo.repo_rel(bug_path)
    bug_dst = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / bug_path.name
    bug_dst_rel = bug_dst.relative_to(fleet_repo.root).as_posix()

    # Stage an untracked sentinel into the MAIN index
    sentinel = fleet_repo.root / "state" / "bug-backlog" / "sentinel-unrelated.yaml"
    sentinel.write_text("title: sentinel\nstatus: open\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", str(sentinel)],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )

    # Verify sentinel is staged in main index before the op
    status_before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert "sentinel-unrelated.yaml" in status_before, (
        "Sentinel must be staged in main index before archive_and_commit"
    )

    # Run archive_and_commit (uses private GIT_INDEX_FILE)
    moves = [Move(src=bug_path, dst=bug_dst, candidate_id=bug_id)]
    acted, failed = _run(archive_and_commit(
        fleet_repo.root,
        moves,
        "archive(bug-backlog): private-index isolation test",
    ))

    assert len(acted) == 1, (
        f"archive_and_commit must act on the bug; got acted={acted}, failed={failed}"
    )
    assert len(failed) == 0

    # Main index must STILL have the sentinel staged (archive_and_commit did not touch it)
    status_after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert "sentinel-unrelated.yaml" in status_after, (
        "Sentinel must remain staged in main index after archive_and_commit — "
        "the private GIT_INDEX_FILE must not have interfered with the main index"
    )

    # The fleet commit must contain the bug's src and dst paths (both sides)
    log_names = _git_log_names_no_renames(fleet_repo, 1)
    assert bug_id in log_names, (
        f"Bug src must appear in the fleet commit; got {log_names}"
    )
    assert bug_dst_rel in log_names, (
        f"Bug dst must appear in the fleet commit; got {log_names}"
    )

    # The sentinel must NOT appear in the fleet commit (private index isolation)
    sentinel_rel = fleet_repo.repo_rel(sentinel)
    assert sentinel_rel not in log_names, (
        f"Sentinel staged only in main index must NOT appear in fleet commit; got {log_names}"
    )


def test_archive_and_commit_restage_src_carries_op_authored_pre_move_write(fleet_repo):
    """Move.restage_src=True: content the CALLER wrote to src immediately before
    building the Move reaches the archival commit (C4c fix, 2026-07-27).

    Regression net for the C4 (2026-07-26) FORWARD-B fix's collateral break:
    plain `git mv` re-keys the private index's read-tree-HEAD blob for src to
    dst — it does not rehash src's current on-disk content — so a caller that
    stamps src's own file right before archiving it (e.g.
    archive_handoffs._stamp_heir_shipped) would see that stamp silently
    dropped from the commit under the no-pathspec form, and the op would exit
    with the write still dirty in the working tree.

    Proof:
    1. Seed a tracked bug, commit it (HEAD content == "status: closed").
    2. Overwrite src's on-disk content directly (simulating an op-authored
       pre-move mutation) WITHOUT committing — src is now dirty relative to
       HEAD, exactly as _stamp_heir_shipped leaves a handoff dirty.
    3. Call archive_and_commit with Move(..., restage_src=True).
    4. Assert: the COMMITTED content at dst is the mutated content (not the
       stale HEAD content) and the tree is clean afterward — the write landed
       in the commit, not left dangling dirty.
    """
    bug_path = fleet_repo.seed_bug("2026-04-03-restage-src-bug.yaml", "closed")
    bug_id = fleet_repo.repo_rel(bug_path)
    dst = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / bug_path.name
    dst_rel = dst.relative_to(fleet_repo.root).as_posix()

    op_authored_content = "title: restage-src bug\nstatus: closed\nnote: op-authored-stamp\n"
    bug_path.write_text(op_authored_content, encoding="utf-8")

    moves = [Move(src=bug_path, dst=dst, candidate_id=bug_id, restage_src=True)]
    acted, failed = _run(archive_and_commit(
        fleet_repo.root,
        moves,
        "archive(bug-backlog): restage_src op-authored content test",
    ))

    assert acted == [{"id": bug_id, "archived": True}], (
        f"restage_src move must archive cleanly; acted={acted} failed={failed}"
    )
    assert failed == []

    committed = subprocess.run(
        ["git", "show", f"HEAD:{dst_rel}"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    assert committed == op_authored_content, (
        "the op-authored pre-move write must reach the archival commit, not "
        f"the stale read-tree-HEAD blob; got committed={committed!r}"
    )

    assert fleet_repo.git_status_clean(), (
        "the op-authored write must be fully committed — no dirty residue "
        "left in the working tree after archive_and_commit returns"
    )


def test_archive_and_commit_restage_src_false_default_unaffected(fleet_repo):
    """Move.restage_src defaults to False — an op-authored pre-move write on a
    move that does NOT opt in is still dropped from the commit (documents the
    baseline restage_src exists to fix, so a future accidental default flip is
    caught by this test instead of silently changing every non-heir caller)."""
    bug_path = fleet_repo.seed_bug("2026-04-04-no-restage-bug.yaml", "closed")
    bug_id = fleet_repo.repo_rel(bug_path)
    dst = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / bug_path.name
    dst_rel = dst.relative_to(fleet_repo.root).as_posix()

    stale_head_content = bug_path.read_text(encoding="utf-8")
    bug_path.write_text("title: no-restage bug\nstatus: closed\nnote: dropped\n", encoding="utf-8")

    moves = [Move(src=bug_path, dst=dst, candidate_id=bug_id)]  # restage_src default False
    acted, failed = _run(archive_and_commit(
        fleet_repo.root,
        moves,
        "archive(bug-backlog): restage_src default-False baseline",
    ))

    assert acted == [{"id": bug_id, "archived": True}]
    assert failed == []

    committed = subprocess.run(
        ["git", "show", f"HEAD:{dst_rel}"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    assert committed == stale_head_content, (
        "without restage_src, the commit must carry the private index's "
        f"read-tree-HEAD blob, not the on-disk mutation; got {committed!r}"
    )


def test_archive_and_commit_restage_src_mv_failure_does_not_leak_into_commit(fleet_repo):
    """A restage_src move whose git mv FAILS must not leak its staged src content
    into the batch commit (2026-07-27 fix for the FORWARD-B laundering reopened
    by restage_src, reviewer-reproduced against the pre-fix code).

    Pre-fix mechanism: the restage_src `git add -- src` runs BEFORE git mv and
    succeeds unconditionally; if the following git mv then fails (dst
    collision here), the code appended to failed[] and moved on WITHOUT
    resetting that stray private-index entry. Since the batch commit has no
    trailing pathspec, it commits the private index as-is — so the failed
    candidate's op-authored content still rides into HEAD at its ORIGINAL
    path, misattributed under a DIFFERENT candidate's archival subject, while
    the caller was told (via failed[]) that candidate never got archived.

    Proof: seed two bugs. Bug A's Move has restage_src=True and a dst that
    already exists (forcing git mv to fail without touching disk). Bug B's
    Move succeeds normally so the batch produces a commit. Assert Bug A
    reports failed, and the commit does NOT carry Bug A's op-authored content
    at Bug A's original path — it still matches the pre-batch HEAD blob.
    """
    bug_a = fleet_repo.seed_bug("2026-04-05-restage-mv-fail-a.yaml", "closed")
    bug_a_id = fleet_repo.repo_rel(bug_a)
    original_content = bug_a.read_text(encoding="utf-8")

    bug_b = fleet_repo.seed_bug("2026-04-05-restage-mv-fail-b.yaml", "closed")
    bug_b_id = fleet_repo.repo_rel(bug_b)

    dst_a = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / bug_a.name
    dst_a.parent.mkdir(parents=True, exist_ok=True)
    dst_a.write_text("pre-existing collision at dst", encoding="utf-8")  # forces git mv to fail

    dst_b = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / bug_b.name

    op_authored_content = "title: restage-mv-fail bug\nstatus: closed\nnote: op-authored-stamp\n"
    bug_a.write_text(op_authored_content, encoding="utf-8")

    moves = [
        Move(src=bug_a, dst=dst_a, candidate_id=bug_a_id, restage_src=True),
        Move(src=bug_b, dst=dst_b, candidate_id=bug_b_id),
    ]
    acted, failed = _run(archive_and_commit(
        fleet_repo.root,
        moves,
        "archive(bug-backlog): restage_src + mv-failure interleaving test",
    ))

    assert acted == [{"id": bug_b_id, "archived": True}], (
        f"only Bug B should have archived cleanly; acted={acted}"
    )
    failed_ids = {f["id"] for f in failed}
    assert bug_a_id in failed_ids, f"Bug A's failed mv must be reported; failed={failed}"

    committed_at_src = subprocess.run(
        ["git", "show", f"HEAD:{bug_a_id}"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    assert committed_at_src == original_content, (
        "Bug A's op-authored content must NOT ride into the batch commit at its "
        f"original path after its git mv failed; got committed={committed_at_src!r} "
        f"(expected unchanged original={original_content!r})"
    )


def test_archive_and_commit_c9_cleanup_does_not_remove_shared_dir_with_sibling_survivor(fleet_repo, monkeypatch):
    """Review: code-reviewer F2 — C9's per-move `created_dirs_by_id` tracking must not
    let one move's cleanup rmdir a shared ancestor directory that still holds a SIBLING
    move's file.

    Shape exercised: two moves (A, B) into a NEW shared subdirectory that neither move's
    dst.parent currently has — A is the first to create it (so only A's
    `created_dirs_by_id` entry is non-empty; B's dst.parent already exists by the time
    B runs, so B's own entry is `[]`, per the existing per-candidate-id design). Both
    git-mv steps succeed, but the batch commit is forced to fail, taking the
    commit-failure reversal branch (`_common.py` :1128-1144): every acted move's disk
    rename is reversed. A's reversal succeeds, so cleanup is ATTEMPTED for A's tracked
    shared dir — but B's reversal is forced to fail (mocked `Path.rename`), so B's file
    is still sitting in that shared directory when A's cleanup runs. Asserts the
    directory survives (still containing B's un-reversed file) rather than being
    rmdir'd out from under it — the exact defensive `except OSError: break` path in
    `_cleanup_created_dirs`, asserted against real filesystem state rather than merely
    reasoned about.
    """
    bug_a = fleet_repo.seed_bug("2026-04-06-c9-shared-a.yaml", "closed")
    bug_a_id = fleet_repo.repo_rel(bug_a)
    bug_b = fleet_repo.seed_bug("2026-04-06-c9-shared-b.yaml", "closed")
    bug_b_id = fleet_repo.repo_rel(bug_b)

    shared_dir = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / "shared"
    assert not shared_dir.exists(), "precondition: shared_dir must not pre-exist"
    dst_a = shared_dir / bug_a.name
    dst_b = shared_dir / bug_b.name

    moves = [
        Move(src=bug_a, dst=dst_a, candidate_id=bug_a_id),
        Move(src=bug_b, dst=dst_b, candidate_id=bug_b_id),
    ]

    real_create = asyncio.create_subprocess_exec

    class _FakeFailedCommitProc:
        returncode = 128

        async def communicate(self):
            return b"", b"fatal: forced commit failure for C9 shared-dir test"

    async def _fake_create(*args, **kwargs):
        if "commit" in args and args[args.index("commit") + 1] == "-m":
            return _FakeFailedCommitProc()
        return await real_create(*args, **kwargs)

    real_rename = Path.rename

    def _fake_rename(self, target):
        if self == dst_b:
            raise OSError("forced reversal failure for C9 shared-dir test")
        return real_rename(self, target)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    monkeypatch.setattr(Path, "rename", _fake_rename)

    acted, failed = _run(archive_and_commit(
        fleet_repo.root, moves, "archive(bug-backlog): C9 shared-dir cleanup test",
    ))

    assert acted == []
    failed_ids = {f["id"] for f in failed}
    assert failed_ids == {bug_a_id, bug_b_id}

    # B's reversal was forced to fail — its file is still at dst, inside the shared dir.
    assert dst_b.exists(), "B's un-reversed file must still be at dst"
    # A's reversal succeeded for real.
    assert bug_a.exists(), "A's reversal must have restored src on disk"
    assert not dst_a.exists()

    # The shared directory must survive — it still holds B's file. If A's cleanup
    # rmdir'd it out from under B, this would be gone (or dst_b would be orphaned).
    assert shared_dir.exists(), (
        "C9 cleanup must not remove a shared ancestor dir that still holds a "
        "sibling move's un-reversed file"
    )
    assert dst_b.exists()


def test_archive_and_commit_both_sides_pathspec(fleet_repo):
    """archive_and_commit commits with BOTH src AND dst in the pathspec (AC4/DR-211 D3).

    A dst-only pathspec would leave the staged deletion of src orphaned in the index
    (scoped-safety-commits.md § "Rename pathspec must include both sides").

    Uses --no-renames to verify both paths are explicitly in the commit rather than
    relying on rename-detection collapsing two entries into one.

    After a successful archive:
    - src no longer on disk
    - dst on disk
    - git log -1 (--no-renames) contains BOTH src AND dst
    - git status --porcelain is clean (no orphaned staged deletions)
    """
    bug_path = fleet_repo.seed_bug("2026-04-02-pathspec-bug.yaml", "closed")
    bug_id = fleet_repo.repo_rel(bug_path)
    dst = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / bug_path.name
    dst_rel = dst.relative_to(fleet_repo.root).as_posix()

    moves = [Move(src=bug_path, dst=dst, candidate_id=bug_id)]
    acted, failed = _run(archive_and_commit(
        fleet_repo.root,
        moves,
        "archive(bug-backlog): both-sides pathspec test",
    ))

    assert len(acted) == 1
    assert len(failed) == 0

    # Both src AND dst must appear in git log (--no-renames)
    log_names = _git_log_names_no_renames(fleet_repo, 1)
    assert bug_id in log_names, (
        f"src must appear in git log (--no-renames pathspec); names={log_names}"
    )
    assert dst_rel in log_names, (
        f"dst must appear in git log (--no-renames pathspec); names={log_names}"
    )

    # Clean index — no orphaned staged deletions (AC4/AC10).
    assert fleet_repo.git_status_clean(), (
        "git status must be clean after archive — both-sides pathspec must not leave "
        "orphaned staged deletions (AC4/AC10)"
    )


def test_archive_and_commit_scoped_commit_no_extras(fleet_repo):
    """The fleet commit must only contain the moved file's paths — no unrelated paths.

    Seeds two bugs; archives only one; verifies the other does not appear in the commit.
    """
    archive_path = fleet_repo.seed_bug("2026-04-03-archive-me.yaml", "closed")
    leave_alone = fleet_repo.seed_bug("2026-04-04-leave-alone.yaml", "closed")

    archive_id = fleet_repo.repo_rel(archive_path)
    leave_alone_id = fleet_repo.repo_rel(leave_alone)
    dst = fleet_repo.root / "archive" / "bug-backlog" / "2026-04" / archive_path.name
    dst_rel = dst.relative_to(fleet_repo.root).as_posix()

    # Only archive one of them
    moves = [Move(src=archive_path, dst=dst, candidate_id=archive_id)]
    acted, failed = _run(archive_and_commit(
        fleet_repo.root,
        moves,
        "archive(bug-backlog): scoped commit test",
    ))

    assert len(acted) == 1
    log_names = _git_log_names_no_renames(fleet_repo, 1)

    # Only archive_id (src) and dst_rel should appear
    assert archive_id in log_names
    assert dst_rel in log_names
    # The leave-alone bug must NOT appear in the commit
    assert leave_alone_id not in log_names, (
        f"Unarchived bug must not appear in fleet commit; names={log_names}"
    )


# ---------------------------------------------------------------------------
# GAP-6 regression: -c commit.gpgsign=false neutralises gpgsign=true repos
# ---------------------------------------------------------------------------


def test_archive_and_commit_gpgsign_override(tmp_path):
    """archive_and_commit succeeds even when the repo has commit.gpgsign=true.

    GAP-6 regression: the ``-c commit.gpgsign=false`` flag in the git commit
    invocation must neutralise the repo's gpgsign=true config.  Without the fix
    git would try to sign using the nonexistent key ``INVALID_KEY_ID_DOESNT_EXIST``,
    gpg would return non-zero ("No public key"), and archive_and_commit would
    reverse all moves and return everything in failed[].

    Proof that the test is a true regression gate (by reasoning):
    - WITHOUT fix: git commit runs without ``-c commit.gpgsign=false``; git reads
      commit.gpgsign=true + user.signingkey=INVALID_KEY_ID_DOESNT_EXIST from
      the repo config; invokes gpg; gpg returns non-zero; commit fails;
      archive_and_commit reverses moves → acted==[], len(failed)==1 → assertion fails.
    - WITH fix: git ignores gpgsign for that invocation; commit succeeds;
      acted==[{"id":..., "archived":True}] → assertion passes.

    The signing key is set to a nonsensical ID so gpg fails deterministically
    without hanging (no passphrase prompt — gpg exits immediately on missing key).
    """
    repo_root = tmp_path / "gpgsign-repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo_root),
            capture_output=True,
            check=True,
        )

    # Init and do all setup commits while gpgsign is still false.
    _git("init", "-b", "main")
    _git("config", "user.email", "gpg-test@claude-klabauter.test")
    _git("config", "user.name", "GPG Test")
    _git("config", "commit.gpgsign", "false")

    # Minimal directory skeleton needed for git mv to locate tracked paths.
    for d in ("state/bug-backlog", "archive/bug-backlog/2026-07"):
        (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / d / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    # Seed a bug file and commit it (while gpgsign is still false).
    bug_path = repo_root / "state" / "bug-backlog" / "2026-07-07-gpg-test-bug.yaml"
    bug_path.write_text(
        'title: "GPG test bug"\nstatus: closed\ncreated: 2026-07-07\n',
        encoding="utf-8",
    )
    _git("add", str(bug_path))
    _git("commit", "-m", "add bug 2026-07-07-gpg-test-bug.yaml")

    # Now flip the repo to require GPG signing with an invalid key.
    # This simulates a developer machine with commit.gpgsign=true + a passphrase-
    # protected (or simply absent) key — the exact environment that triggers GAP-6.
    _git("config", "commit.gpgsign", "true")
    _git("config", "user.signingkey", "INVALID_KEY_ID_DOESNT_EXIST")

    bug_id = bug_path.relative_to(repo_root).as_posix()
    bug_dst = repo_root / "archive" / "bug-backlog" / "2026-07" / bug_path.name

    moves = [Move(src=bug_path, dst=bug_dst, candidate_id=bug_id)]
    acted, failed = _run(archive_and_commit(
        repo_root,
        moves,
        "archive(bug-backlog): GAP-6 gpgsign override regression test",
    ))

    # The commit MUST succeed: -c commit.gpgsign=false in archive_and_commit
    # overrides the repo's signing config for this single invocation.
    assert len(acted) == 1, (
        f"archive_and_commit must succeed despite commit.gpgsign=true + invalid key "
        f"(GAP-6); got acted={acted}, failed={failed}"
    )
    assert len(failed) == 0
    assert acted[0]["id"] == bug_id
    assert acted[0]["archived"] is True


# ---------------------------------------------------------------------------
# main_worktree_root helper
# ---------------------------------------------------------------------------


def test_main_worktree_root_returns_common_dir_parent(fleet_repo):
    """main_worktree_root(common_dir) returns common_dir.parent for standard .git layout."""
    result = main_worktree_root(fleet_repo.common_dir)
    assert result == fleet_repo.common_dir.parent
    assert result == fleet_repo.root


def test_main_worktree_root_is_directory(fleet_repo):
    """main_worktree_root result is an existing directory.

    AC11 call-site-unaffected evidence: this pre-existing test exercises
    _common.main_worktree_root through the re-export post-C10-relocation and
    would catch a behavior regression; it discharges AC11's second clause
    (an existing call site is unaffected by the move).
    """
    result = main_worktree_root(fleet_repo.common_dir)
    assert result.is_dir(), f"main_worktree_root must be a directory; got {result}"


def test_main_worktree_root_reexport_is_same_function_object():
    """ops.fleet._common.main_worktree_root is a re-export of lifecycle.main_worktree_root,
    not a second implementation — the C10 relocation must not fork the function."""
    from coordinator_core import lifecycle
    from coordinator_core.ops.fleet import _common

    assert _common.main_worktree_root is lifecycle.main_worktree_root


# ---------------------------------------------------------------------------
# _make_git_env — session-id forwarding fix (break-class: stale Session-Id:
# trailer stamped by prepare-commit-msg falling through to the sentinel
# because CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID were stripped).
# ---------------------------------------------------------------------------

def test_make_git_env_forwards_session_id_vars(monkeypatch):
    """CLAUDE_SESSION_ID and CLAUDE_CODE_SESSION_ID are forwarded so the
    prepare-commit-msg hook can stamp the real ambient session id instead of
    falling through to the stale .git/coordinator-sessions sentinel.

    Review: code-reviewer (2026-07-21, Finding 1) — also pins that forwarding
    is EXACT-MATCH tuple membership, not prefix/substring: a var whose name
    merely widens on top of a session-id key (CLAUDE_SESSION_ID_EVIL) must
    NOT be forwarded. Guards against a future refactor to
    key.startswith((...)) silently widening the forwarded set.
    """
    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-abc-123")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-def-456")
    monkeypatch.setenv("CLAUDE_SESSION_ID_EVIL", "attacker-value")

    env = _make_git_env()

    assert env.get("CLAUDE_SESSION_ID") == "session-abc-123"
    assert env.get("CLAUDE_CODE_SESSION_ID") == "session-def-456"
    assert "CLAUDE_SESSION_ID_EVIL" not in env


def test_make_git_env_still_strips_execution_redirect_vectors(monkeypatch):
    """Regression guard: the session-id forwarding fix must not widen the
    security perimeter — all four named execution-redirect vectors
    (GIT_SSH_COMMAND, GIT_EXEC_PATH, GIT_PROXY_COMMAND, GIT_TEMPLATE_DIR)
    remain stripped.

    Review: code-reviewer (2026-07-21, Finding 2) — completes the pin to all
    four vectors named in the module docstring; previously only 2 of 4 were
    exercised.
    """
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o ProxyCommand=evil")
    monkeypatch.setenv("GIT_EXEC_PATH", "/tmp/evil-exec-path")
    monkeypatch.setenv("GIT_PROXY_COMMAND", "evil-proxy-command")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", "/tmp/evil-template")

    env = _make_git_env()

    assert "GIT_SSH_COMMAND" not in env
    assert "GIT_EXEC_PATH" not in env
    assert "GIT_PROXY_COMMAND" not in env
    assert "GIT_TEMPLATE_DIR" not in env


def test_make_git_env_forwards_home_path_and_sets_index_file(monkeypatch):
    """HOME/PATH forwarding and idx_path -> GIT_INDEX_FILE behavior are
    unchanged by the session-id forwarding fix."""
    monkeypatch.setenv("HOME", "/Users/testuser")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = _make_git_env(idx_path="/tmp/private-index")

    assert env.get("HOME") == "/Users/testuser"
    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("GIT_INDEX_FILE") == "/tmp/private-index"


def test_make_git_env_forwards_d1_extra_vars(monkeypatch):
    """D1 (2026-07-28, break-class): `git commit` execs this repo's own
    prepare-commit-msg/post-commit hooks two processes down, and those hooks
    (plus git-for-Windows itself) need COORDINATOR_SETTINGS_HOME and a raft
    of Windows platform vars that the pre-fix allowlist silently stripped.
    Pins that the widened set actually reaches the child env.
    """
    extra_vars = {
        "COORDINATOR_SETTINGS_HOME": "/custom/settings-home",
        "USERPROFILE": "C:\\Users\\testuser",
        "HOMEDRIVE": "C:",
        "HOMEPATH": "\\Users\\testuser",
        "SYSTEMROOT": "C:\\Windows",
        "PATHEXT": ".COM;.EXE;.BAT",
        "TEMP": "C:\\Users\\testuser\\AppData\\Local\\Temp",
        "TMP": "C:\\Users\\testuser\\AppData\\Local\\Temp",
        "TMPDIR": "/tmp",
        "APPDATA": "C:\\Users\\testuser\\AppData\\Roaming",
        "LOCALAPPDATA": "C:\\Users\\testuser\\AppData\\Local",
        "MSYSTEM": "MINGW64",
        "OS": "Windows_NT",
    }
    for key, val in extra_vars.items():
        monkeypatch.setenv(key, val)

    env = _make_git_env()

    for key, val in extra_vars.items():
        assert env.get(key) == val, f"{key} was not forwarded"


def test_make_git_env_windows_shaped_home_var_survives(monkeypatch):
    """Windows-shaped case: HOME unset (as it normally is on native Windows),
    USERPROFILE set — proves a usable home var reaches the child env even
    when the POSIX-shaped HOME is absent, so git-for-Windows can still find
    its global .gitconfig.
    """
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\testuser")

    env = _make_git_env()

    assert "HOME" not in env
    assert env.get("USERPROFILE") == "C:\\Users\\testuser"


def test_make_git_env_d1_widening_still_strips_execution_redirect_vectors(monkeypatch):
    """The D1 widening must not reintroduce the redirect-vector strip —
    re-pins all four vectors alongside the new extra-forward set being
    present, to catch a future refactor that widens by prefix instead of
    exact key membership."""
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o ProxyCommand=evil")
    monkeypatch.setenv("GIT_EXEC_PATH", "/tmp/evil-exec-path")
    monkeypatch.setenv("GIT_PROXY_COMMAND", "evil-proxy-command")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", "/tmp/evil-template")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "/custom/settings-home")
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")

    env = _make_git_env()

    assert "GIT_SSH_COMMAND" not in env
    assert "GIT_EXEC_PATH" not in env
    assert "GIT_PROXY_COMMAND" not in env
    assert "GIT_TEMPLATE_DIR" not in env
    assert env.get("COORDINATOR_SETTINGS_HOME") == "/custom/settings-home"
    assert env.get("SYSTEMROOT") == "C:\\Windows"


def test_make_git_env_does_not_forward_comspec(monkeypatch):
    """Review: code-reviewer F1 (2026-07-28) — COMSPEC names the interpreter
    Windows uses for shell=True/os.system()/ShellExecute calls, the same
    "names an executable to run" shape GIT_SSH_COMMAND is stripped for.
    Checked (not assumed): no consumer downstream of `git commit` in the D1
    chain reads COMSPEC, and this repo's subprocess policy is list-argv only
    (no shell=True/os.system anywhere in production code) — so it was
    dropped from _EXTRA_FORWARD_ENV_KEYS rather than kept-and-justified.
    Pins the negative so a future refactor doesn't silently re-add it."""
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\system32\\cmd.exe")

    env = _make_git_env()

    assert "COMSPEC" not in env

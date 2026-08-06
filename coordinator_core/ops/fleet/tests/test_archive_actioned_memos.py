"""
coordinator_core.ops.fleet.tests.test_archive_actioned_memos

Tests for the fleet.archive_actioned_memos op.

Import guard: coordinator_core.ops.fleet.archive_actioned_memos MUST be imported at
module load time to fire the @register_op("fleet.archive_actioned_memos") side-effect
and populate _REGISTRY.  Without this import the decorator has not run and any
completeness assertion passes vacuously over an empty (or stale) registry
(lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) Positive floor / import guard — registry non-empty, op registered by name.
  (b) dry_run:true — actioned memo appears in candidates with required fields;
      non-actioned excluded; live-claim excluded; mutates nothing.
  (c) dry_run:false (act) — git-mv inbox → archive/ (FLAT — no YYYY-MM/ subdirs);
      source gone, dest present, git log --no-renames shows BOTH src and dst,
      git status --porcelain clean.
  (d) Idempotent replay — re-dispatch same candidate_ids after archive →
      skipped reason "already-archived", exit_code:0, no second commit.
  (e) Act-time terminality drift (D1 re-verify) — memo status changed from
      "actioned" to non-terminal between preview and act → skipped, NOT moved,
      source preserved.
  (f) Liveness-guard skip — actioned memo with live memo-claim dir →
      excluded from dry_run candidates; skipped on act.
  (g) Partial failure → exit_code:2; committed memo succeeds, untracked memo
      (exists on disk but not git-tracked) fails git-mv; main git index has no
      staged residue.
  (j) Disposition-field guard — regression net for the 70a54f9b/47bc7eed/
      989bf41b/ca9d3e83 unread-swallow incident (34 memos hand-flipped to
      status:actioned with zero disposition fields, archived unread). A
      status:actioned memo with NO disposition field is skipped with
      _REASON_ACTIONED_NO_DISPOSITION and NOT moved; a status:actioned memo
      with EACH of the four disposition fields in turn IS archived.

Spec backlinks:
  - Plan (C7): docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C7
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md D1-D4
  - Incident report: cross-repo memo to example-cockpit-repo, 2026-07-26 (four sweep
    commits archiving 34 memos with no decision/decision_note/realized_by/
    actioned_note field)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# Lesson: 2026-07-04-universal-registry-completeness-tests-ov.yaml
# ---------------------------------------------------------------------------
import coordinator_core.ops.fleet  # noqa: F401 — triggers package __init__
import coordinator_core.ops.fleet.archive_actioned_memos  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.archive_actioned_memos import (
    _DISPOSITION_FIELDS,
    _handler,
)

# Positive floor assertion — the op must be registered before ANY test runs.
# Checks len >= 1 (floor) then the specific op name (completeness).
_OP_NAME = "fleet.archive_actioned_memos"
_fleet_registered_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
assert len(_fleet_registered_ops) >= 1, (
    "import guard failed: no fleet.* ops in _REGISTRY — "
    "check coordinator_core.ops.fleet.archive_actioned_memos import"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.fleet.archive_actioned_memos @register_op did not fire"
)

# Patch target for the liveness function imported into the handler module.
_LIVE_CLAIM_PATCH = "coordinator_core.ops.fleet.archive_actioned_memos.cs_claim_holder_live"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_params(dry_run: bool, candidate_ids=None) -> dict:
    """Build a minimal valid fleet.archive_actioned_memos params dict."""
    p: dict = {"mode": "already-terminal", "dry_run": dry_run}
    if candidate_ids is not None:
        p["candidate_ids"] = candidate_ids
    return p


def _ensure_memo_dirs(fleet_repo) -> None:
    """Ensure cross-repo/inbox/ and cross-repo/archive/ exist and are committed.

    Called lazily by _seed_memo; safe to call multiple times (mkdir parents=True,
    exist_ok=True guards the second call in a test session).
    """
    inbox = fleet_repo.root / "cross-repo" / "inbox"
    archive = fleet_repo.root / "cross-repo" / "archive"
    needs_commit = False
    for d in (inbox, archive):
        keeper = d / ".gitkeep"
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            keeper.write_text("", encoding="utf-8")
            fleet_repo._git("add", str(keeper))
            needs_commit = True
    if needs_commit:
        fleet_repo._git("commit", "-m", "chore: add cross-repo inbox/archive dirs")


def _seed_memo(
    fleet_repo,
    name: str,
    status: str,
    title: str = "Test Memo",
    commit: bool = True,
    disposition_field: Optional[str] = None,
    no_disposition: bool = False,
) -> Path:
    """Write an inbox memo with the given frontmatter status.

    commit=True (default): stages and commits the file so it is git-tracked
    (required for git mv to succeed).

    commit=False: writes the file to disk ONLY — untracked by git; git mv will
    fail on this file (used for partial-failure tests).

    disposition_field: which _DISPOSITION_FIELDS member to attach when
    status == "actioned" (default: the first field, "decision" — mirrors what
    memo_transition._action always attaches on a real actioned flip). Ignored
    for any other status.

    no_disposition=True: write status:actioned WITHOUT any disposition field —
    the exact shape of a hand `Edit` that bypasses memo_transition (the
    70a54f9b/47bc7eed/989bf41b/ca9d3e83 unread-swallow incident this module's
    _is_terminal fix closes). Every OTHER "actioned" seed in this file attaches
    a disposition field by default so the pre-existing archival-path tests
    keep exercising the programmatically-actioned shape, not the hand-edited one.
    """
    _ensure_memo_dirs(fleet_repo)
    path = fleet_repo.root / "cross-repo" / "inbox" / name
    frontmatter_lines = [
        "---",
        f'title: "{title}"',
        f"status: {status}",
        "created: 2026-07-06",
    ]
    if status == "actioned" and not no_disposition:
        field = disposition_field or _DISPOSITION_FIELDS[0]
        frontmatter_lines.append(f'{field}: "test disposition for {field}"')
    frontmatter_lines.append("---")
    content = "\n".join(frontmatter_lines) + textwrap.dedent(f"""

        # {title}

        Body.
    """)
    path.write_text(content, encoding="utf-8")
    if commit:
        fleet_repo._git("add", str(path))
        fleet_repo._git("commit", "-m", f"add memo {name}")
    return path


def _git_log_names_no_renames(fleet_repo, n: int = 1):
    """Return file paths in the last n commits with rename-detection disabled.

    Uses --no-renames so git mv is shown as BOTH deleted source AND added
    destination — required for DR-211 D3 both-sides-pathspec verification.
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


def _staged_names(fleet_repo):
    """Return the list of staged (index) file paths from git diff --cached --name-only."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
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
# (a) Positive floor / import guard
# ---------------------------------------------------------------------------


def test_op_registered():
    """fleet.archive_actioned_memos must be in the registry after the import guard fires.

    The module-level assertions already cover this; this test makes the contract
    explicit and produces a named failure in pytest output.
    """
    fleet_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
    assert len(fleet_ops) >= 1, (
        f"floor: at least one fleet.* op must be registered; got {sorted(fleet_ops)}"
    )
    assert _OP_NAME in _REGISTRY, (
        f"{_OP_NAME!r} must be registered; present ops: {sorted(fleet_ops)}"
    )


# ---------------------------------------------------------------------------
# (b) dry_run:true — candidates, exclusions, mutates-nothing
# ---------------------------------------------------------------------------


def test_dry_run_returns_actioned_candidate_with_required_fields(fleet_repo):
    """dry_run:true returns actioned memos in candidates[] with all required fields."""
    _seed_memo(fleet_repo, "2026-07-06-alpha.md", "actioned", title="Alpha Memo")
    # Non-terminal memo must be excluded.
    _seed_memo(fleet_repo, "2026-07-06-active.md", "in_progress", title="Active Memo")

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    candidates = result["candidates"]

    ids = [c["id"] for c in candidates]
    assert "cross-repo/inbox/2026-07-06-alpha.md" in ids, (
        "actioned memo must appear in dry_run candidates"
    )
    assert "cross-repo/inbox/2026-07-06-active.md" not in ids, (
        "non-actioned memo must be excluded from candidates"
    )

    # Verify required fields on the actioned candidate.
    alpha = next(c for c in candidates if "alpha" in c["id"])
    for field in ("id", "title", "status", "family", "terminal_since", "note"):
        assert field in alpha, f"candidate missing required field {field!r}"
    assert alpha["status"] == "actioned"
    assert alpha["family"] == "memo"


def test_dry_run_mutates_nothing(fleet_repo):
    """dry_run:true must not move any file or create any commit."""
    _seed_memo(fleet_repo, "2026-07-06-readonly.md", "actioned")
    commits_before = fleet_repo.git_log_names(n=5)

    _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    assert fleet_repo.path_exists("cross-repo/inbox/2026-07-06-readonly.md"), (
        "dry_run:true must NOT move the source file"
    )
    assert fleet_repo.git_status_clean(), (
        "dry_run:true must not dirty the git index"
    )
    assert fleet_repo.git_log_names(n=5) == commits_before, (
        "dry_run:true must not create any commits"
    )


def test_dry_run_excludes_live_memo_claim(fleet_repo):
    """Actioned memo with a live memo-claim dir is excluded from dry_run candidates.

    Mirrors cs_sweep_actioned_memos L1950 liveness guard.
    """
    _seed_memo(fleet_repo, "2026-07-06-claimed.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-claimed.md"

    # Create the claim dir so the liveness check is triggered.
    claim_dir = fleet_repo.common_dir / "coordinator-sessions" / "memo-claims" / "2026-07-06-claimed.md"
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_LIVE_CLAIM_PATCH, return_value=True):
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "actioned memo with live claim must be excluded from dry_run candidates"
    )


def test_dry_run_includes_actioned_memo_with_dead_claim(fleet_repo):
    """Actioned memo whose claim dir exists but claim is dead IS a candidate."""
    _seed_memo(fleet_repo, "2026-07-06-dead-claim.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-dead-claim.md"

    claim_dir = fleet_repo.common_dir / "coordinator-sessions" / "memo-claims" / "2026-07-06-dead-claim.md"
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_LIVE_CLAIM_PATCH, return_value=False):
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid in ids, (
        "actioned memo whose claim is dead must appear in dry_run candidates"
    )


def test_dry_run_excludes_memo_on_indeterminate_liveness_read(fleet_repo):
    """An errored/indeterminate liveness read (cs_claim_holder_live raises)
    must be treated as NOT terminal (deferred), never as a dead claim that
    clears the memo for archival.

    Regression guard for the 2026-07-21 fix: cs_claim_holder_live used to
    swallow every exception into a bare False (confirmed-dead), which this
    op's _is_terminal then read as "claim not live" -- clearing the memo for
    archival out from under a session whose liveness read simply failed. Now
    the exception propagates out of cs_claim_holder_live and this op's own
    try/except around the call must catch it and fail closed to "not
    terminal" (defer archival).
    """
    _seed_memo(fleet_repo, "2026-07-06-indeterminate.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-indeterminate.md"

    claim_dir = fleet_repo.common_dir / "coordinator-sessions" / "memo-claims" / "2026-07-06-indeterminate.md"
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_LIVE_CLAIM_PATCH, side_effect=OSError("simulated indeterminate read")):
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "an indeterminate/errored liveness read must defer archival, not clear it"
    )


# ---------------------------------------------------------------------------
# (c) dry_run:false (act) — git-mv, flat dest, index clean, log src+dst
# ---------------------------------------------------------------------------


def test_act_archives_actioned_memo_flat_dest(fleet_repo):
    """dry_run:false: git-mv moves actioned memo to cross-repo/archive/ (flat).

    Flat destination means NO YYYY-MM/ subdirectory — unlike handoffs/plans.
    Source must be gone; dest must be at cross-repo/archive/<filename>.
    """
    _seed_memo(fleet_repo, "2026-07-06-target.md", "actioned", title="Target Memo")
    cid = "cross-repo/inbox/2026-07-06-target.md"

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert result["skipped"] == []
    assert result["failed"] == []

    # Source must be gone.
    assert not fleet_repo.path_exists(cid), "source file must be removed by git mv"

    # Destination is FLAT: cross-repo/archive/<filename> (no YYYY-MM/ subdir).
    expected_dest = "cross-repo/archive/2026-07-06-target.md"
    assert fleet_repo.path_exists(expected_dest), (
        f"archived memo must be at flat dest {expected_dest!r} (no YYYY-MM/ subdir)"
    )

    # Index must be clean.
    assert fleet_repo.git_status_clean(), "working tree must be clean after archive"


def test_act_git_log_shows_both_src_and_dst(fleet_repo):
    """git log --no-renames must enumerate BOTH src AND dst paths (DR-211 D3 both-sides pathspec).

    --no-renames disables rename-collapse so git mv is shown as deleted source
    AND added destination.  A dst-only pathspec would orphan the src deletion
    in the staged index.
    """
    _seed_memo(fleet_repo, "2026-07-06-logcheck.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-logcheck.md"

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))
    assert result["exit_code"] == 0

    log_names = _git_log_names_no_renames(fleet_repo, 1)
    assert cid in log_names, (
        f"src path {cid!r} must appear in last commit (--no-renames); got {log_names!r}"
    )
    expected_dest = "cross-repo/archive/2026-07-06-logcheck.md"
    assert expected_dest in log_names, (
        f"dst path {expected_dest!r} must appear in last commit; got {log_names!r}"
    )


def test_act_commit_subject_names_op(fleet_repo):
    """Commit subject must identify fleet.archive_actioned_memos for audit-trail clarity."""
    _seed_memo(fleet_repo, "2026-07-06-subject.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-subject.md"

    _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    subject = fleet_repo.git_log_subject()
    assert "fleet.archive_actioned_memos" in subject, (
        f"commit subject must name the op; got {subject!r}"
    )


def test_act_non_actioned_memo_not_archived(fleet_repo):
    """Non-actioned memo supplied as candidate_id is skipped (status drift), not archived."""
    path = _seed_memo(fleet_repo, "2026-07-06-live.md", "in_progress")
    cid = "cross-repo/inbox/2026-07-06-live.md"

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    # exit_code:0 — a non-terminal skip is not an error.
    assert result["exit_code"] == 0
    assert result["acted"] == [], "non-actioned memo must NOT be archived"
    skip_ids = [s["id"] for s in result["skipped"]]
    assert cid in skip_ids, f"{cid!r} must be in skipped[]"

    # Source must still exist.
    assert fleet_repo.path_exists(cid), "non-actioned source must not have been moved"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (d) Idempotent replay — re-dispatch → already-archived, exit_code:0
# ---------------------------------------------------------------------------


def test_idempotent_replay_skips_already_archived(fleet_repo):
    """Re-dispatch same candidate_ids after archive → skipped 'already-archived', exit_code:0.

    Idempotency contract: a second dispatch with the same candidate_ids must
    produce exit_code:0 and classify the archived memo as skipped, never failed.
    No new commit must be created.
    """
    _seed_memo(fleet_repo, "2026-07-06-idem.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-idem.md"

    # --- First dispatch: archives the memo ---
    first = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))
    assert first["exit_code"] == 0, f"first dispatch must succeed; got {first!r}"
    assert first["acted"] == [{"id": cid, "archived": True}]
    assert not fleet_repo.path_exists(cid), "source must be gone after first dispatch"
    assert fleet_repo.git_status_clean()

    commit_after_first = fleet_repo.git_log_subject()

    # --- Second dispatch with same candidate_ids ---
    second = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert second["exit_code"] == 0, (
        f"idempotent replay must return exit_code:0; got {second!r}"
    )
    assert second["acted"] == [], "nothing new must be acted on replay"
    assert second["failed"] == [], "already-archived must never appear in failed[]"

    skip_map = {s["id"]: s["reason"] for s in second["skipped"]}
    assert cid in skip_map, f"{cid!r} must be in skipped[] on replay"
    assert skip_map[cid] == "already-archived", (
        f"skip reason must be 'already-archived'; got {skip_map[cid]!r}"
    )

    # No new commit must have been created.
    assert fleet_repo.git_log_subject() == commit_after_first, (
        "idempotent replay must not create a new commit"
    )
    assert fleet_repo.git_status_clean(), "index must be clean after idempotent replay"


def test_idempotent_replay_dry_run_excludes_archived(fleet_repo):
    """dry_run preview after archive: already-archived memo is excluded from candidates."""
    _seed_memo(fleet_repo, "2026-07-06-idem-preview.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-idem-preview.md"

    # Archive first.
    _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))
    assert not fleet_repo.path_exists(cid)

    # Second dry_run preview: archived memo must NOT appear in candidates.
    preview = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))
    ids = [c["id"] for c in preview["candidates"]]
    assert cid not in ids, (
        "already-archived memo must be excluded from dry_run candidates on re-run"
    )


# ---------------------------------------------------------------------------
# (e) Act-time terminality drift (D1 re-verify)
# ---------------------------------------------------------------------------


def test_drift_skip_status_changed_between_preview_and_act(fleet_repo):
    """D1 re-verify: memo flipped to non-actioned between preview and act → skipped.

    The memo is actioned at T1 (preview), status is mutated before T3 (act).
    At act time the D1 re-verify must classify it as skipped, not moved.
    """
    path = _seed_memo(fleet_repo, "2026-07-06-drift.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-drift.md"

    # T1: preview — memo must appear as a candidate.
    preview = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))
    preview_ids = [c["id"] for c in preview["candidates"]]
    assert cid in preview_ids, "actioned memo must appear in T1 preview candidates"

    # Drift: mutate status to non-terminal between preview and act.
    fleet_repo.update_file_status(path, "in_flight")

    # T3: act — must be skipped (D1 terminality re-verify).
    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, (
        f"all-skipped must produce exit_code:0; got {result!r}"
    )
    assert result["acted"] == [], "drifted memo must NOT be moved"
    assert result["failed"] == [], "drift is a skip, not a failure"

    skip_ids = [s["id"] for s in result["skipped"]]
    assert cid in skip_ids, f"{cid!r} must be in skipped[] after status drift"

    # Source must still exist (not moved).
    assert fleet_repo.path_exists(cid), "drifted memo must remain at original location"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (f) Liveness-guard skip
# ---------------------------------------------------------------------------


def test_liveness_guard_live_claim_skips_on_act(fleet_repo):
    """Actioned memo with a live memo-claim is skipped at act time.

    Mirrors cs_sweep_actioned_memos liveness guard: a live claim signals the
    memo is still being processed; archiving it would race with the claimer.
    """
    _seed_memo(fleet_repo, "2026-07-06-live-claim-act.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-live-claim-act.md"

    claim_dir = (
        fleet_repo.common_dir
        / "coordinator-sessions"
        / "memo-claims"
        / "2026-07-06-live-claim-act.md"
    )
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_LIVE_CLAIM_PATCH, return_value=True):
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))

    assert result["exit_code"] == 0
    assert result["acted"] == [], "live-claim memo must NOT be moved"
    skip_ids = [s["id"] for s in result["skipped"]]
    assert cid in skip_ids, f"{cid!r} must be in skipped[] when live claim present"
    assert fleet_repo.path_exists(cid), "live-claim source must not have been moved"
    assert fleet_repo.git_status_clean()


def test_liveness_guard_dead_claim_acts_normally(fleet_repo):
    """Actioned memo whose claim is dead proceeds normally through act."""
    _seed_memo(fleet_repo, "2026-07-06-dead-claim-act.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-dead-claim-act.md"

    claim_dir = (
        fleet_repo.common_dir
        / "coordinator-sessions"
        / "memo-claims"
        / "2026-07-06-dead-claim-act.md"
    )
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_LIVE_CLAIM_PATCH, return_value=False):
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))

    assert result["exit_code"] == 0
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert not fleet_repo.path_exists(cid)
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (g) Partial failure → exit_code:2; main git index has no staged residue
# ---------------------------------------------------------------------------


def test_partial_failure_exit_code_2_clean_staged_index(fleet_repo):
    """DETERMINATE-PARTIAL: one committed memo succeeds, one untracked memo fails git-mv.

    The untracked memo exists on disk (passes filesystem scan + terminality check)
    but is NOT in the git index, so git mv fails for it.  The committed memo
    succeeds.  The handler must return exit_code:2 with the committed memo in
    acted[] and the untracked memo in failed[], with no staged residue in the
    main git index (DR-211 D3: private GIT_INDEX_FILE isolates the private stage).

    Note: git status --porcelain is NOT asserted clean here because the untracked
    memo file remains in cross-repo/inbox/ (shown as '??' by git).  The invariant
    checked is the main index: git diff --cached --name-only must be empty.
    """
    # Committed memo — git mv will succeed.
    _seed_memo(fleet_repo, "2026-07-06-good-memo.md", "actioned", commit=True)
    cid_good = "cross-repo/inbox/2026-07-06-good-memo.md"

    # Untracked memo — written to disk but NOT committed; git mv will fail.
    _seed_memo(fleet_repo, "2026-07-06-untracked-memo.md", "actioned", commit=False)
    cid_bad = "cross-repo/inbox/2026-07-06-untracked-memo.md"

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid_good, cid_bad]),
        repo_root=fleet_repo.common_dir,
    ))

    # exit_code:2 — DETERMINATE-PARTIAL (failed[] non-empty).
    assert result["exit_code"] == 2, (
        f"partial failure must produce exit_code:2; got exit_code={result['exit_code']} "
        f"acted={result['acted']} failed={result['failed']}"
    )

    # Committed memo must be in acted[].
    acted_ids = {a["id"] for a in result["acted"]}
    assert cid_good in acted_ids, (
        f"committed memo {cid_good!r} must be in acted[]; got {result['acted']!r}"
    )

    # Untracked memo must be in failed[].
    failed_ids = {f["id"] for f in result["failed"]}
    assert cid_bad in failed_ids, (
        f"untracked memo {cid_bad!r} must be in failed[]; got {result['failed']!r}"
    )

    # Committed memo must be archived (source gone, dest present).
    assert not fleet_repo.path_exists(cid_good), (
        "committed memo source must be gone after successful archive"
    )
    assert fleet_repo.path_exists("cross-repo/archive/2026-07-06-good-memo.md"), (
        "committed memo must be present at flat archive destination"
    )

    # Main git index must have no staged residue from the failed memo.
    staged = _staged_names(fleet_repo)
    assert staged == [], (
        f"main git index must have no staged residue after partial failure; "
        f"staged: {staged!r} (DR-211 D3 private GIT_INDEX_FILE invariant)"
    )


# ---------------------------------------------------------------------------
# (h) Sweep regression net (2026-07-20) — the defect that stranded 7 actioned
#     memos in cross-repo/inbox/.  Two legs:
#       h1/h2: the core predicate — an `actioned` memo IS archived, a
#              non-actioned one is NOT (the sweep's whole contract).
#       h3/h4: byte-identical duplicate delivery converges instead of being
#              skipped as "already-archived" forever; a DIFFERING archived
#              copy is still never clobbered.
# ---------------------------------------------------------------------------


def test_internal_sweep_archives_actioned_and_leaves_non_actioned(fleet_repo):
    """Self-scanning sweep: actioned memo archived, non-actioned memo untouched.

    This is the regression net for the 2026-07-20 defect — 7 memos carrying
    `status: actioned` sat in cross-repo/inbox/ because nothing ever called the
    sweep.  Exercises archive_actioned_memos_internal (the callable both
    session.boot_sweep and bin/sweep-actioned-memos.py drive), not the
    candidate_ids wire path.
    """
    from coordinator_core.ops.fleet.archive_actioned_memos import (
        archive_actioned_memos_internal,
    )

    _seed_memo(fleet_repo, "2026-07-06-done.md", "actioned", title="Done Memo")
    _seed_memo(fleet_repo, "2026-07-06-todo.md", "open", title="Todo Memo")

    acted, skipped, failed = _run(
        archive_actioned_memos_internal(fleet_repo.root, fleet_repo.common_dir)
    )

    assert failed == [], f"sweep must not fail: {failed!r}"

    acted_ids = {a["id"].replace("\\", "/") for a in acted}
    assert "cross-repo/inbox/2026-07-06-done.md" in acted_ids, (
        f"actioned memo MUST be archived by the sweep; acted={acted!r}"
    )

    assert not fleet_repo.path_exists("cross-repo/inbox/2026-07-06-done.md")
    assert fleet_repo.path_exists("cross-repo/archive/2026-07-06-done.md")

    # Non-actioned memo must be left exactly where it is.
    assert fleet_repo.path_exists("cross-repo/inbox/2026-07-06-todo.md"), (
        "non-actioned memo MUST remain in the inbox"
    )
    assert not fleet_repo.path_exists("cross-repo/archive/2026-07-06-todo.md")
    skipped_ids = {s["id"].replace("\\", "/") for s in skipped}
    assert "cross-repo/inbox/2026-07-06-todo.md" in skipped_ids

    assert fleet_repo.git_status_clean(), "sweep must leave a clean working tree"


def test_internal_sweep_is_idempotent(fleet_repo):
    """A second consecutive sweep is a clean no-op — archives nothing, no commit."""
    from coordinator_core.ops.fleet.archive_actioned_memos import (
        archive_actioned_memos_internal,
    )

    _seed_memo(fleet_repo, "2026-07-06-once.md", "actioned")

    acted_1, _s1, failed_1 = _run(
        archive_actioned_memos_internal(fleet_repo.root, fleet_repo.common_dir)
    )
    assert len(acted_1) == 1 and failed_1 == []

    head_after_first = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode().strip()

    acted_2, _s2, failed_2 = _run(
        archive_actioned_memos_internal(fleet_repo.root, fleet_repo.common_dir)
    )
    assert acted_2 == [], f"second sweep must archive nothing; got {acted_2!r}"
    assert failed_2 == []

    head_after_second = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode().strip()
    assert head_after_first == head_after_second, "idempotent replay must not commit"


def test_identical_duplicate_delivery_is_archived_not_stranded(fleet_repo):
    """A re-delivered, byte-identical actioned memo converges out of the inbox.

    Live case: cross-repo/inbox/2026-07-13-claude-central-em-claim-lock-liveness-
    prune-engine-port.md was byte-identical to the already-archived copy of the
    same name.  The old `if dst.exists(): skip "already-archived"` branch meant
    it could NEVER leave the inbox on any sweep run.
    """
    from coordinator_core.ops.fleet.archive_actioned_memos import (
        archive_actioned_memos_internal,
    )

    src = _seed_memo(fleet_repo, "2026-07-06-dup.md", "actioned", title="Dup Memo")
    body = src.read_bytes()

    # Pre-place a byte-identical, git-tracked copy in the archive.
    dst = fleet_repo.root / "cross-repo" / "archive" / "2026-07-06-dup.md"
    dst.write_bytes(body)
    fleet_repo._git("add", str(dst))
    fleet_repo._git("commit", "-m", "pre-place identical archived copy")

    acted, _skipped, failed = _run(
        archive_actioned_memos_internal(fleet_repo.root, fleet_repo.common_dir)
    )

    assert failed == [], f"duplicate convergence must not fail: {failed!r}"
    acted_ids = {a["id"].replace("\\", "/") for a in acted}
    assert "cross-repo/inbox/2026-07-06-dup.md" in acted_ids, (
        f"identical duplicate delivery MUST be swept, not stranded; acted={acted!r}"
    )
    assert not fleet_repo.path_exists("cross-repo/inbox/2026-07-06-dup.md")
    assert dst.read_bytes() == body, "archived content must be unchanged"
    assert fleet_repo.git_status_clean()


def test_differing_archived_copy_is_never_clobbered(fleet_repo):
    """When the archived copy DIFFERS, the inbox memo is skipped, not overwritten."""
    from coordinator_core.ops.fleet.archive_actioned_memos import (
        archive_actioned_memos_internal,
    )

    _seed_memo(fleet_repo, "2026-07-06-diff.md", "actioned", title="Diff Memo")

    dst = fleet_repo.root / "cross-repo" / "archive" / "2026-07-06-diff.md"
    original = b"---\nstatus: actioned\n---\n\nDIFFERENT ARCHIVED HISTORY\n"
    dst.write_bytes(original)
    fleet_repo._git("add", str(dst))
    fleet_repo._git("commit", "-m", "pre-place differing archived copy")

    acted, skipped, failed = _run(
        archive_actioned_memos_internal(fleet_repo.root, fleet_repo.common_dir)
    )

    assert acted == [] and failed == []
    skipped_ids = {s["id"].replace("\\", "/") for s in skipped}
    assert "cross-repo/inbox/2026-07-06-diff.md" in skipped_ids
    assert dst.read_bytes() == original, "differing archived history MUST NOT be clobbered"
    assert fleet_repo.path_exists("cross-repo/inbox/2026-07-06-diff.md")


# ---------------------------------------------------------------------------
# (i) Unreadable inbox — glob-swallows-PermissionError regression guard
#
# Path.glob("*.md") silently swallows PermissionError while walking an
# unreadable directory (yields an empty iterator, no exception), which made
# the previous `except OSError: return []` here dead code for the exact
# permission-denied case it existed to guard — an unreadable inbox read as
# "nothing to archive", indistinguishable from a genuinely empty one. The
# fix swaps to iterdir() (which does raise OSError) and surfaces the failure
# instead of converging on a bare empty result.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_inbox_dry_run_returns_setup_error_not_empty_candidates(fleet_repo, caplog):
    """dry_run:true on an unreadable cross-repo/inbox/ must fail loud, not converge
    on candidates:[] — that shape is indistinguishable from a genuinely empty inbox.
    """
    _seed_memo(fleet_repo, "2026-07-06-hidden.md", "actioned")
    inbox_dir = fleet_repo.root / "cross-repo" / "inbox"

    original_mode = inbox_dir.stat().st_mode
    os.chmod(inbox_dir, 0o000)
    try:
        with caplog.at_level(
            logging.WARNING, logger="coordinator_core.ops.fleet.archive_actioned_memos"
        ):
            result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))
    finally:
        os.chmod(inbox_dir, original_mode)

    assert result["exit_code"] == 1, (
        f"an unreadable inbox must surface as a setup error, not candidates:[]; got {result!r}"
    )
    assert result["candidates"] == []
    assert any(str(inbox_dir) in r.message for r in caplog.records), (
        "expected a logged WARNING naming the unreadable inbox dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_inbox_act_returns_setup_error(fleet_repo):
    """dry_run:false on an unreadable cross-repo/inbox/ must fail loud (exit_code:1),
    never silently converge on an empty (acted=[], skipped=[], failed=[]) triple.
    """
    _seed_memo(fleet_repo, "2026-07-06-hidden-act.md", "actioned")
    cid = "cross-repo/inbox/2026-07-06-hidden-act.md"
    inbox_dir = fleet_repo.root / "cross-repo" / "inbox"

    original_mode = inbox_dir.stat().st_mode
    os.chmod(inbox_dir, 0o000)
    try:
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))
    finally:
        os.chmod(inbox_dir, original_mode)

    assert result["exit_code"] == 1, (
        f"an unreadable inbox must surface as a setup error; got {result!r}"
    )
    assert result["acted"] == []
    assert result["failed"] == []


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_internal_sweep_unreadable_inbox_surfaces_in_skipped(fleet_repo):
    """archive_actioned_memos_internal (the boot-sweep/bin-script callable) must
    surface an unreadable inbox in skipped[], never converge on ([], [], []).
    """
    from coordinator_core.ops.fleet.archive_actioned_memos import (
        archive_actioned_memos_internal,
    )

    _seed_memo(fleet_repo, "2026-07-06-hidden-internal.md", "actioned")
    inbox_dir = fleet_repo.root / "cross-repo" / "inbox"

    original_mode = inbox_dir.stat().st_mode
    os.chmod(inbox_dir, 0o000)
    try:
        acted, skipped, failed = _run(
            archive_actioned_memos_internal(fleet_repo.root, fleet_repo.common_dir)
        )
    finally:
        os.chmod(inbox_dir, original_mode)

    assert acted == [] and failed == []
    assert skipped, (
        "an unreadable inbox must be surfaced in skipped[], not converge on a bare empty triple"
    )
    reasons = [s["reason"] for s in skipped]
    assert any("inbox-scan-failed" in r for r in reasons), (
        f"expected an inbox-scan-failed reason naming the unreadable dir; got {reasons!r}"
    )


# ---------------------------------------------------------------------------
# (j) Disposition-field guard — regression net for the 70a54f9b/47bc7eed/
#     989bf41b/ca9d3e83 unread-swallow incident: a hand `Edit` of frontmatter
#     can set status:actioned directly, bypassing memo_transition.py's
#     mandatory one-of-four-fields requirement. A status:actioned memo with
#     NO disposition field must be treated as non-terminal and left in the
#     inbox; a status:actioned memo carrying any ONE of the four fields must
#     archive normally.
# ---------------------------------------------------------------------------


def test_actioned_no_disposition_field_is_skipped_not_archived(fleet_repo):
    """status:actioned with none of _DISPOSITION_FIELDS present is NOT terminal.

    This is the exact shape of the incident: a hand `Edit` flips status to
    "actioned" without going through memo_transition (the only programmatic
    writer, which always attaches a disposition field). The memo must stay in
    the inbox — never archived unread.
    """
    _seed_memo(
        fleet_repo, "2026-07-06-no-disposition.md", "actioned",
        title="Hand-Flipped Memo", no_disposition=True,
    )
    cid = "cross-repo/inbox/2026-07-06-no-disposition.md"

    # dry_run:true — must be excluded from candidates.
    preview = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))
    preview_ids = [c["id"] for c in preview["candidates"]]
    assert cid not in preview_ids, (
        "status:actioned memo with no disposition field must NOT appear in "
        "dry_run candidates"
    )

    # dry_run:false — must be skipped with the new named reason, not moved.
    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))
    assert result["exit_code"] == 0
    assert result["acted"] == [], "memo with no disposition field must NOT be archived"
    assert result["failed"] == [], "this is a skip, not a failure"

    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map, f"{cid!r} must be in skipped[]"
    # The act path's D1 re-verify wraps the terminality reason in "re-live: "
    # (mirrors test_drift_skip_status_changed_between_preview_and_act) — the
    # underlying reason string is still the named constant.
    assert "actioned-no-disposition" in skip_map[cid], (
        f"skip reason must contain the named constant; got {skip_map[cid]!r}"
    )

    assert fleet_repo.path_exists(cid), (
        "memo with no disposition field must remain in the inbox, unarchived"
    )
    assert fleet_repo.git_status_clean()


def test_actioned_no_disposition_field_is_skipped_via_internal_sweep(fleet_repo):
    """Same guard exercised through archive_actioned_memos_internal (the
    session.boot_sweep / bin-script callable), confirming BOTH call sites of
    _is_terminal share the fix (it lives in the shared predicate)."""
    from coordinator_core.ops.fleet.archive_actioned_memos import (
        archive_actioned_memos_internal,
    )

    _seed_memo(
        fleet_repo, "2026-07-06-no-disposition-internal.md", "actioned",
        title="Hand-Flipped Memo (internal)", no_disposition=True,
    )
    cid = "cross-repo/inbox/2026-07-06-no-disposition-internal.md"

    acted, skipped, failed = _run(
        archive_actioned_memos_internal(fleet_repo.root, fleet_repo.common_dir)
    )

    assert failed == []
    acted_ids = {a["id"].replace("\\", "/") for a in acted}
    assert cid not in acted_ids, "no-disposition memo must not be archived by the internal sweep"

    skip_map = {s["id"].replace("\\", "/"): s["reason"] for s in skipped}
    assert cid in skip_map
    assert skip_map[cid] == "actioned-no-disposition"

    assert fleet_repo.path_exists(cid)
    assert fleet_repo.git_status_clean()


@pytest.mark.parametrize("field", _DISPOSITION_FIELDS)
def test_actioned_with_each_disposition_field_is_archived(fleet_repo, field):
    """status:actioned + EACH of the four disposition fields (in turn) IS archived.

    Mirrors the real memo_transition._action contract: exactly one of
    decision/decision_note/realized_by/actioned_note is required and any one
    suffices — this predicate must accept all four, not just the first.
    """
    name = f"2026-07-06-disposition-{field}.md"
    _seed_memo(
        fleet_repo, name, "actioned",
        title=f"Actioned via {field}", disposition_field=field,
    )
    cid = f"cross-repo/inbox/{name}"

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code for field={field!r}; result={result!r}"
    assert result["acted"] == [{"id": cid, "archived": True}], (
        f"memo with disposition field {field!r} must be archived; got {result!r}"
    )
    assert result["skipped"] == []
    assert result["failed"] == []
    assert not fleet_repo.path_exists(cid)
    assert fleet_repo.path_exists(f"cross-repo/archive/{name}")
    assert fleet_repo.git_status_clean()

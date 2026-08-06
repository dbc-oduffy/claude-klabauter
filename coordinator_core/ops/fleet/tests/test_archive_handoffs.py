"""
coordinator_core.ops.fleet.tests.test_archive_handoffs

Tests for the fleet.archive_completed_handoffs op.

Import guard: coordinator_core.ops.fleet.archive_handoffs MUST be imported at
module load time to fire the @register_op("fleet.archive_completed_handoffs")
side-effect and populate _REGISTRY.  Without this import the decorator has not
run and any completeness test passes vacuously over an empty registry
(lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) dry_run:true  — candidates[] with note field populated
  (b) dry_run:false — archives consumed+childless+unclaimed: source gone,
                      dest at archive/handoffs/YYYY-MM/, git log -1 --name-only
                      shows exact src+dst, git status --porcelain clean
  (c) RETENTION    — consumed handoff with live child (reverse_membership non-empty)
                      is NOT archived (not in candidates, not moved)
  (d) RE-LIVE skip — handoff that gains a live claim between preview and act is
                      skipped with reason containing "re-live"
  (e) claim-liveness — consumed_by session that is live → not a candidate
  (f) IDEMPOTENT-REPLAY (AC12) — re-dispatch same candidate_ids after archive
                      → skipped reason "already-archived", exit_code:0, mutates nothing
  (g) IN_FLIGHT archive-safety — a consumed handoff with deployment_state:in_flight
                      is never archived, independent of the liveness verdict
                      (heartbeat-race guard; interim subset of example-doctrine-repo lvv-04/C3)
  (k) Heir branch  — a consumed handoff succeeded via a predecessor/
                      additional_predecessors edge is archived promptly,
                      bypassing A2/Check 4 on the heir path only; forked_from
                      is NOT succession (DR-224); see the "(k) Heir branch"
                      section below for the full sub-matrix (dry_run:true and
                      dry_run:false coverage)

Spec backlinks:
  - Plan C7: docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md § C7, AC5, AC12
  - Contract: coordinator_core/contract/cockpit-invoke-producer-contract.md §2.1, §3.1
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md D1, D2(i)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.fleet  # noqa: F401 — triggers package __init__
import coordinator_core.ops.fleet.archive_handoffs  # noqa: F401 — fires @register_op

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet._common import handoff_claim_dir
from coordinator_core.ops.fleet.archive_handoffs import (
    _HEIR_NOTE_PREFIX,
    _HEIR_STATUS_LABELS,
    _handler,
    _is_terminal,
)

# session.reap import — reused (never re-derived) for the AC7 regression-oracle
# claim-dir path convention below (_CLAIM_SUBDIRS, _sessions_dir, _handler).
import coordinator_core.ops.session.reap  # noqa: F401 — fires @register_op("session.reap")
from coordinator_core.ops.session.reap import (
    _CLAIM_SUBDIRS,
    _handler as _reap_handler,
    _sessions_dir as _reap_sessions_dir,
)

# Positive floor assertion: the handler must be registered before any test runs.
_OP_NAME = "fleet.archive_completed_handoffs"
_fleet_registered_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
assert len(_fleet_registered_ops) >= 1, (
    "import guard failed: no fleet.* ops in _REGISTRY — "
    "check coordinator_core.ops.fleet.archive_handoffs import"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.fleet.archive_handoffs @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


# Patch targets — both are name-bound in archive_handoffs at import time.
_LIVE_SIDS_PATCH = "coordinator_core.ops.fleet.archive_handoffs.resolve_live_session_ids"
_REVERSE_PATCH = "coordinator_core.ops.fleet.archive_handoffs.reverse_membership"


def _edge_kind_aware_reverse_membership(non_heir_children: frozenset):
    """Build a reverse_membership side_effect that returns children ONLY on
    the default (all-edge-kinds) call, never on the heir-only
    (edge_kinds=_HEIR_EDGE_KINDS) call — simulates a live child that
    references the candidate via a NON-succession edge (forked_from), i.e.
    the "fork-only" heir-branch verdict, so pre-existing RETENTION tests
    keep asserting retention after the heir branch (2026-07-22) was added.
    _is_terminal's heir branch calls reverse_membership up to twice per
    Branch-A candidate (edge_kinds=_HEIR_EDGE_KINDS first, then the default);
    Check 3 (unconditional, unchanged) calls it a further time with the
    default when the heir branch's own verdict was "childless" — a blanket
    return_value=non-empty mock (the pre-heir-branch pattern) would make the
    FIRST (heir-only) call look like a real heir and flip these tests'
    expected outcome from retained to archived."""
    from coordinator_core.ops.fleet.archive_handoffs import _HEIR_EDGE_KINDS

    def _side_effect(node_path, dag_index, *, exclude=None, edge_kinds=None):
        if edge_kinds == _HEIR_EDGE_KINDS:
            return frozenset()
        return non_heir_children

    return _side_effect


def _make_params(dry_run: bool, candidate_ids=None) -> dict:
    """Build a minimal valid fleet.archive_completed_handoffs params dict."""
    p: dict = {"mode": "already-terminal", "dry_run": dry_run}
    if candidate_ids is not None:
        p["candidate_ids"] = candidate_ids
    return p


def _git_log_names_no_renames(fleet_repo, n: int = 1):
    """Return file paths touched in the last n commits with rename-detection disabled.

    Uses ``--no-renames`` so that a git-mv rename is shown as BOTH the source
    (deleted) and the destination (added) as separate path entries — unlike the
    default ``--name-only`` behaviour which collapses a rename into only the
    destination path.  This is required to verify the scoped-pathspec contract
    (AC4 / DR-211 D3) where both src AND dst must appear in the commit.
    """
    import subprocess as _sp
    result = _sp.run(
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
# (a) dry_run:true — candidates[] with note
# ---------------------------------------------------------------------------

def test_dry_run_returns_terminal_candidate_with_note(fleet_repo):
    """dry_run:true enumerates terminal (consumed+childless+unclaimed) handoffs with note."""
    # Consumed handoff — no consumed_by means resolve_live_session_ids is never called;
    # no predecessor fields means reverse_membership will find no children.
    fleet_repo.seed_handoff("2026-07-01-h-alpha.md", "claimed")
    # Non-terminal handoff — must NOT appear in candidates.
    fleet_repo.seed_handoff("2026-07-01-h-active.md", "in-progress")

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert isinstance(result["candidates"], list)

    ids = [c["id"] for c in result["candidates"]]
    assert "state/handoffs/2026-07-01-h-alpha.md" in ids, (
        "consumed+childless handoff must appear in dry_run candidates"
    )
    assert "state/handoffs/2026-07-01-h-active.md" not in ids, (
        "in-progress handoff must be excluded from candidates"
    )

    candidate = next(c for c in result["candidates"] if "h-alpha" in c["id"])
    assert candidate["family"] == "handoff"
    # Wire status_label for Branch A is hardcoded "consumed" (frozen wire
    # contract label, not the frontmatter status field) until it flips at C6.
    assert candidate["status"] == "consumed"
    assert "note" in candidate
    assert candidate["note"] == "consumed; no live children; no live claim"
    # acted/skipped/failed are empty in dry_run mode
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []


# ---------------------------------------------------------------------------
# (b) dry_run:false — git-mv: source gone, dest at YYYY-MM/, log clean, index clean
# ---------------------------------------------------------------------------

def test_act_archives_consumed_childless_unclaimed(fleet_repo):
    """dry_run:false: git-mv moves handoff, index clean, log shows src+dst."""
    fleet_repo.seed_handoff("2026-07-02-target.md", "claimed")
    cid = "state/handoffs/2026-07-02-target.md"

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert result["skipped"] == []
    assert result["failed"] == []

    # Source path must be gone from disk.
    assert not fleet_repo.path_exists(cid), "source file must be removed by git mv"

    # Destination: archive/handoffs/2026-07/<filename>
    expected_dest = "archive/handoffs/2026-07/2026-07-02-target.md"
    assert fleet_repo.path_exists(expected_dest), (
        f"archived file must exist at {expected_dest!r}"
    )

    # git log -1 (--no-renames) must show BOTH src AND dst paths.
    # --no-renames disables rename-collapse so a git mv rename is shown as a
    # deletion (src) + addition (dst) — the scoped-pathspec AC4 / DR-211 D3 check.
    # (Default --name-only collapses renames to dst only.)
    log_names = _git_log_names_no_renames(fleet_repo, 1)
    assert cid in log_names, f"src path {cid!r} must appear in last commit; got {log_names!r}"
    assert expected_dest in log_names, (
        f"dst path {expected_dest!r} must appear in last commit; got {log_names!r}"
    )

    # git status --porcelain must be empty (no dirty index, no unstaged changes).
    assert fleet_repo.git_status_clean(), "working tree must be clean after archive"


def test_act_archives_multiple_candidates(fleet_repo):
    """dry_run:false with multiple candidate_ids: all archived in one commit."""
    fleet_repo.seed_handoff("2026-07-02-ha.md", "claimed")
    fleet_repo.seed_handoff("2026-07-02-hb.md", "claimed")
    cid_a = "state/handoffs/2026-07-02-ha.md"
    cid_b = "state/handoffs/2026-07-02-hb.md"

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid_a, cid_b]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    acted_ids = {a["id"] for a in result["acted"]}
    assert cid_a in acted_ids
    assert cid_b in acted_ids
    assert result["failed"] == []

    assert not fleet_repo.path_exists(cid_a)
    assert not fleet_repo.path_exists(cid_b)
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (c) RETENTION: consumed handoff WITH a live child is NOT archived
# ---------------------------------------------------------------------------

def test_retention_consumed_handoff_with_live_child(fleet_repo):
    """A consumed handoff that still has a live child is excluded from candidates."""
    parent_path = fleet_repo.seed_handoff("2026-07-01-parent.md", "claimed")
    parent_rel = "state/handoffs/2026-07-01-parent.md"

    # Simulate a live child referencing this parent by making reverse_membership
    # return a non-empty frozenset for the parent's path.
    fake_child = str(fleet_repo.root / "state" / "handoffs" / "2026-07-01-child.md")

    with patch(
        _REVERSE_PATCH,
        side_effect=_edge_kind_aware_reverse_membership(frozenset({fake_child})),
    ):
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "consumed handoff with live children must be excluded from candidates (RETENTION guard)"
    )


def test_retention_terminal_child_does_not_block_archive(fleet_repo):
    """has_live_children terminal/archived-child exclusion (Chunk 7 blueprint surface).

    A consumed parent whose ONLY referencing child is itself terminal
    (status:consumed) must NOT be retained — real reverse_membership (not the
    mock) must exclude the terminal child from the live set so the parent is
    archivable.  Regresses the bug where handoff.has_live_children counted
    archive-resident/terminal children as live forever.
    """
    parent_path = fleet_repo.seed_handoff("2026-07-05-parent-terminal.md", "claimed")
    parent_rel = "state/handoffs/2026-07-05-parent-terminal.md"

    # Child is itself consumed (terminal) and references the parent.
    fleet_repo.seed_handoff(
        "2026-07-05-child-terminal.md", "claimed", predecessor=str(parent_path)
    )

    # No mock on reverse_membership — exercise the real predicate end-to-end.
    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel in ids, (
        "parent whose only child is terminal (consumed) must be archivable "
        "(terminal child excluded from live-children set); "
        f"got candidates={result['candidates']!r}"
    )


def test_retention_archived_child_does_not_block_archive(fleet_repo):
    """A consumed parent whose only referencing child is archive-resident is archivable."""
    parent_path = fleet_repo.seed_handoff("2026-07-05-parent-archived.md", "claimed")
    parent_rel = "state/handoffs/2026-07-05-parent-archived.md"

    # Child lives under archive/handoffs/2026-06/ and references the parent.
    archive_dir = fleet_repo.root / "archive" / "handoffs" / "2026-06"
    archive_dir.mkdir(parents=True, exist_ok=True)
    child_path = archive_dir / "2026-06-01-child-archived.md"
    child_path.write_text(
        f"---\ntitle: \"Archived Child\"\nstatus: active\ncreated: 2026-06-01\n"
        f"predecessor: {parent_path}\n---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    fleet_repo._git("add", str(child_path))
    fleet_repo._git("commit", "-m", "add archived child")

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel in ids, (
        "parent whose only child is archive-resident must be archivable "
        f"(archived child excluded from live-children set); got candidates={result['candidates']!r}"
    )


def test_retention_genuinely_live_child_still_blocks_archive(fleet_repo):
    """A consumed parent whose only child is genuinely live (active,
    state/-resident) AND references it via a NON-succession edge
    (forked_from) is still retained — the real predicate, no mock, must
    still fail-closed correctly.

    2026-07-22 heir-branch update: this test previously used a
    ``predecessor``-edge child to exercise "genuinely live child blocks
    archive". Under the heir branch (see the "(k) Heir branch" test section
    below and archive_handoffs.py's module docstring), a live child
    referencing via predecessor/additional_predecessors IS precisely the
    "has an heir" case the new branch exists to archive PROMPTLY — so that
    child edge no longer belongs in a RETENTION test. Switched to
    forked_from, which the heir branch explicitly does NOT treat as
    succession (DR-224) — preserving this test's original "a real,
    non-terminal, non-heir live child still blocks archival" intent.
    """
    parent_path = fleet_repo.seed_handoff("2026-07-05-parent-live.md", "claimed")
    parent_rel = "state/handoffs/2026-07-05-parent-live.md"

    fleet_repo.seed_handoff(
        "2026-07-05-child-live.md",
        "active",
        extra_frontmatter=f"forked_from: {parent_path}\npredecessor: none",
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "parent with a genuinely live (active), non-succession-edge child must "
        f"remain retained (not archivable); got candidates={result['candidates']!r}"
    )


def test_retention_live_child_blocks_act_too(fleet_repo):
    """D1 act-time re-verify: handoff whose child becomes live between preview and act is skipped."""
    fleet_repo.seed_handoff("2026-07-01-retention-act.md", "claimed")
    cid = "state/handoffs/2026-07-01-retention-act.md"
    fake_child = str(fleet_repo.root / "state" / "handoffs" / "2026-07-01-child.md")

    # Act: reverse_membership now returns a live child (simulates child gaining a
    # non-succession reference — fork-only — so the heir branch does not flip
    # this to an "archived" outcome; see _edge_kind_aware_reverse_membership).
    with patch(
        _REVERSE_PATCH,
        side_effect=_edge_kind_aware_reverse_membership(frozenset({fake_child})),
    ):
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))

    assert result["exit_code"] == 0
    assert result["acted"] == []
    skip_ids = [s["id"] for s in result["skipped"]]
    assert cid in skip_ids, "re-live handoff (gained live child) must land in skipped[]"
    # Source file must not have been moved.
    assert fleet_repo.path_exists(cid)
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (d) RE-LIVE skip: handoff gains live claim between preview and act
# ---------------------------------------------------------------------------

def test_relive_skip_claim_acquired_between_preview_and_act(fleet_repo):
    """Handoff whose consumed_by session goes live between preview and act → skipped 're-live'."""
    fleet_repo.seed_handoff(
        "2026-07-03-relive.md", "claimed", claimed_by="session-relive-abc"
    )
    cid = "state/handoffs/2026-07-03-relive.md"

    # --- PREVIEW: session not live → handoff appears as terminal candidate ---
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        preview = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    preview_ids = [c["id"] for c in preview["candidates"]]
    assert cid in preview_ids, "consumed+unclaimed handoff must appear in preview candidates"

    # --- ACT: session-relive-abc is now live (claim acquired after preview) ---
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset({"session-relive-abc"})):
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))

    assert result["exit_code"] == 0, f"re-live skip must not set exit_code:1 or 2; got {result!r}"
    assert result["acted"] == [], "re-live handoff must NOT be moved"

    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map, f"{cid!r} must be in skipped[]; got {result['skipped']!r}"
    assert "re-live" in skip_map[cid], (
        f"skip reason must contain 're-live'; got {skip_map[cid]!r}"
    )

    # Source file must still exist — nothing was moved.
    assert fleet_repo.path_exists(cid), "re-live handoff source must not have been moved"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (e) Claim-liveness: consumed_by session that is live → not archived
# ---------------------------------------------------------------------------

def test_claim_liveness_live_consumed_by_excludes_candidate(fleet_repo):
    """Handoff whose consumed_by session is currently live is not a terminal candidate."""
    fleet_repo.seed_handoff(
        "2026-07-03-claimed.md", "claimed", claimed_by="live-session-xyz"
    )
    cid = "state/handoffs/2026-07-03-claimed.md"

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset({"live-session-xyz"})):
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "handoff consumed by a live session must be excluded from candidates (claim-liveness)"
    )


def test_claim_liveness_dead_consumed_by_includes_candidate(fleet_repo):
    """Handoff whose consumed_by session is dead (not live) IS a terminal candidate."""
    fleet_repo.seed_handoff(
        "2026-07-03-dead-claimed.md", "claimed", claimed_by="dead-session-000"
    )
    cid = "state/handoffs/2026-07-03-dead-claimed.md"

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):  # no live sessions
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid in ids, (
        "handoff consumed by a dead session must appear in candidates"
    )


# Review: code-reviewer F3 (2026-07-14 slice1) — the new PRIMARY claim-dir-liveness
# signal (Check 4's claim_dir.is_dir() branch) was previously exercised only via the
# dry_run:false act path (test_regression_oracle_prune_and_archive_coherence).
# Cockpit-facing reviewers primarily consume dry_run:true candidates[], so the
# load-bearing safety property must be proven on the preview path directly too.
_ARCHIVE_CLAIM_LIVE_PATCH_E = "coordinator_core.ops.fleet.archive_handoffs.cs_claim_holder_live"


def test_claim_liveness_live_claim_dir_excludes_candidate_preview(fleet_repo):
    """dry_run:true: a live claim-dir holder excludes the handoff from candidates[]
    even with NO consumed_by set (the new PRIMARY signal, not the fallback)."""
    handoff_path = fleet_repo.seed_handoff("2026-07-14-claimdir-live-preview.md", "claimed")
    cid = "state/handoffs/2026-07-14-claimdir-live-preview.md"

    claim_dir = handoff_claim_dir(fleet_repo.common_dir, handoff_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    def _claim_live(claim_dir_str: str) -> bool:
        return claim_dir_str == str(claim_dir)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()), \
         patch(_ARCHIVE_CLAIM_LIVE_PATCH_E, side_effect=_claim_live):
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "a live claim-dir holder must exclude the handoff from preview candidates "
        f"even with no consumed_by set; got {result['candidates']!r}"
    )


def test_claim_liveness_blocks_act(fleet_repo):
    """D1 re-verify: consumed_by session live at act time → skipped 're-live'."""
    fleet_repo.seed_handoff(
        "2026-07-03-claimed-act.md", "claimed", claimed_by="session-act-guard"
    )
    cid = "state/handoffs/2026-07-03-claimed-act.md"

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset({"session-act-guard"})):
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))

    assert result["exit_code"] == 0
    assert result["acted"] == []
    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map
    assert "re-live" in skip_map[cid]
    assert fleet_repo.path_exists(cid)


# ---------------------------------------------------------------------------
# (f) IDEMPOTENT-REPLAY (AC12)
# ---------------------------------------------------------------------------

def test_idempotent_replay_skips_already_archived(fleet_repo):
    """Re-dispatch same candidate_ids after archive → skipped 'already-archived', exit_code:0."""
    fleet_repo.seed_handoff("2026-07-04-idem.md", "claimed")
    cid = "state/handoffs/2026-07-04-idem.md"

    # --- First dispatch: archives the handoff ---
    first = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))
    assert first["exit_code"] == 0, f"first dispatch must succeed; got {first!r}"
    assert first["acted"] == [{"id": cid, "archived": True}]
    assert not fleet_repo.path_exists(cid), "source must be gone after first dispatch"
    assert fleet_repo.git_status_clean()

    # --- Second dispatch with the same candidate_ids: source is gone ---
    second = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    # exit_code:0 — already-archived is a clean skip, not a failure.
    assert second["exit_code"] == 0, (
        f"idempotent replay must return exit_code:0; got {second!r}"
    )
    assert second["acted"] == [], "nothing new must be acted on replay"
    assert second["failed"] == [], "already-archived must never appear in failed[]"

    skip_map = {s["id"]: s["reason"] for s in second["skipped"]}
    assert cid in skip_map, f"{cid!r} must appear in skipped[] on replay; got {second['skipped']!r}"
    assert skip_map[cid] == "already-archived", (
        f"skip reason must be 'already-archived'; got {skip_map[cid]!r}"
    )

    # Index must remain clean after idempotent replay.
    assert fleet_repo.git_status_clean(), "index must be clean after idempotent replay"


# ---------------------------------------------------------------------------
# Absolute-path candidate_id tolerance (DR-215 close-out item ii)
# ---------------------------------------------------------------------------

def test_act_archives_when_candidate_id_is_absolute_path(fleet_repo):
    """An absolute-path candidate_id for a live, terminal handoff is archived —
    NOT silently classified as already-archived (the strict repo-relative-only
    .get(cid) lookup previously no-op'd on absolute candidate_ids)."""
    fleet_repo.seed_handoff("2026-07-06-abs-target.md", "claimed")
    rel_cid = "state/handoffs/2026-07-06-abs-target.md"
    abs_cid = str(fleet_repo.root / rel_cid)

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[abs_cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["acted"] == [{"id": abs_cid, "archived": True}], (
        f"absolute-path candidate must be archived, not skipped; got {result!r}"
    )
    assert result["skipped"] == [], (
        f"absolute-path candidate must NOT be silently skipped as already-archived; "
        f"got {result['skipped']!r}"
    )
    assert result["failed"] == []

    # Source path must be gone from disk; destination present.
    assert not fleet_repo.path_exists(rel_cid), "source file must be removed by git mv"
    expected_dest = "archive/handoffs/2026-07/2026-07-06-abs-target.md"
    assert fleet_repo.path_exists(expected_dest), (
        f"archived file must exist at {expected_dest!r}"
    )
    assert fleet_repo.git_status_clean()


def test_idempotent_replay_mixed_fresh_and_archived(fleet_repo):
    """AC12 mix: one already-archived + one fresh → fresh is acted, archived is skipped."""
    fleet_repo.seed_handoff("2026-07-04-archived.md", "claimed")
    fleet_repo.seed_handoff("2026-07-04-fresh.md", "claimed")
    cid_archived = "state/handoffs/2026-07-04-archived.md"
    cid_fresh = "state/handoffs/2026-07-04-fresh.md"

    # Archive only cid_archived first.
    first = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid_archived]),
        repo_root=fleet_repo.common_dir,
    ))
    assert first["exit_code"] == 0
    assert not fleet_repo.path_exists(cid_archived)

    # Re-dispatch with BOTH: cid_archived (already gone) + cid_fresh (still live).
    second = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid_archived, cid_fresh]),
        repo_root=fleet_repo.common_dir,
    ))

    assert second["exit_code"] == 0
    acted_ids = {a["id"] for a in second["acted"]}
    assert cid_fresh in acted_ids, "fresh candidate must be acted on second dispatch"
    skip_map = {s["id"]: s["reason"] for s in second["skipped"]}
    assert cid_archived in skip_map
    assert skip_map[cid_archived] == "already-archived"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (g) IN_FLIGHT archive-safety — deployment_state:in_flight hard exclusion
# ---------------------------------------------------------------------------
#
# Regresses the heartbeat-race bug (2026-07-10 example-retrieval-repo inbox memos): the
# Check 4 liveness signal (resolve_live_session_ids) is heartbeat-windowed and
# can transiently drop a genuinely-live session mid-long-tool-call, so a
# consumed handoff can look unclaimed even while it is actively in flight.
# These cases are constructed so Check 4 WOULD pass (consumed_by absent or a
# non-live sid, no children) — proving the new gate fires independent of the
# liveness verdict.

def test_in_flight_deployment_state_excluded_from_dry_run_candidates(fleet_repo):
    """A consumed handoff with deployment_state:in_flight must NOT appear in
    dry_run candidates, even with no consumed_by claim (would otherwise pass
    Check 4 trivially — consumed_by absent means no live session holds it)."""
    fleet_repo.seed_handoff(
        "2026-07-10-in-flight.md",
        "claimed",
        extra_frontmatter="deployment_state: in_flight",
    )
    cid = "state/handoffs/2026-07-10-in-flight.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "deployment_state:in_flight handoff must never appear in candidates "
        f"(archive-safety); got candidates={result['candidates']!r}"
    )


def test_in_flight_deployment_state_blocks_act_even_with_dead_consumed_by(fleet_repo):
    """A consumed+in_flight handoff with a DEAD consumed_by sid (Check 4 would
    pass — the exact heartbeat-race shape) must still be skipped/not archived
    at act time, proving the gate is independent of the liveness verdict."""
    fleet_repo.seed_handoff(
        "2026-07-10-in-flight-act.md",
        "claimed",
        claimed_by="dead-session-999",
        extra_frontmatter="deployment_state: in_flight",
    )
    cid = "state/handoffs/2026-07-10-in-flight-act.md"

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):  # sid reads as not-live
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))

    assert result["exit_code"] == 0
    assert result["acted"] == [], (
        "deployment_state:in_flight handoff must NOT be archived even when the "
        f"liveness check reads not-live; got acted={result['acted']!r}"
    )
    assert fleet_repo.path_exists(cid), "in_flight handoff source must not have been moved"
    assert fleet_repo.git_status_clean()


def test_in_flight_deployment_state_direct_unit_is_terminal(fleet_repo):
    """Direct _is_terminal unit assertion: deployment_state:in_flight → not terminal,
    with the exact reason string, independent of liveness/children."""
    handoff_path = fleet_repo.seed_handoff(
        "2026-07-10-in-flight-direct.md",
        "claimed",
        extra_frontmatter="deployment_state: in_flight",
    )

    is_terminal, reason, status_label = _run(
        _is_terminal(handoff_path, [str(handoff_path)], fleet_repo.root, fleet_repo.common_dir)
    )

    assert is_terminal is False
    assert reason == "deployment_state=in_flight — not terminal (archive-safety)"


# ---------------------------------------------------------------------------
# (h) Branch B — terminal deployment_state (active+shipped/abandoned widening,
# 2026-07-13).  Regresses the off-baton handoff stranding bug: a handoff with
# status:active + deployment_state:shipped/abandoned is a schema-valid terminal
# state (claude-klabauter's status enum is only {active, consumed}) produced when work
# ships off-baton (never consumed) — e.g. auto-reconcile or /workstream-complete
# Step 2.7 --stamp-only.  Prior to this widening, Check 1 (status==consumed)
# rejected these outright and they were stranded forever.
# ---------------------------------------------------------------------------

def test_active_shipped_with_resolvable_shipped_in_is_terminal(fleet_repo):
    """active + shipped + resolvable shipped_in, no children, no live claim → terminal."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    fleet_repo.seed_handoff(
        "2026-07-13-shipped-resolvable.md",
        "active",
        extra_frontmatter=f"deployment_state: shipped\nshipped_in: {head_sha}",
    )
    cid = "state/handoffs/2026-07-13-shipped-resolvable.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid in ids, (
        "active+shipped handoff with resolvable shipped_in must be archivable "
        f"(Branch B); got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == cid)
    assert candidate["status"] == "shipped", (
        f"status label for the shipped branch must be 'shipped'; got {candidate!r}"
    )


def test_active_shipped_with_unresolvable_shipped_in_is_not_terminal(fleet_repo):
    """active + shipped + unresolvable/absent shipped_in → NOT terminal (fail-closed)."""
    fleet_repo.seed_handoff(
        "2026-07-13-shipped-unresolvable.md",
        "active",
        extra_frontmatter="deployment_state: shipped\nshipped_in: deadbeef",
    )
    cid = "state/handoffs/2026-07-13-shipped-unresolvable.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "active+shipped handoff with an unresolvable shipped_in must be retained "
        f"(fail-closed); got candidates={result['candidates']!r}"
    )


def test_active_shipped_with_absent_shipped_in_is_not_terminal(fleet_repo):
    """active + shipped with NO shipped_in field at all → NOT terminal (fail-closed)."""
    fleet_repo.seed_handoff(
        "2026-07-13-shipped-absent.md",
        "active",
        extra_frontmatter="deployment_state: shipped",
    )
    cid = "state/handoffs/2026-07-13-shipped-absent.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "active+shipped handoff with absent shipped_in must be retained (fail-closed); "
        f"got candidates={result['candidates']!r}"
    )


def test_active_abandoned_no_children_no_claim_is_terminal(fleet_repo):
    """active + abandoned, no children, no live claim → terminal; status label 'abandoned'."""
    fleet_repo.seed_handoff(
        "2026-07-13-abandoned.md",
        "active",
        extra_frontmatter="deployment_state: abandoned",
    )
    cid = "state/handoffs/2026-07-13-abandoned.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid in ids, (
        f"active+abandoned handoff must be archivable (Branch B); "
        f"got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == cid)
    assert candidate["status"] == "abandoned", (
        f"status label for the abandoned branch must be 'abandoned'; got {candidate!r}"
    )


def test_active_shipped_with_live_children_is_not_terminal(fleet_repo):
    """active + shipped + resolvable shipped_in but WITH live children → NOT terminal."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-13-shipped-with-child.md",
        "active",
        extra_frontmatter=f"deployment_state: shipped\nshipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-13-shipped-with-child.md"

    fleet_repo.seed_handoff(
        "2026-07-13-shipped-child-live.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "active+shipped handoff with a live child must be retained "
        f"(has_live_children guard applies to Branch B too); got candidates={result['candidates']!r}"
    )


# ---------------------------------------------------------------------------
# (i) Branch B / Branch A co-occurrence — status:consumed + deployment_state:shipped
# (code-reviewer Finding 1, 2026-07-13 slice).  Branch B is checked FIRST in
# _is_terminal, so a status:consumed handoff that ALSO carries
# deployment_state:shipped now routes through Branch B's fail-closed shipped_in
# gate instead of Branch A's unconditional (for non-in_flight) qualification.
# This is a genuine behavior change on the consumed path (previously: always
# archived once consumed+not-in_flight; now: archived only if shipped_in is
# present AND resolvable) — and is the INTENDED verdict, per precedent: today's
# archive_shipped_handoffs._is_shipped_terminal already applies the identical
# fail-closed shipped_in gate with NO status check at all, so a
# status:consumed+shipped+unresolvable-shipped_in handoff was ALREADY being
# retained by that op whenever boot_sweep ran both sweeps in sequence. These two
# tests close the gap: they prove the union-qualification (a) is real (not just
# theoretical), and (b) the fail-closed retention on the consumed path is a
# deliberate consistency alignment with archive_shipped_handoffs.py, not a
# stranding regression.
# ---------------------------------------------------------------------------

def test_consumed_shipped_with_resolvable_shipped_in_is_terminal(fleet_repo):
    """status:consumed + deployment_state:shipped + resolvable shipped_in, no
    children, no claim → terminal via Branch B; status_label == 'shipped' proves
    the union-qualification (Branch B checked before Branch A) is real."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    fleet_repo.seed_handoff(
        "2026-07-13-consumed-shipped-resolvable.md",
        "claimed",
        extra_frontmatter=f"deployment_state: shipped\nshipped_in: {head_sha}",
    )
    cid = "state/handoffs/2026-07-13-consumed-shipped-resolvable.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid in ids, (
        "status:consumed + deployment_state:shipped + resolvable shipped_in must "
        f"be archivable (Branch B qualifies before Branch A); got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == cid)
    assert candidate["status"] == "shipped", (
        "status label must be 'shipped' (Branch B), not 'consumed' (Branch A) — "
        f"proves Branch B's union-qualification actually fires; got {candidate!r}"
    )


def test_consumed_shipped_with_unresolvable_shipped_in_is_retained(fleet_repo):
    """status:consumed + deployment_state:shipped + absent/unresolvable shipped_in
    → NOT terminal / RETAINED (fail-closed).

    This is the INTENDED verdict, not a stranding regression: prior to the
    2026-07-13 Branch-B widening, this exact handoff (status:consumed,
    deployment_state:shipped, never in_flight) archived unconditionally via the
    old single-branch predicate. After the widening, Branch B is checked first
    and its fail-closed shipped_in gate now retains it when shipped_in is
    absent/unresolvable. This is a genuine behavior change on the consumed path
    — but archive_shipped_handoffs.py's existing dedicated shipped sweep
    (_is_shipped_terminal) ALREADY applies the identical fail-closed shipped_in
    gate with NO status check at all, so a status:consumed+shipped+unresolvable-
    shipped_in handoff was ALREADY being retained by that op whenever
    session.boot_sweep ran both sweeps (consumed-handoffs then shipped-handoffs)
    in the same boot. This test makes fleet.archive_completed_handoffs (and the
    consumed sub-sweep of boot_sweep) consistent with that precedent, closing
    the gap flagged in code-reviewer Finding 1 (2026-07-13 slice) between "this
    is very likely intentional" and "this is tested and confirmed intentional."
    """
    fleet_repo.seed_handoff(
        "2026-07-13-consumed-shipped-unresolvable.md",
        "claimed",
        extra_frontmatter="deployment_state: shipped\nshipped_in: deadbeef",
    )
    cid = "state/handoffs/2026-07-13-consumed-shipped-unresolvable.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "status:consumed + deployment_state:shipped + unresolvable shipped_in "
        "must be RETAINED (fail-closed) — this is the intended verdict, "
        "consistent with archive_shipped_handoffs.py's identical fail-closed "
        f"gate for shipped handoffs regardless of status; got candidates={result['candidates']!r}"
    )


def test_active_abandoned_with_garbage_shipped_in_still_terminal(fleet_repo):
    """active + abandoned + a garbage/unresolvable shipped_in (leftover from a
    prior shipped→abandoned transition), no children, no claim → still terminal.

    Proves _shipped_in_resolvable is genuinely never invoked on the abandoned
    branch — closes the gap between "field absent" (the existing abandoned test)
    and "field present but ignored" (code-reviewer Finding 7, 2026-07-13 slice)."""
    fleet_repo.seed_handoff(
        "2026-07-13-abandoned-garbage-shipped-in.md",
        "active",
        extra_frontmatter="deployment_state: abandoned\nshipped_in: not-a-real-sha",
    )
    cid = "state/handoffs/2026-07-13-abandoned-garbage-shipped-in.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid in ids, (
        "active+abandoned handoff must be terminal regardless of a garbage "
        f"shipped_in value (abandoned needs no resolvability check); got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == cid)
    assert candidate["status"] == "abandoned"


def test_active_shipped_with_malformed_shipped_in_is_not_terminal(fleet_repo):
    """active + shipped + a malformed (non-hex / whitespace-only) shipped_in
    value → NOT terminal (fail-closed).

    Distinguishes the malformed-argument failure mode from the
    valid-but-unreachable one already covered by
    test_active_shipped_with_unresolvable_shipped_in_is_not_terminal (which uses
    "deadbeef" — syntactically SHA-like but not resolvable). Both should behave
    identically (return False from _shipped_in_resolvable), but a single test
    conflated both shapes (code-reviewer Finding 8, 2026-07-13 slice)."""
    fleet_repo.seed_handoff(
        "2026-07-13-shipped-malformed.md",
        "active",
        extra_frontmatter="deployment_state: shipped\nshipped_in: not-a-sha-at-all",
    )
    cid = "state/handoffs/2026-07-13-shipped-malformed.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "active+shipped handoff with a malformed (non-hex) shipped_in must be "
        f"retained (fail-closed); got candidates={result['candidates']!r}"
    )


def test_active_in_flight_is_not_terminal_via_branch_b(fleet_repo):
    """active + in_flight must NOT qualify Branch B — only shipped/abandoned qualify."""
    handoff_path = fleet_repo.seed_handoff(
        "2026-07-13-active-in-flight.md",
        "active",
        extra_frontmatter="deployment_state: in_flight",
    )
    cid = "state/handoffs/2026-07-13-active-in-flight.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "active+in_flight handoff must never qualify (neither branch admits it); "
        f"got candidates={result['candidates']!r}"
    )

    is_terminal, reason, status_label = _run(
        _is_terminal(handoff_path, [str(handoff_path)], fleet_repo.root, fleet_repo.common_dir)
    )
    assert is_terminal is False
    assert reason == "status='active' (not claimed)"


def test_consumed_non_in_flight_regression_still_terminal(fleet_repo):
    """Regression: existing consumed + non-in_flight case is still terminal via Branch A."""
    fleet_repo.seed_handoff("2026-07-13-consumed-regression.md", "claimed")
    cid = "state/handoffs/2026-07-13-consumed-regression.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid in ids, f"consumed handoff must remain terminal; got {result['candidates']!r}"
    candidate = next(c for c in result["candidates"] if c["id"] == cid)
    # Wire status_label for Branch A is hardcoded "consumed" (frozen wire
    # contract label, not the frontmatter status field) until it flips at C6.
    assert candidate["status"] == "consumed"


def test_consumed_in_flight_regression_still_excluded(fleet_repo):
    """Regression: existing consumed + in_flight case is still excluded (allow_in_flight=False)."""
    fleet_repo.seed_handoff(
        "2026-07-13-consumed-in-flight-regression.md",
        "claimed",
        extra_frontmatter="deployment_state: in_flight",
    )
    cid = "state/handoffs/2026-07-13-consumed-in-flight-regression.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        f"consumed+in_flight handoff must remain excluded; got {result['candidates']!r}"
    )


# ---------------------------------------------------------------------------
# (j) AC7 — Regression oracle: end-to-end prune+archive coherence (C3,
# claim-lock-liveness plan § Regression oracle,
# docs/plans/2026-07-14-claim-lock-liveness-archival-gate-unification.md).
#
# Generalizes the cockpit repro: dead-holder claim locks across all 3 claim
# classes (handoff-claims, memo-claims, plan-claims), a consumed+terminal
# handoff blocked SOLELY by a dead-holder handoff-claims lock, and a
# live-holder handoff-claims lock guarding a second consumed handoff. The
# claim dirs are constructed by REUSING session.reap's own path derivation
# (_CLAIM_SUBDIRS[i] + handoff_path.name, imported above) — never a hand-rolled
# literal — so a convention mismatch between the reaper and the archival gate
# fails this test loudly instead of silently no-opping.
#
# Asserts, after one session.reap sweep + fleet.archive_completed_handoffs
# act run: every dead-holder lock is pruned; the previously-blocked handoff
# now archives (C1); the live-holder lock is NEVER pruned and its handoff is
# NEVER falsely archived (load-bearing safety, AC3); an immediate re-run of
# both ops is a no-op (idempotency).
# ---------------------------------------------------------------------------

_REAP_CLAIM_LIVE_PATCH = "coordinator_core.ops.session.reap.cs_claim_holder_live"
_REAP_LIVE_SIDS_PATCH = "coordinator_core.ops.session.reap.resolve_live_session_ids"
_ARCHIVE_CLAIM_LIVE_PATCH = "coordinator_core.ops.fleet.archive_handoffs.cs_claim_holder_live"


def test_regression_oracle_prune_and_archive_coherence(fleet_repo):
    """AC7: N dead-holder locks (all 3 classes) pruned; previously-blocked
    handoff archives; a live-holder lock is never pruned or falsely archived;
    an immediate re-run is a no-op."""
    dead_target_path = fleet_repo.seed_handoff("2026-07-14-oracle-dead.md", "claimed")
    dead_target_cid = "state/handoffs/2026-07-14-oracle-dead.md"

    live_target_path = fleet_repo.seed_handoff("2026-07-14-oracle-live.md", "claimed")
    live_target_cid = "state/handoffs/2026-07-14-oracle-live.md"

    sessions_dir = _reap_sessions_dir(fleet_repo.common_dir)

    assert set(_CLAIM_SUBDIRS) == {"handoff-claims", "memo-claims", "plan-claims"}, (
        "regression-oracle test assumes reap.py's own 3-class claim set; a "
        f"convention drift here must fail loudly, got {_CLAIM_SUBDIRS!r}"
    )

    # Dead-holder locks — one per class. The handoff-claims dead lock uses the
    # EXACT basename convention the archival gate's Check 4 derives
    # (handoff_path.name, incl. .md) — reused via dead_target_path.name, never
    # hand-rolled.
    dead_handoff_claim_dir = sessions_dir / _CLAIM_SUBDIRS[0] / dead_target_path.name
    dead_memo_claim_dir = sessions_dir / _CLAIM_SUBDIRS[1] / "oracle-memo-dead"
    dead_plan_claim_dir = sessions_dir / _CLAIM_SUBDIRS[2] / "oracle-plan-dead"
    for claim_dir in (dead_handoff_claim_dir, dead_memo_claim_dir, dead_plan_claim_dir):
        claim_dir.mkdir(parents=True, exist_ok=True)

    # Live-holder lock, on the SECOND handoff — must never be pruned or falsely archived.
    live_handoff_claim_dir = sessions_dir / _CLAIM_SUBDIRS[0] / live_target_path.name
    live_handoff_claim_dir.mkdir(parents=True, exist_ok=True)

    def _claim_live(claim_dir_str: str) -> bool:
        return claim_dir_str == str(live_handoff_claim_dir)

    # --- Sweep: session.reap, force=True (bypasses the 12h cadence gate;
    # sub-reap (iii) also runs decoupled from it per C4 regardless — force=True
    # just keeps the whole run cadence-independent, per test_reap.py convention). ---
    with patch(_REAP_LIVE_SIDS_PATCH, return_value=frozenset()), \
         patch(_REAP_CLAIM_LIVE_PATCH, side_effect=_claim_live):
        reap_result = _run(_reap_handler({"force": True}, repo_root=fleet_repo.common_dir))

    assert reap_result["exit_code"] == 0, f"unexpected reap exit_code; {reap_result!r}"
    reaped = set(reap_result["reaped_claims"])
    assert f"handoff-claims/{dead_target_path.name}" in reaped, (
        f"dead-holder handoff-claims lock must be pruned; got {reaped!r}"
    )
    assert "memo-claims/oracle-memo-dead" in reaped, (
        f"dead-holder memo-claims lock must be pruned; got {reaped!r}"
    )
    assert "plan-claims/oracle-plan-dead" in reaped, (
        f"dead-holder plan-claims lock must be pruned; got {reaped!r}"
    )
    assert f"handoff-claims/{live_target_path.name}" not in reaped, (
        "live-holder lock must NEVER be pruned"
    )

    assert not dead_handoff_claim_dir.exists()
    assert not dead_memo_claim_dir.exists()
    assert not dead_plan_claim_dir.exists()
    assert live_handoff_claim_dir.exists(), "live-holder claim dir must survive the sweep"

    # --- Archive: fleet.archive_completed_handoffs act on both candidates. ---
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()), \
         patch(_ARCHIVE_CLAIM_LIVE_PATCH, side_effect=_claim_live):
        archive_result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[dead_target_cid, live_target_cid]),
            repo_root=fleet_repo.common_dir,
        ))

    assert archive_result["exit_code"] == 0, f"unexpected archive exit_code; {archive_result!r}"
    acted_ids = {a["id"] for a in archive_result["acted"]}
    assert dead_target_cid in acted_ids, (
        "the previously-blocked terminal handoff must archive once its "
        f"dead-holder lock is pruned; got acted={archive_result['acted']!r}"
    )
    assert live_target_cid not in acted_ids, (
        "the live-holder-guarded handoff must NEVER be falsely archived"
    )
    skip_map = {s["id"]: s["reason"] for s in archive_result["skipped"]}
    assert live_target_cid in skip_map, f"got skipped={archive_result['skipped']!r}"
    assert "re-live" in skip_map[live_target_cid]

    assert not fleet_repo.path_exists(dead_target_cid), "archived source must be gone"
    assert fleet_repo.path_exists(live_target_cid), "live-guarded source must remain"
    assert fleet_repo.git_status_clean()

    # --- Idempotency: an immediate re-run of both ops is a no-op. ---
    with patch(_REAP_LIVE_SIDS_PATCH, return_value=frozenset()), \
         patch(_REAP_CLAIM_LIVE_PATCH, side_effect=_claim_live):
        reap_replay = _run(_reap_handler({"force": True}, repo_root=fleet_repo.common_dir))
    assert reap_replay["reaped_claims"] == [], (
        "immediate re-sweep must be a no-op — dead locks already pruned; "
        f"got {reap_replay['reaped_claims']!r}"
    )
    assert live_handoff_claim_dir.exists(), "live-holder lock must still survive replay sweep"

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()), \
         patch(_ARCHIVE_CLAIM_LIVE_PATCH, side_effect=_claim_live):
        archive_replay = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[dead_target_cid, live_target_cid]),
            repo_root=fleet_repo.common_dir,
        ))

    assert archive_replay["exit_code"] == 0
    assert archive_replay["acted"] == [], "replay must act on nothing new"
    replay_skip_map = {s["id"]: s["reason"] for s in archive_replay["skipped"]}
    assert replay_skip_map.get(dead_target_cid) == "already-archived", (
        f"replay archive must skip the already-archived id; got {replay_skip_map!r}"
    )
    assert live_target_cid in replay_skip_map
    assert "re-live" in replay_skip_map[live_target_cid]


# ---------------------------------------------------------------------------
# (k) Heir branch (2026-07-22) — "a handoff whose baton has been passed to a
# successor should be archived promptly, not retained".
#
# Coverage: succession edges (predecessor / additional_predecessors) make a
# consumed candidate immediately terminal — bypassing BOTH the A2 in_flight
# exclusion and Check 4's live-claim guard on the heir path only.
# forked_from is NOT a succession edge (DR-224; a spinoff founds its own
# line and does not retire its origin) — a fork-point-only referencing set
# keeps the candidate RETAINED, with a distinguishable note.
#
# Spec backlink: docs/decisions/DR-224-succession-resolves-a-dead-holder-node-supersede-not-release.md
# ---------------------------------------------------------------------------

_ARCHIVE_CLAIM_LIVE_PATCH_HEIR = "coordinator_core.ops.fleet.archive_handoffs.cs_claim_holder_live"


def test_heir_predecessor_child_is_eligible(fleet_repo):
    """consumed + a live child referencing via predecessor + a resolvable
    shipped_in (FIX 1 H4 eligibility gate) → eligible (archived)."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-predecessor.md",
        "claimed",
        extra_frontmatter=f"shipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-22-heir-predecessor.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-predecessor-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel in ids, (
        "consumed handoff succeeded via predecessor edge must be a heir-eligible "
        f"candidate; got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == parent_rel)
    # Wire status_label for Branch A is hardcoded "consumed" (frozen wire
    # contract label, not the frontmatter status field) until it flips at C6.
    assert candidate["status"] == "consumed"
    assert candidate["note"].startswith("consumed; succeeded by "), (
        f"heir-eligible note must be distinguishable; got {candidate['note']!r}"
    )


def test_heir_additional_predecessors_only_child_is_eligible(fleet_repo):
    """consumed + a live child referencing ONLY via additional_predecessors[] +
    a resolvable shipped_in (FIX 1 H4) → eligible."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-addl-pred.md",
        "claimed",
        extra_frontmatter=f"shipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-22-heir-addl-pred.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-addl-pred-child.md",
        "active",
        extra_frontmatter=f"additional_predecessors: [{parent_path}]",
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel in ids, (
        "consumed handoff succeeded via additional_predecessors edge must be "
        f"heir-eligible; got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == parent_rel)
    assert candidate["note"].startswith("consumed; succeeded by ")


def test_heir_forked_from_only_child_is_retained(fleet_repo):
    """consumed + a live child referencing ONLY via forked_from → RETAINED
    (forked_from is derivation ancestry, not succession — DR-224)."""
    parent_path = fleet_repo.seed_handoff("2026-07-22-fork-only.md", "claimed")
    parent_rel = "state/handoffs/2026-07-22-fork-only.md"

    fleet_repo.seed_handoff(
        "2026-07-22-fork-only-child.md",
        "active",
        extra_frontmatter=f"forked_from: {parent_path}\npredecessor: none",
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "consumed handoff with ONLY a forked_from (spinoff) child must remain "
        f"RETAINED — origin baton is still live; got candidates={result['candidates']!r}"
    )

    is_terminal, reason, status_label = _run(
        _is_terminal(
            parent_path,
            [str(parent_path), str(fleet_repo.root / "state" / "handoffs" / "2026-07-22-fork-only-child.md")],
            fleet_repo.root,
            fleet_repo.common_dir,
        )
    )
    assert is_terminal is False
    assert reason.startswith("has fork-point children only:"), (
        f"fork-only retention must carry a distinguishable note; got {reason!r}"
    )
    assert "origin baton still live" in reason
    assert status_label == ""


def test_heir_mixed_fork_and_predecessor_children_is_eligible(fleet_repo):
    """consumed + BOTH a forked_from child AND a predecessor child + a
    resolvable shipped_in (FIX 1 H4) → eligible (any succession edge is
    sufficient; the fork-point child does not veto it)."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-mixed.md",
        "claimed",
        extra_frontmatter=f"shipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-22-mixed.md"

    fleet_repo.seed_handoff(
        "2026-07-22-mixed-fork-child.md",
        "active",
        extra_frontmatter=f"forked_from: {parent_path}\npredecessor: none",
    )
    fleet_repo.seed_handoff(
        "2026-07-22-mixed-succ-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel in ids, (
        "a succession child must make the candidate heir-eligible even when a "
        f"fork-point child ALSO references it; got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == parent_rel)
    assert candidate["note"].startswith("consumed; succeeded by ")


def test_heir_active_status_with_predecessor_child_is_retained(fleet_repo):
    """active (NOT consumed) + a live predecessor child → RETAINED — the heir
    branch is gated on status:consumed (H1); a non-consumed record is never
    eligible via this branch regardless of who references it (fan-in guard)."""
    parent_path = fleet_repo.seed_handoff("2026-07-22-active-with-heir.md", "active")
    parent_rel = "state/handoffs/2026-07-22-active-with-heir.md"

    fleet_repo.seed_handoff(
        "2026-07-22-active-with-heir-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "an active (non-consumed) handoff must never qualify via the heir "
        f"branch, even with a predecessor-referencing child; got candidates={result['candidates']!r}"
    )


def test_heir_bypasses_in_flight_exclusion(fleet_repo):
    """consumed + deployment_state:in_flight + a live predecessor child → eligible.

    This is the exact case the heir branch exists for: a parent is
    status:consumed+deployment_state:in_flight at the moment /handoff writes
    its successor. The heir branch bypasses Check A2 (in_flight hard
    exclusion) on the heir path only — with allow_in_flight=False (the
    standalone op's default), which is the strongest possible proof the
    bypass is heir-specific, not a general A2 loosening. Also carries a
    resolvable shipped_in (FIX 1 H4 eligibility gate)."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-in-flight.md",
        "claimed",
        extra_frontmatter=f"deployment_state: in_flight\nshipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-22-heir-in-flight.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-in-flight-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel in ids, (
        "consumed+in_flight handoff with a live succession child must be "
        f"archived via the heir-branch bypass; got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == parent_rel)
    assert candidate["note"].startswith("consumed; succeeded by ")


def test_in_flight_with_no_heir_still_retained(fleet_repo):
    """consumed + deployment_state:in_flight + NO heir → still retained by the
    existing A2 rule — proves the heir bypass does not leak into the general
    in_flight-exclusion path when there is no succession edge at all."""
    fleet_repo.seed_handoff(
        "2026-07-22-in-flight-no-heir.md",
        "claimed",
        extra_frontmatter="deployment_state: in_flight",
    )
    cid = "state/handoffs/2026-07-22-in-flight-no-heir.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "consumed+in_flight handoff with NO heir must remain excluded (A2 "
        f"exclusion unchanged); got candidates={result['candidates']!r}"
    )


def test_heir_bypasses_live_claim_guard(fleet_repo):
    """consumed + a live claim-dir holder + a live predecessor child → eligible.

    Check 4 (live claim guard) would ordinarily retain this candidate; the
    heir branch bypasses it on the heir path only — the predecessor session
    handing off the baton is typically still (transiently) live/claim-held
    at the exact moment its successor is written. Also carries a resolvable
    shipped_in (FIX 1 H4 eligibility gate)."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-claimed.md",
        "claimed",
        extra_frontmatter=f"shipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-22-heir-claimed.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-claimed-child.md", "active", predecessor=str(parent_path)
    )

    claim_dir = handoff_claim_dir(fleet_repo.common_dir, parent_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    def _claim_live(claim_dir_str: str) -> bool:
        return claim_dir_str == str(claim_dir)

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()), \
         patch(_ARCHIVE_CLAIM_LIVE_PATCH_HEIR, side_effect=_claim_live):
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel in ids, (
        "consumed handoff with a live succession child must be archived via "
        "the heir-branch bypass even while its claim-dir holder is live; "
        f"got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == parent_rel)
    assert candidate["note"].startswith("consumed; succeeded by ")


def test_heir_candidate_carries_structured_heir_field(fleet_repo):
    """F4: the dry_run:true candidate dict carries a structured "heir": True
    field (additive wire field) — not just a note-string a downstream consumer
    would have to pattern-match. Also carries a resolvable shipped_in (FIX 1
    H4 eligibility gate)."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-field.md",
        "claimed",
        extra_frontmatter=f"shipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-22-heir-field.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-field-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    candidate = next(c for c in result["candidates"] if c["id"] == parent_rel)
    assert candidate.get("heir") is True, (
        f"heir-eligible candidate must carry a structured heir:True field; "
        f"got candidate={candidate!r}"
    )


def test_non_heir_candidate_heir_field_is_false(fleet_repo):
    """F4: a plain (non-heir) terminal candidate carries heir:False, not an
    absent/ambiguous key."""
    fleet_repo.seed_handoff("2026-07-22-non-heir-field.md", "claimed")
    cid = "state/handoffs/2026-07-22-non-heir-field.md"

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    candidate = next(c for c in result["candidates"] if c["id"] == cid)
    assert candidate.get("heir") is False, (
        f"non-heir candidate must carry heir:False; got candidate={candidate!r}"
    )


def test_heir_status_labels_accepts_both_dr084_vocabularies():
    """DR-084 P1..P4 window: _HEIR_STATUS_LABELS must accept the sentinel value
    _is_terminal's Branch A ACTUALLY returns today ("consumed") as well as the
    raw vocabulary token it could return if that normalization is ever removed
    ("claimed") — see the constant's own module-level comment for why this is
    defense-in-depth, not a currently-observable bug fix."""
    assert _HEIR_STATUS_LABELS == frozenset({"consumed", "claimed"})


def test_heir_flag_fires_when_is_terminal_reports_claimed_status_label(fleet_repo):
    """Defense-in-depth for the DR-084 widening (status_label in
    _HEIR_STATUS_LABELS, not status_label == "consumed"): proves the is_heir
    wire computation would NOT go blind if _is_terminal's Branch-A
    normalization ever stopped hard-coding the "consumed" sentinel and started
    passing the raw "claimed" vocabulary token through instead. _is_terminal is
    patched directly rather than relying on real frontmatter, because TODAY
    _is_terminal always normalizes status_label to "consumed" regardless of
    the source record's vocabulary (see _HEIR_STATUS_LABELS' module comment)
    — this is the only way to exercise the "claimed" leg of the widened check
    without waiting for that normalization to change out from under it.
    """
    fleet_repo.seed_handoff("2026-07-22-claimed-label-heir.md", "claimed")
    cid = "state/handoffs/2026-07-22-claimed-label-heir.md"

    async def _fake_is_terminal(handoff_path, dag_index, worktree, common_dir, *, allow_in_flight=False):
        return True, f"{_HEIR_NOTE_PREFIX}some-successor.md", "claimed"

    with patch(
        "coordinator_core.ops.fleet.archive_handoffs._is_terminal",
        new=_fake_is_terminal,
    ):
        result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    candidate = next(c for c in result["candidates"] if c["id"] == cid)
    assert candidate.get("heir") is True, (
        "status_label=='claimed' must still be recognized as heir-eligible — "
        f"got candidate={candidate!r}"
    )


def test_heir_archives_at_act_time(fleet_repo):
    """Finding 3: dry_run:false archives a heir candidate — the bypass proven
    at dry_run:true (preview) also holds at act time (D1 re-verify uses the
    same _is_terminal predicate, but this proves it directly rather than by
    inference). Also carries a resolvable shipped_in (FIX 1 H4 eligibility
    gate)."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-act.md",
        "claimed",
        extra_frontmatter=f"deployment_state: in_flight\nshipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-22-heir-act.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-act-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[parent_rel]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    acted_ids = [a["id"] for a in result["acted"]]
    assert parent_rel in acted_ids, (
        f"heir candidate must be archived at act time (dry_run:false); "
        f"got acted={result['acted']!r}, skipped={result['skipped']!r}, "
        f"failed={result['failed']!r}"
    )
    assert not fleet_repo.path_exists(parent_rel), "source must be removed by git mv"

    expected_dest = "archive/handoffs/2026-07/2026-07-22-heir-act.md"
    assert fleet_repo.path_exists(expected_dest), (
        f"heir candidate must physically move to {expected_dest!r}"
    )
    assert fleet_repo.git_status_clean()

    # Finding 1/2 (2026-07-22, code-reviewer slice wsc-heir-A): the archived
    # file's frontmatter must be stamped deployment_state: shipped — this op
    # upholds DR-224's "stamping shipped" promise for ANY caller, not only
    # via session.boot_sweep — and status: consumed must survive untouched.
    archived_meta = _read_meta(str(fleet_repo.root / expected_dest))
    assert archived_meta is not None, (
        f"archived file at {expected_dest!r} must have readable frontmatter"
    )
    assert archived_meta.get("deployment_state") == "shipped", (
        f"heir-archived candidate must be stamped deployment_state: shipped; "
        f"got {archived_meta.get('deployment_state')!r}"
    )
    assert archived_meta.get("status") == "claimed", (
        f"status must remain 'claimed', unmodified by the heir stamp; "
        f"got {archived_meta.get('status')!r}"
    )


def test_heir_branch_childless_falls_through_unchanged(fleet_repo):
    """consumed, genuinely no referencing children of any kind → the heir
    branch's "childless" verdict falls through to the pre-existing
    A2/Check3/Check4 pipeline unchanged (byte-identical note to the
    pre-heir-branch behavior)."""
    handoff_path = fleet_repo.seed_handoff("2026-07-22-childless.md", "claimed")

    is_terminal, note, status_label = _run(
        _is_terminal(handoff_path, [str(handoff_path)], fleet_repo.root, fleet_repo.common_dir)
    )

    assert is_terminal is True
    assert note == "consumed; no live children; no live claim"
    # Wire status_label for Branch A is hardcoded "consumed" (frozen wire
    # contract label, not the frontmatter status field) until it flips at C6.
    assert status_label == "consumed"


def test_heir_branch_error_indeterminate_dag_index_is_retained(fleet_repo):
    """Any error/indeterminate signal partitioning the children (here: an
    empty dag_index, which reverse_membership fail-closes on) RETAINS the
    candidate — never archives on an indeterminate signal."""
    handoff_path = fleet_repo.seed_handoff("2026-07-22-indeterminate.md", "claimed")

    is_terminal, reason, status_label = _run(
        _is_terminal(handoff_path, [], fleet_repo.root, fleet_repo.common_dir)
    )

    assert is_terminal is False
    assert "reverse_membership error" in reason
    assert status_label == ""
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (l) FIX 1 (2026-07-22) — heir eligibility is gated on a resolvable
# shipped_in (H4). `abandoned` retirement is fleet-wide coordinator
# doctrine; reaper-scoped precedent, example-doctrine-repo coordinator/docs/wiki/handoff-
# tracker-system.md:536-540 (2026-07-20): archival only ever happens after a
# handoff reaches shipped; sweep-authored `abandoned` no longer exists
# (that sentence names the reaper, not this op — applied here on the same
# fleet-wide basis).
# ---------------------------------------------------------------------------


def test_heir_no_shipped_in_is_retained(fleet_repo):
    """consumed + a live succession child + NO shipped_in field at all →
    RETAINED, not archived, frontmatter untouched (H4 read-only probe)."""
    parent_path = fleet_repo.seed_handoff("2026-07-22-heir-no-ship.md", "claimed")
    parent_rel = "state/handoffs/2026-07-22-heir-no-ship.md"
    before_text = parent_path.read_text(encoding="utf-8")

    fleet_repo.seed_handoff(
        "2026-07-22-heir-no-ship-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "a heir candidate with no resolvable shipped_in must be RETAINED "
        f"(H4 gate); got candidates={result['candidates']!r}"
    )

    is_terminal, reason, status_label = _run(
        _is_terminal(
            parent_path,
            [str(parent_path), str(fleet_repo.root / "state" / "handoffs" / "2026-07-22-heir-no-ship-child.md")],
            fleet_repo.root,
            fleet_repo.common_dir,
        )
    )
    assert is_terminal is False
    assert reason.startswith("consumed; succeeded by "), (
        f"H4-retention note must be distinguishable; got {reason!r}"
    )
    assert "no resolvable shipped_in" in reason
    assert status_label == ""
    assert fleet_repo.git_status_clean(), "H4 probe must not mutate frontmatter"
    assert parent_path.read_text(encoding="utf-8") == before_text, (
        "H4 is a READ-ONLY probe — frontmatter must be byte-identical after the check"
    )


def test_heir_unresolvable_shipped_in_is_retained(fleet_repo):
    """consumed + a live succession child + a present but UNRESOLVABLE
    (garbage) shipped_in → RETAINED (H4 fail-closed)."""
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-bad-ship.md",
        "claimed",
        extra_frontmatter="shipped_in: deadbeef",
    )
    parent_rel = "state/handoffs/2026-07-22-heir-bad-ship.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-bad-ship-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "a heir candidate with an unresolvable shipped_in must be RETAINED "
        f"(H4 fail-closed gate); got candidates={result['candidates']!r}"
    )


def test_heir_spinoff_roadmap_with_deliverable_id_is_retained(fleet_repo):
    """FIX 2: consumed + kind:spinoff-roadmap + a populated deliverable_id +
    a live succession child + a resolvable shipped_in → RETAINED for the
    promoter (promote-shipped-in-flight-stubs.py's deliverable-spine join),
    mirroring example-doctrine-repo's reaper predicate P1 — even though H4's ship-evidence
    check would otherwise pass, H3 vetoes archival unconditionally."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-roadmap.md",
        "claimed",
        extra_frontmatter=(
            f"shipped_in: {head_sha}\n"
            "kind: spinoff-roadmap\n"
            "deliverable_id: deliv-42"
        ),
    )
    parent_rel = "state/handoffs/2026-07-22-heir-roadmap.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-roadmap-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "a kind:spinoff-roadmap candidate with a populated deliverable_id "
        "must be RETAINED for the promoter, never archived by the heir "
        f"branch; got candidates={result['candidates']!r}"
    )

    is_terminal, reason, status_label = _run(
        _is_terminal(
            parent_path,
            [str(parent_path), str(fleet_repo.root / "state" / "handoffs" / "2026-07-22-heir-roadmap-child.md")],
            fleet_repo.root,
            fleet_repo.common_dir,
        )
    )
    assert is_terminal is False
    assert "spinoff-roadmap" in reason and "deliv-42" in reason, (
        f"spinoff-roadmap retention note must name the promoter as owner; got {reason!r}"
    )
    assert status_label == ""


def test_heir_roadmap_baton_canonical_kind_with_deliverable_id_is_retained(fleet_repo):
    """C4 anti-regression (baton-kind-vocabulary migration): the SAME H3
    retention as test_heir_spinoff_roadmap_with_deliverable_id_is_retained
    above, but using the CANONICAL post-migration `kind: roadmap-baton`
    spelling instead of the retired `spinoff-roadmap` one — this is the
    live defect the migration's D1 vocabulary rename exposed: migrated
    live records now carry `roadmap-baton`, and a literal
    `kind == "spinoff-roadmap"` comparison would silently stop retaining
    them (archival-eligibility regression). Proves `canonical_kind()`
    de-aliasing at this call site covers the canonical spelling, not only
    the retired one."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-29-heir-roadmap-baton.md",
        "claimed",
        extra_frontmatter=(
            f"shipped_in: {head_sha}\n"
            "kind: roadmap-baton\n"
            "deliverable_id: deliv-c4-42"
        ),
    )
    parent_rel = "state/handoffs/2026-07-29-heir-roadmap-baton.md"

    fleet_repo.seed_handoff(
        "2026-07-29-heir-roadmap-baton-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "a kind:roadmap-baton candidate with a populated deliverable_id "
        "must be RETAINED for the promoter, never archived by the heir "
        f"branch; got candidates={result['candidates']!r}"
    )

    is_terminal, reason, status_label = _run(
        _is_terminal(
            parent_path,
            [
                str(parent_path),
                str(fleet_repo.root / "state" / "handoffs" / "2026-07-29-heir-roadmap-baton-child.md"),
            ],
            fleet_repo.root,
            fleet_repo.common_dir,
        )
    )
    assert is_terminal is False
    assert "roadmap-baton" in reason and "deliv-c4-42" in reason, (
        f"roadmap-baton retention note must name the promoter as owner; got {reason!r}"
    )
    assert status_label == ""


def test_heir_spinoff_roadmap_without_deliverable_id_follows_normal_heir_rules(fleet_repo):
    """FIX 2 negative case: consumed + kind:spinoff-roadmap but NO
    deliverable_id + a live succession child + a resolvable shipped_in →
    normal heir rules apply (eligible/archived) — H3 requires BOTH
    conditions, kind alone is not enough."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-heir-roadmap-no-deliv.md",
        "claimed",
        extra_frontmatter=f"shipped_in: {head_sha}\nkind: spinoff-roadmap",
    )
    parent_rel = "state/handoffs/2026-07-22-heir-roadmap-no-deliv.md"

    fleet_repo.seed_handoff(
        "2026-07-22-heir-roadmap-no-deliv-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel in ids, (
        "kind:spinoff-roadmap with NO deliverable_id must fall through to "
        f"normal heir eligibility rules; got candidates={result['candidates']!r}"
    )
    candidate = next(c for c in result["candidates"] if c["id"] == parent_rel)
    assert candidate.get("heir") is True
    assert candidate["note"].startswith("consumed; succeeded by ")


# ---------------------------------------------------------------------------
# (m) AC5 — H3 falsifiability diagnostic: a promoter-owned roadmap-baton
# retained via H3's promoter-owned carve-out (or its Check-A2 in_flight
# fallback, reached when the successor has ITSELF already been archived) must
# be distinguishable from "promoter working normally" when it has gone stale
# with no shipped_in and no live successor left to account for it.
# ---------------------------------------------------------------------------


def _iso_ago(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_h3_stranded_promoter_owned_baton_emits_diagnostic(fleet_repo):
    """AC5: promoter-owned roadmap-baton, ARCHIVED successor, no shipped_in,
    claimed_at well past the staleness threshold -> a diagnostic fires
    naming the deliverable as stranded, even though the candidate is (and
    remains) correctly RETAINED, not archived."""
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-stranded-roadmap.md",
        "claimed",
        extra_frontmatter=(
            "kind: roadmap-baton\n"
            "deliverable_id: dlv-stranded-01\n"
            "deployment_state: in_flight\n"
            f"claimed_at: {_iso_ago(30 * 24 * 3600)}"
        ),
    )
    parent_rel = "state/handoffs/2026-07-22-stranded-roadmap.md"

    # Successor is ARCHIVED (not live) — reverse_membership excludes it from
    # the live-children set, so _classify_heir_children reports "childless"
    # and H3's own branch never runs; the candidate is retained via Check A2
    # (deployment_state: in_flight) instead. The diagnostic must still fire.
    archive_dir = fleet_repo.root / "archive" / "handoffs" / "2026-06"
    archive_dir.mkdir(parents=True, exist_ok=True)
    child_path = archive_dir / "2026-06-01-stranded-roadmap-child.md"
    child_path.write_text(
        f"---\ntitle: \"Archived Successor\"\nstatus: active\ncreated: 2026-06-01\n"
        f"predecessor: {parent_path}\n---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    fleet_repo._git("add", str(child_path))
    fleet_repo._git("commit", "-m", "add archived successor")

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "a promoter-owned roadmap-baton must still be RETAINED, never "
        f"archived by this diagnostic; got candidates={result['candidates']!r}"
    )

    diagnostics = result.get("diagnostics", [])
    diag_ids = [d["id"] for d in diagnostics]
    assert parent_rel in diag_ids, (
        "a stranded promoter-owned roadmap-baton (archived successor, no "
        "shipped_in, stale claimed_at) must emit a diagnostic; "
        f"got diagnostics={diagnostics!r}"
    )
    diag = next(d for d in diagnostics if d["id"] == parent_rel)
    assert "dlv-stranded-01" in diag["diagnostic"]
    assert "stranded" in diag["diagnostic"]


def test_h3_healthy_promoter_owned_baton_recent_claim_no_diagnostic(fleet_repo):
    """Negative case: same shape (promoter-owned, archived successor, no
    shipped_in) but claimed_at is RECENT (well under the staleness
    threshold) -> no diagnostic. "Promoter hasn't run yet since claim" is
    indistinguishable from "promoter never ran" only once the record has
    gone stale; a fresh claim must not false-positive."""
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-healthy-roadmap.md",
        "claimed",
        extra_frontmatter=(
            "kind: roadmap-baton\n"
            "deliverable_id: dlv-healthy-01\n"
            "deployment_state: in_flight\n"
            f"claimed_at: {_iso_ago(60)}"
        ),
    )
    parent_rel = "state/handoffs/2026-07-22-healthy-roadmap.md"

    archive_dir = fleet_repo.root / "archive" / "handoffs" / "2026-06"
    archive_dir.mkdir(parents=True, exist_ok=True)
    child_path = archive_dir / "2026-06-01-healthy-roadmap-child.md"
    child_path.write_text(
        f"---\ntitle: \"Archived Successor\"\nstatus: active\ncreated: 2026-06-01\n"
        f"predecessor: {parent_path}\n---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    fleet_repo._git("add", str(child_path))
    fleet_repo._git("commit", "-m", "add archived successor")

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        f"a promoter-owned roadmap-baton must be RETAINED; got candidates={result['candidates']!r}"
    )

    diagnostics = result.get("diagnostics", [])
    diag_ids = [d["id"] for d in diagnostics]
    assert parent_rel not in diag_ids, (
        "a recently-claimed promoter-owned roadmap-baton (not yet past the "
        f"staleness threshold) must NOT emit a diagnostic; got diagnostics={diagnostics!r}"
    )


def test_h3_stranded_with_one_live_and_one_archived_successor_emits_diagnostic(fleet_repo):
    """Regression (code-reviewer F4): the module comment claims the diagnostic
    fires for BOTH "H3's own branch" (a LIVE succession child) and the
    Check-A2 archived-successor fallback — but those two shapes are, by
    `_classify_heir_children`'s own docstring, mutually exclusive UNLESS the
    candidate has multiple successor children (one live, one already
    archived): `reverse_membership`'s archive-residency exclusion drops only
    the archived child, so `_classify_heir_children` still sees the live one
    and reports `heir_kind == "heir"` — H3's own branch fires, not the Check-A2
    fallback. This constructs exactly that shape and pins that the diagnostic
    still fires on H3's own branch, not only the fallback the other two tests
    exercise."""
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-multi-child-roadmap.md",
        "claimed",
        extra_frontmatter=(
            "kind: roadmap-baton\n"
            "deliverable_id: dlv-multi-child-01\n"
            "deployment_state: in_flight\n"
            f"claimed_at: {_iso_ago(30 * 24 * 3600)}"
        ),
    )
    parent_rel = "state/handoffs/2026-07-22-multi-child-roadmap.md"

    # Archived successor — invisible to reverse_membership's live scan.
    archive_dir = fleet_repo.root / "archive" / "handoffs" / "2026-06"
    archive_dir.mkdir(parents=True, exist_ok=True)
    child_path = archive_dir / "2026-06-01-multi-child-roadmap-archived.md"
    child_path.write_text(
        f"---\ntitle: \"Archived Successor\"\nstatus: active\ncreated: 2026-06-01\n"
        f"predecessor: {parent_path}\n---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    fleet_repo._git("add", str(child_path))
    fleet_repo._git("commit", "-m", "add archived successor")

    # Live successor — makes _classify_heir_children report heir_kind == "heir",
    # so H3's OWN branch vetoes archival (not the Check-A2 in_flight fallback).
    fleet_repo.seed_handoff(
        "2026-07-22-multi-child-roadmap-live.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        "a promoter-owned roadmap-baton with a live heir must be RETAINED via "
        f"H3's own branch; got candidates={result['candidates']!r}"
    )

    diagnostics = result.get("diagnostics", [])
    diag_ids = [d["id"] for d in diagnostics]
    assert parent_rel in diag_ids, (
        "a stranded promoter-owned roadmap-baton must emit a diagnostic even "
        "when H3's OWN branch (a live heir) is what vetoes archival, not only "
        f"the Check-A2 archived-successor fallback; got diagnostics={diagnostics!r}"
    )
    diag = next(d for d in diagnostics if d["id"] == parent_rel)
    assert "dlv-multi-child-01" in diag["diagnostic"]
    assert "stranded" in diag["diagnostic"]


def test_h3_promoter_owned_baton_with_shipped_in_no_diagnostic(fleet_repo):
    """Negative case: a promoter-owned roadmap-baton that already carries a
    resolvable shipped_in must not be flagged as stranded — it is (or is
    about to be) promoted, not silently ignored. Uses a LIVE succession
    child (H3's own branch, not the Check-A2 fallback) to also assert the
    diagnostic stays silent on the H3-branch shape when shipped_in is
    present."""
    head_sha = fleet_repo._git("rev-parse", "HEAD").stdout.decode().strip()
    parent_path = fleet_repo.seed_handoff(
        "2026-07-22-shipped-roadmap.md",
        "claimed",
        extra_frontmatter=(
            f"shipped_in: {head_sha}\n"
            "kind: roadmap-baton\n"
            "deliverable_id: dlv-shipped-01\n"
            f"claimed_at: {_iso_ago(30 * 24 * 3600)}"
        ),
    )
    parent_rel = "state/handoffs/2026-07-22-shipped-roadmap.md"

    fleet_repo.seed_handoff(
        "2026-07-22-shipped-roadmap-child.md", "active", predecessor=str(parent_path)
    )

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert parent_rel not in ids, (
        f"H3 must still veto archival unconditionally; got candidates={result['candidates']!r}"
    )

    diagnostics = result.get("diagnostics", [])
    diag_ids = [d["id"] for d in diagnostics]
    assert parent_rel not in diag_ids, (
        "a promoter-owned roadmap-baton with a resolvable shipped_in must "
        f"NOT be flagged as stranded; got diagnostics={diagnostics!r}"
    )


# ---------------------------------------------------------------------------
# (l) dag_index scan-incomplete — glob-swallows-PermissionError regression
#     guard.
#
# _collect_all_handoff_paths previously used rglob("*.md") for the archived
# subtree (Path.glob()/rglob()'s selector silently swallows PermissionError
# while walking — unreadable dir -> empty iterator, no exception) and relied
# on _common.collect_live_handoff_paths's own bare `except OSError: return []`
# for the live subtree — both dead code for the exact permission-denied case
# they existed to guard.  A successor sitting under either unreadable subtree
# would make reverse_membership's childless/heir classification silently
# WRONG: a truly-live child invisible to a partial dag_index reads as
# "childless", and the predecessor would be archived — or worse, a heir
# candidate would be misclassified — out from under a scan we know is
# incomplete.  The fix marks the whole invocation dag_incomplete and fails
# closed: nothing is reclassified or archived when the scan cannot be
# trusted.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_archived_subtree_dry_run_returns_zero_candidates(fleet_repo, caplog):
    """An unreadable archive/handoffs/YYYY-MM/ subdir marks the dag_index
    incomplete — an otherwise-archivable consumed+childless+unclaimed handoff
    in state/handoffs/ must NOT be offered as a candidate, because the
    unreadable subtree could be hiding a live successor that would otherwise
    retain it (heir-succession vs. abandoned classification must never
    silently flip on a partial scan).
    """
    fleet_repo.seed_handoff("2026-07-22-scan-incomplete.md", "claimed")

    archive_month_dir = fleet_repo.root / "archive" / "handoffs" / "2026-07"
    archive_month_dir.mkdir(parents=True, exist_ok=True)
    (archive_month_dir / "2026-07-01-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = archive_month_dir.stat().st_mode
    os.chmod(archive_month_dir, 0o000)
    try:
        with caplog.at_level(
            logging.WARNING, logger="coordinator_core.ops.fleet.archive_handoffs"
        ):
            result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))
    finally:
        os.chmod(archive_month_dir, original_mode)

    assert result["candidates"] == [], (
        "an otherwise-terminal handoff must NOT be offered as a candidate "
        f"when the dag_index scan is incomplete; got {result['candidates']!r}"
    )
    assert any(str(archive_month_dir) in r.message for r in caplog.records), (
        "expected a logged WARNING naming the unreadable archived handoff dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_archived_subtree_act_skips_all_candidates(fleet_repo):
    """The same otherwise-archivable handoff, explicitly candidate_id'd on the
    act path while an archive/handoffs/ subdir is unreadable, must be skipped
    (never archived) — the dag_incomplete fail-closed applies at T3 too.
    """
    parent_path = fleet_repo.seed_handoff("2026-07-22-scan-incomplete-act.md", "claimed")
    parent_rel = "state/handoffs/2026-07-22-scan-incomplete-act.md"

    archive_month_dir = fleet_repo.root / "archive" / "handoffs" / "2026-07"
    archive_month_dir.mkdir(parents=True, exist_ok=True)
    (archive_month_dir / "2026-07-01-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = archive_month_dir.stat().st_mode
    os.chmod(archive_month_dir, 0o000)
    try:
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[parent_rel]),
            repo_root=fleet_repo.common_dir,
        ))
    finally:
        os.chmod(archive_month_dir, original_mode)

    assert result["acted"] == [], (
        "a candidate must NOT be archived while the dag_index scan is incomplete"
    )
    skipped_reasons = {s["id"]: s["reason"] for s in result["skipped"]}
    assert skipped_reasons.get(parent_rel, "").startswith("dag-scan-incomplete"), (
        f"expected a dag-scan-incomplete skip reason; got {skipped_reasons!r}"
    )
    assert parent_path.exists(), (
        "handoff must remain in place — never moved on an incomplete dag scan"
    )


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_live_handoffs_dir_dry_run_returns_zero_candidates(fleet_repo, caplog):
    """An unreadable state/handoffs/ dir also marks the dag_index incomplete
    (the live-subtree half of _collect_all_handoff_paths) — preview must fail
    closed to zero candidates rather than crash or silently proceed on a
    dag_index missing every live handoff.
    """
    fleet_repo.seed_handoff("2026-07-22-live-scan-incomplete.md", "claimed")

    state_dir = fleet_repo.root / "state" / "handoffs"
    original_mode = state_dir.stat().st_mode
    os.chmod(state_dir, 0o000)
    try:
        with caplog.at_level(
            logging.WARNING, logger="coordinator_core.ops.fleet.archive_handoffs"
        ):
            result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))
    finally:
        os.chmod(state_dir, original_mode)

    assert result["candidates"] == [], (
        f"expected zero candidates on an unreadable live-handoffs dir; got {result['candidates']!r}"
    )
    assert any(str(state_dir) in r.message for r in caplog.records), (
        "expected a logged WARNING naming the unreadable state/handoffs dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )

"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_call_site_coverage

AC5 of docs/plans/2026-08-05-resync-stages-the-committed-blob.md (chunk C5a) —
per-call-site coverage for the C1 `--cacheinfo` main-index-resync fix in
`coordinator_core/ops/fleet/_common.py::archive_and_commit`.

Reconciliation source of truth (DO NOT re-derive; encode only):
state/sizings/2026-08-05-arm-b-dirty-src-resync-stages-worktree-blob.yaml
`scout_evidence`'s call-site table. Grep-verified 10 literal `archive_and_commit(`
call expressions: 8 purely LIVE, 1 MIXED (archive_plans.py — one call builds both
a live `sidecar_moves` list and a partially-guarded `primary_move`), 1 UNPROVEN
(archive_paper_trail.py — out of scope here per C8/C10's D2, and per this plan's
own C5 body).

The defect under coverage (Arm B, test_index_residue_reproduction.py): a dirty
`src` at archival time, with the caller's `Move.restage_src` left at its
documented default `False`, formerly caused a FULLY SUCCESSFUL post-commit
main-index resync to stage `dst`'s on-disk (dirty) content into the shared main
index as a modification — silently absorbable into a bystander session's next
bare `git commit`. C1 (2026-08-05) replaced the plain `--add -- dst` resync leg
with `git ls-tree HEAD -- dst` + `update-index --add --cacheinfo <mode> <sha>
dst`, so the resync now stages exactly what the archival commit RECORDED for
dst, leaving a peer's dirty edit as an UNSTAGED worktree modification instead.

WHY a single parametrized test discharges "per call site", not 9 near-identical
op-level integration tests (structural judgment delegated by the plan body):
the C1 defect and fix live ENTIRELY inside `archive_and_commit`'s shared
post-commit resync block — that block does not branch on which caller invoked
it. The only per-call-site variables that could plausibly change resync
exposure are (a) the exact `restage_src` value the site passes for its
live-classified Move(s), and (b) whether that Move shares one
`archive_and_commit` batch call with sibling Moves (so a per-item independence
question — does one dirty Move in a batch corrupt/skip resync for a clean
sibling? — is also live). Both are captured directly below, using each site's
OWN literal value (cited by file:line, matching the reconciliation table) —
this is call-site fidelity for the ONLY axis the mechanism can vary on, not a
cosmetic renaming of Arm B. Building all 9 sites' own realistic candidate-
discovery/claim-liveness/terminality-guard fixtures (the ~500-2000 line
per-op test modules already do this for their own ACs) would be pure
contortion for THIS AC — none of that machinery touches the resync mechanism.

Where a site's real Move-construction is itself extractable/cheap to invoke
end-to-end (archive_release_accumulator's handler is a thin ~15-line wrapper
over archive_and_commit with no candidate-discovery guards), this file also
covers it via the real `_handler()` for stronger fidelity — see
test_archive_release_accumulator_handler_end_to_end_dirty_src below.

CF1 (2026-08-05) — op-level `_handler()` coverage, per-site disposition:
the C5a executor's own reasoning ("would require duplicating large swaths of
each op's own 500-2000 line candidate-discovery/claim-liveness fixture
machinery") was tested here, not accepted on argument, per the PM's ruling.
Finding: it does NOT hold for any of the 9 live-classified sites — EVERY one
already had a small, directly-reusable "act" test in its own per-op test
module (or, for handoff_archive_transition.py, in the sibling
coordinator_core/ops/tests/ tree) that seeds one terminal candidate and calls
the op's real `_handler`/internal callable with no mocking of the
resync-relevant path. This file borrows those fixtures (imported, not
duplicated) and adds one dirty-src variant per site below:
  - row1 (handoff_archive_transition.py, do_stamp=False/chain mode):
    test_row1_handoff_archive_transition_chain_mode_handler_end_to_end_dirty_src
    — reuses coordinator_core.ops.tests.conftest.HandoffRepo (imported) with a
    locally inlined ~10-line git-init (that fixture's own `handoff_repo`
    pytest fixture is not visible from this file's directory — no ancestor
    conftest.py relationship — so the class is imported and driven directly
    against `tmp_path`, not the fixture function).
  - row2 (archive_actioned_memos.py:381, archive_actioned_memos_internal):
    test_row2_archive_actioned_memos_internal_end_to_end_dirty_src
  - row3 (archive_actioned_memos.py:532, _handler act path):
    test_row3_archive_actioned_memos_handler_end_to_end_dirty_src
  - row4 (archive_handoffs.py:1380, non-heir branch):
    test_row4_archive_handoffs_handler_end_to_end_dirty_src
  - row6 (archive_plans.py:811, sidecar — the LIVE portion of the MIXED row):
    test_row6_archive_plans_handler_end_to_end_dirty_sidecar
  - row7 (archive_queue_entry.py:227 — the plan's ONLY currently-live-
    triggered site, per prune-closed-improvements.py's route(...) call):
    test_row7_archive_queue_entry_handler_end_to_end_dirty_src
  - row8 (archive_release_accumulator.py:185): already covered above by
    test_archive_release_accumulator_handler_end_to_end_dirty_src (pre-dates
    this section; not duplicated).
  - row9 (archive_shipped_handoffs.py:331):
    test_row9_archive_shipped_handoffs_handler_end_to_end_dirty_src
  - row10 (prune_bugs.py:277):
    test_row10_prune_bugs_handler_end_to_end_dirty_src

No site required a structural-property assertion in place of op-level
coverage — the "genuinely not tractable" branch this file's own docstring
anticipated turned out to have zero members among the 9 live sites. The one
remaining site, archive_paper_trail.py (row5, UNPROVEN), stays out of scope
per C8/C10's D2 and is NOT re-litigated here — no coverage of any kind was
attempted for it.

Per-site restage_src values used below (verified against disk at write time,
matching the reconciliation table's grep-verified line numbers):
  1. coordinator_core/ops/handoff_archive_transition.py:1543   restage_src=do_stamp (False branch tested — the else-False arm of the conditional; do_stamp=True is a DIFFERENT, already-documented-safe "op-authored pre-move content" mechanism, not the Arm B foreign-dirty-src hazard — see archive_and_commit's own docstring note, out of scope for this defect)
  2. coordinator_core/ops/fleet/archive_actioned_memos.py:381  restage_src=False (default; archive_actioned_memos_internal)
  3. coordinator_core/ops/fleet/archive_actioned_memos.py:532  restage_src=False (default; _handle_act)
  4. coordinator_core/ops/fleet/archive_handoffs.py:1380       restage_src=is_heir (False branch tested — non-heir candidates; heir True branch is the same op-authored-content mechanism as row 1)
  6. coordinator_core/ops/fleet/archive_plans.py:811            restage_src=False (default) for BOTH primary_move and sidecar Moves (_build_moves_for_plan) — the MIXED row: primary_move additionally passes a T3 dirty-tree guard (_plan_worktree_dirty) before the Move is ever built; sidecar Moves carry no such guard, so sidecar is the LIVE portion under test
  7. coordinator_core/ops/fleet/archive_queue_entry.py:227      restage_src=False (default)
  8. coordinator_core/ops/fleet/archive_release_accumulator.py:185 restage_src=False (default)
  9. coordinator_core/ops/fleet/archive_shipped_handoffs.py:331 restage_src=False (default kwarg on _handle_act; only handoff_ship_archive.py's single-handoff composite opts True)
 10. coordinator_core/ops/fleet/prune_bugs.py:277                restage_src=False (default)

archive_release_accumulator EXEMPTION (structural property, itself asserted —
see test_archive_release_accumulator_no_invoker_in_either_repo): registered
but has NO invoker anywhere in either repo (claude-klabauter or example-doctrine-repo) —
merging-to-main's SKILL.md still spells the raw `git mv`. It is COVERED below
(both via the shared parametrized mechanism test and via a real end-to-end
`_handler()` call) but MUST NOT be read as currently live-triggered — this is
the treatment the plan body names as the rule, not the exception, for any
future exemption in this file.

Spec backlinks:
  - AC5: docs/plans/2026-08-05-resync-stages-the-committed-blob.md, chunk C5
  - Reconciliation table: state/sizings/2026-08-05-arm-b-dirty-src-resync-stages-worktree-blob.yaml
  - Mechanism + fix: coordinator_core/ops/fleet/_common.py archive_and_commit
    (_ls_tree_head_cacheinfo / _update_index_with_retry)
  - Generic mechanism reproduction: test_index_residue_reproduction.py (Arm B)

Negative-spec:
  - Does NOT re-derive triggerability (which sites see a dirty src in
    production today) — that is established by sweep and encoded in the
    module docstring above, not re-proven here.
  - Does NOT cover archive_paper_trail.py:260 — UNPROVEN, out of scope (C8/C10's D2).
  - Does NOT modify any existing per-op test file (test_archive_handoffs.py,
    test_archive_plans.py, test_archive_queue_entry.py, etc.) — this is a NEW,
    additive file. CF1's op-level tests IMPORT seed helpers from
    test_archive_actioned_memos.py, test_archive_queue_entry.py, and
    test_archive_shipped_handoffs.py, and import the HandoffRepo class from
    coordinator_core/ops/tests/conftest.py — importing is not modifying;
    none of those files' bytes changed.
  - Does NOT exercise this against the live working tree — every case runs
    inside the `fleet_repo` fixture's throwaway tmp_path git repo
    (coordinator_core/ops/fleet/tests/conftest.py), or (row1 only) an
    equivalent ad-hoc tmp_path git repo built inline.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit

# Import guard: fires the @register_op side-effect for
# fleet.archive_release_accumulator, used by the end-to-end handler test below.
import coordinator_core.ops.fleet.archive_release_accumulator  # noqa: F401
from coordinator_core.ops.fleet.archive_release_accumulator import _handler as _release_accumulator_handler

# ---------------------------------------------------------------------------
# CF1 imports — real op-level `_handler()`/internal callables + each site's
# own per-op test module's seed helper, BORROWED (not duplicated). Each
# import also fires the @register_op side-effect for that op as a side
# effect of importing its defining module.
# ---------------------------------------------------------------------------
import coordinator_core.ops.fleet.archive_actioned_memos  # noqa: F401
import coordinator_core.ops.fleet.archive_handoffs  # noqa: F401
import coordinator_core.ops.fleet.archive_plans  # noqa: F401
import coordinator_core.ops.fleet.archive_queue_entry  # noqa: F401
import coordinator_core.ops.fleet.archive_shipped_handoffs  # noqa: F401
import coordinator_core.ops.fleet.prune_bugs  # noqa: F401
import coordinator_core.ops.handoff_archive_transition  # noqa: F401

from coordinator_core.ops.fleet.archive_actioned_memos import (
    _handler as _actioned_memos_handler,
    archive_actioned_memos_internal,
)
from coordinator_core.ops.fleet.archive_handoffs import _handler as _archive_handoffs_handler
from coordinator_core.ops.fleet.archive_plans import _archive_completed_plans
from coordinator_core.ops.fleet.archive_queue_entry import _handler as _archive_queue_entry_handler
from coordinator_core.ops.fleet.archive_shipped_handoffs import _handler as _archive_shipped_handoffs_handler
from coordinator_core.ops.fleet.prune_bugs import _handler as _prune_bugs_handler
from coordinator_core.ops.handoff_archive_transition import _handler as _handoff_archive_transition_handler

# Per-op-test-module seed helpers, borrowed verbatim rather than rebuilt.
from coordinator_core.ops.fleet.tests.test_archive_actioned_memos import _seed_memo
from coordinator_core.ops.fleet.tests.test_archive_queue_entry import _seed_queue_entry
from coordinator_core.ops.fleet.tests.test_archive_shipped_handoffs import (
    _head_sha as _shipped_handoff_head_sha,
    _seed_shipped_handoff,
)
from coordinator_core.ops.tests.conftest import HandoffRepo

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[4]


def _resolve_sibling_root() -> Path | None:
    """The sibling repo's root, resolved through the machine-local registry
    rather than a hardcoded adjacent-directory guess.

    The literal traversal this replaces trips
    `test_no_hardcoded_paths.py::test_no_hardcoded_cross_repo_paths_in_production_code`
    ('sibling-repo-crossing-traversal'): a hardcoded sibling path is wrong on
    any machine whose checkouts are not adjacent, and this repo treats
    install-surface portability as first-class.

    Returns None when the pointer is unset or names a missing directory, so the
    one test that consults it skips rather than failing on a checkout with no
    sibling clone.
    """
    from coordinator_core.doe_root_pointer import read_doe_root_pointer

    pointer = read_doe_root_pointer()
    if not pointer:
        return None
    root = Path(pointer)
    return root if root.is_dir() else None


# Captured at COLLECTION time, under the real (un-quarantined) HOME — the same
# technique, and for the same reason, as coordinator_core/conftest.py's own
# collection-time capture. That conftest's autouse fixture repoints HOME and
# USERPROFILE at a per-test quarantine dir, and the doe-root pointer resolves
# through a registry/pointer file under HOME. Resolving this inside a test body
# therefore always returns "", which would silently turn the cross-repo leg
# below into a permanent skip — a green suite asserting nothing, which is worse
# than the hardcoded path this replaces.
_EXAMPLE_DOCTRINE_REPO_ROOT = _resolve_sibling_root()


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed.

    Mirrors test_index_residue_reproduction.py's identical helper.
    """
    return asyncio.run(coro)


def _write_and_commit(fleet_repo, rel_path: str, content: str) -> Path:
    """Write + commit an arbitrary repo-relative file — mirrors conftest.py's
    seed_* helpers but without imposing any op's frontmatter shape, since this
    file tests the resync mechanism, not any op's candidate-discovery logic."""
    path = fleet_repo.root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add {rel_path}")
    return path


def _dirty(path: Path, content: str) -> None:
    """Uncommitted worktree edit — NOT staged. Mirrors Arm B's dirty-src setup."""
    path.write_text(content, encoding="utf-8")


def _status_line_for(fleet_repo, rel_path: str) -> str:
    """Return the `git status --porcelain` line for rel_path, or "" if clean."""
    out = fleet_repo._git_unchecked("status", "--porcelain", "--", rel_path).stdout.decode()
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[0] if lines else ""


# ---------------------------------------------------------------------------
# The 9 call sites carrying a live Move — single parametrized mechanism test.
# ---------------------------------------------------------------------------
#
# Each case: (label, clean_src_rel, clean_dst_rel, dirty_src_rel, dirty_dst_rel,
# restage_src_for_dirty). The "clean" pair is a bystander Move in the SAME
# archive_and_commit batch call as the "dirty" one, for every site that
# genuinely batches multiple Moves in production (memos, handoffs, plans
# sidecars, shipped handoffs, bugs) — proving per-item resync independence,
# not just single-Move behaviour. Single-Move sites (handoff_archive_transition,
# archive_queue_entry, archive_release_accumulator) are tested with one Move,
# matching their real call shape (never a batch in production).

_CALL_SITES = [
    pytest.param(
        "handoff_archive_transition.py:1543 (restage_src=do_stamp, False branch)",
        None, None,
        "state/handoffs/2026-01-01-a.md", "archive/handoffs/2026-01/2026-01-01-a.md",
        False,
        id="row1-handoff_archive_transition",
    ),
    pytest.param(
        "archive_actioned_memos.py:381 (archive_actioned_memos_internal)",
        "cross-repo/inbox/2026-01-01-b.md", "cross-repo/archive/2026-01-01-b.md",
        "cross-repo/inbox/2026-01-01-c.md", "cross-repo/archive/2026-01-01-c.md",
        False,
        id="row2-archive_actioned_memos_internal",
    ),
    pytest.param(
        "archive_actioned_memos.py:532 (_handle_act)",
        "cross-repo/inbox/2026-01-02-b.md", "cross-repo/archive/2026-01-02-b.md",
        "cross-repo/inbox/2026-01-02-c.md", "cross-repo/archive/2026-01-02-c.md",
        False,
        id="row3-archive_actioned_memos_handle_act",
    ),
    pytest.param(
        "archive_handoffs.py:1380 (restage_src=is_heir, False branch)",
        "state/handoffs/2026-01-03-x.md", "archive/handoffs/2026-01/2026-01-03-x.md",
        "state/handoffs/2026-01-03-y.md", "archive/handoffs/2026-01/2026-01-03-y.md",
        False,
        id="row4-archive_handoffs_non_heir",
    ),
    pytest.param(
        "archive_queue_entry.py:227",
        None, None,
        "state/improvement-queue/2026-01-01-item.yaml",
        "archive/improvement-queue/2026-01/2026-01-01-item.yaml",
        False,
        id="row7-archive_queue_entry",
    ),
    pytest.param(
        "archive_release_accumulator.py:185",
        None, None,
        "state/week-changelog/2026-01-01-pending-release.md",
        "archive/release-notes/2026-01-01-v1.0.0-pending-release.md",
        False,
        id="row8-archive_release_accumulator",
    ),
    pytest.param(
        "archive_shipped_handoffs.py:331 (restage_src default False)",
        "state/handoffs/2026-01-04-x.md", "archive/handoffs/2026-01/2026-01-04-x.md",
        "state/handoffs/2026-01-04-y.md", "archive/handoffs/2026-01/2026-01-04-y.md",
        False,
        id="row9-archive_shipped_handoffs",
    ),
    pytest.param(
        "prune_bugs.py:277",
        "state/bug-backlog/2026-01-01-a.yaml", "archive/bug-backlog/2026-01/2026-01-01-a.yaml",
        "state/bug-backlog/2026-01-01-b.yaml", "archive/bug-backlog/2026-01/2026-01-01-b.yaml",
        False,
        id="row10-prune_bugs",
    ),
]


@pytest.mark.parametrize(
    "label,clean_src,clean_dst,dirty_src,dirty_dst,restage_src", _CALL_SITES,
)
def test_call_site_dirty_src_resync_leaves_dst_unstaged(
    fleet_repo, label, clean_src, clean_dst, dirty_src, dirty_dst, restage_src,
):
    """For each of 8 purely-LIVE call sites (row 6 — archive_plans — is covered
    separately below because its MIXED nature needs primary+sidecar in one
    batch, not a generic clean+dirty pair): construct the Move(s) with EXACTLY
    that site's own restage_src value, dirty the live-classified src at
    archival time, run them through the real archive_and_commit, and assert
    the C1 fix holds — dst lands as an UNSTAGED worktree modification, never a
    staged one, so a subsequent bystander bare commit cannot absorb it.
    """
    moves = []
    if clean_src is not None:
        clean_src_path = _write_and_commit(fleet_repo, clean_src, "clean content\n")
        moves.append(Move(
            src=clean_src_path, dst=fleet_repo.root / clean_dst,
            candidate_id=clean_src,
        ))

    dirty_src_path = _write_and_commit(fleet_repo, dirty_src, "original content\n")
    _dirty(dirty_src_path, "DIRTY uncommitted edit\n")
    moves.append(Move(
        src=dirty_src_path, dst=fleet_repo.root / dirty_dst,
        candidate_id=dirty_src, restage_src=restage_src,
    ))

    subject = f"test archival: {label}"
    acted, failed = _run(archive_and_commit(fleet_repo.root, moves, subject))

    assert failed == [], f"{label}: unexpected failures {failed!r}"
    assert len(acted) == len(moves), f"{label}: expected all moves acted, got {acted!r}"
    for item in acted:
        assert "index_resync_failed" not in item, (
            f"{label}: resync should fully succeed (no retry exhaustion) — got {item!r}"
        )

    # HEAD holds the STALE (pre-dirty, last-committed) blob at dirty_dst —
    # git mv against the private index never rehashed the dirty on-disk content.
    head_content = fleet_repo._git("show", f"HEAD:{dirty_dst}").stdout.decode()
    assert head_content == "original content\n", (
        f"{label}: HEAD content at dst should be the pre-dirty committed blob"
    )

    # The C1 fix: dst is an UNSTAGED worktree modification (leading space " M"),
    # NEVER a staged one ("M "), which is the exact shape a bystander bare
    # `git commit` (no pathspec) would absorb.
    dirty_status = _status_line_for(fleet_repo, dirty_dst)
    assert dirty_status.startswith(" M"), (
        f"{label}: expected dst to be an UNSTAGED modification (' M {dirty_dst}'); "
        f"got {dirty_status!r} — a staged 'M ' here is the Arm B defect reproducing"
    )

    if clean_src is not None:
        # Per-item independence: the clean sibling Move in the SAME batch call
        # resynced cleanly — no residue at all, staged or unstaged.
        clean_status = _status_line_for(fleet_repo, clean_dst)
        assert clean_status == "", (
            f"{label}: clean sibling Move should leave no residue; got {clean_status!r}"
        )


# ---------------------------------------------------------------------------
# Row 6 — archive_plans.py:811 — MIXED: primary_move (partially-guarded) +
# sidecar_moves (LIVE, no dirty-tree guard) in ONE archive_and_commit batch.
# ---------------------------------------------------------------------------


def test_row6_archive_plans_sidecar_move_is_the_live_portion(fleet_repo):
    """archive_plans.py's _build_moves_for_plan constructs moves[0] (primary)
    and moves[1:] (sidecars) with IDENTICAL restage_src=False, but only the
    primary passes a T3 dirty-tree guard (_plan_worktree_dirty) before its
    Move is ever built — the sidecar carries no such guard (module docstring
    "Unreconciled-AC skip guard" / _handle_act's own docstring point 8: "build
    Move (primary + sidecars)"). This is the reconciliation table's MIXED row:
    a dirty PRIMARY never reaches archive_and_commit at all (skipped upstream,
    outside this file's scope — that guard's own correctness is
    test_archive_plans.py's concern), but a dirty SIDECAR does — proving that
    portion is exposed to, and closed by, the same C1 fix as every other LIVE
    site.
    """
    primary_src = _write_and_commit(
        fleet_repo, "docs/plans/2026-01-01-my-plan.md", "plan body\n",
    )
    sidecar_src = _write_and_commit(
        fleet_repo, "docs/plans/2026-01-01-my-plan.review.md", "sidecar body\n",
    )
    _dirty(sidecar_src, "DIRTY sidecar edit\n")

    primary_dst = "archive/specs/2026-01/2026-01-01-my-plan.md"
    sidecar_dst = "archive/specs/2026-01/2026-01-01-my-plan.review.md"

    moves = [
        Move(src=primary_src, dst=fleet_repo.root / primary_dst,
             candidate_id="docs/plans/2026-01-01-my-plan.md"),
        Move(src=sidecar_src, dst=fleet_repo.root / sidecar_dst,
             candidate_id="sidecar-for:docs/plans/2026-01-01-my-plan.md:2026-01-01-my-plan.review.md"),
    ]
    acted, failed = _run(archive_and_commit(
        fleet_repo.root, moves, "fleet: archive 1 terminal plan(s) [fleet.archive_completed_plans]",
    ))

    assert failed == []
    assert len(acted) == 2
    for item in acted:
        assert "index_resync_failed" not in item

    # Primary: clean src, no residue at all.
    assert _status_line_for(fleet_repo, primary_dst) == ""

    # Sidecar: the LIVE portion — dirty content stays an unstaged modification,
    # not absorbed into the shared index.
    sidecar_status = _status_line_for(fleet_repo, sidecar_dst)
    assert sidecar_status.startswith(" M"), (
        f"expected sidecar dst to be an UNSTAGED modification; got {sidecar_status!r}"
    )
    head_sidecar = fleet_repo._git("show", f"HEAD:{sidecar_dst}").stdout.decode()
    assert head_sidecar == "sidecar body\n"


# ---------------------------------------------------------------------------
# CF1 — op-level `_handler()` coverage for rows 1, 2, 3, 4, 6 (already above
# for 6, kept together with its own MIXED-row test), 7, 9, 10. See the
# module docstring's "CF1 (2026-08-05)" paragraph for the per-row index and
# the honesty finding (op-level coverage was tractable everywhere it was
# tried — no site needed a structural-property fallback).
# ---------------------------------------------------------------------------


def _porcelain_status(root: Path, rel_path: str) -> str:
    """Return the `git status --porcelain` line for rel_path under root, or ""
    if clean. Root-generic sibling of _status_line_for (which is FleetRepo-
    bound) — row1 below drives an ad-hoc HandoffRepo, not a fleet_repo."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", rel_path],
        cwd=str(root), capture_output=True,
    )
    lines = [l for l in proc.stdout.decode().splitlines() if l.strip()]
    return lines[0] if lines else ""


def _head_blob(root: Path, rel_path: str) -> str:
    """Return the git-committed blob content for rel_path at HEAD under root."""
    proc = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        cwd=str(root), capture_output=True, check=True,
    )
    return proc.stdout.decode()


def test_row7_archive_queue_entry_handler_end_to_end_dirty_src(fleet_repo):
    """Real fleet.archive_queue_entry `_handler()` against a dirty
    improvement-queue entry — the plan's ONLY currently-live-triggered call
    site (prune-closed-improvements.py's route(...) call from /update-docs
    Phase 11i; entries are written dirty BY DESIGN by
    coordinator-queue-append). Reuses test_archive_queue_entry.py's own
    `_seed_queue_entry` fixture verbatim.
    """
    name = "2026-01-01-dirty-item.yaml"
    src = _seed_queue_entry(fleet_repo, name)
    src.write_text(
        "created: 2026-01-01\nfrom_repo: test-repo\nqueue_scope: project\n"
        "title: \"Test Queue Entry\"\nbody: \"DIRTY uncommitted edit\"\nstatus: open\n",
        encoding="utf-8",
    )

    result = _run(_archive_queue_entry_handler(
        {"entry_path": f"state/improvement-queue/{name}", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is True, result
    dest_rel = result["dest"]
    status = _porcelain_status(fleet_repo.root, dest_rel)
    assert status.startswith(" M"), (
        f"expected dest to be an UNSTAGED modification; got {status!r}"
    )
    assert "DIRTY uncommitted edit" not in _head_blob(fleet_repo.root, dest_rel)


def test_row10_prune_bugs_handler_end_to_end_dirty_src(fleet_repo):
    """Real fleet.prune_closed_bugs `_handler()` against a dirty closed-bug
    entry. Reuses fleet_repo.seed_bug (conftest.py FleetRepo helper)."""
    bug_path = fleet_repo.seed_bug("2026-01-01-dirty-bug.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)
    bug_path.write_text(
        bug_path.read_text(encoding="utf-8") + "# DIRTY uncommitted edit\n", encoding="utf-8",
    )

    result = _run(_prune_bugs_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["acted"] == [{"id": candidate_id, "archived": True}], result
    dest_rel = f"archive/bug-backlog/2026-01/{bug_path.name}"
    status = _porcelain_status(fleet_repo.root, dest_rel)
    assert status.startswith(" M"), (
        f"expected dest to be an UNSTAGED modification; got {status!r}"
    )
    assert "DIRTY uncommitted edit" not in _head_blob(fleet_repo.root, dest_rel)


def test_row2_archive_actioned_memos_internal_end_to_end_dirty_src(fleet_repo):
    """Real fleet.archive_actioned_memos.archive_actioned_memos_internal (the
    session.boot_sweep composite-boot per-family callable, row2) against a
    dirty actioned memo. Reuses test_archive_actioned_memos.py's `_seed_memo`.
    """
    name = "2026-01-01-dirty-memo.md"
    memo_path = _seed_memo(fleet_repo, name, "actioned", title="Dirty Memo")
    memo_path.write_text(
        memo_path.read_text(encoding="utf-8") + "\nDIRTY uncommitted addition\n", encoding="utf-8",
    )

    acted, skipped, failed = _run(archive_actioned_memos_internal(
        worktree_root=fleet_repo.root, common_dir=fleet_repo.common_dir,
    ))

    assert failed == [], failed
    dest_rel = f"cross-repo/archive/{name}"
    status = _porcelain_status(fleet_repo.root, dest_rel)
    assert status.startswith(" M"), (
        f"expected dest to be an UNSTAGED modification; got {status!r}"
    )
    assert "DIRTY uncommitted addition" not in _head_blob(fleet_repo.root, dest_rel)


def test_row3_archive_actioned_memos_handler_end_to_end_dirty_src(fleet_repo):
    """Real fleet.archive_actioned_memos `_handler()` act path (row3) against
    a dirty actioned memo. Reuses test_archive_actioned_memos.py's `_seed_memo`.
    """
    name = "2026-01-02-dirty-memo.md"
    memo_path = _seed_memo(fleet_repo, name, "actioned", title="Dirty Memo Two")
    memo_path.write_text(
        memo_path.read_text(encoding="utf-8") + "\nDIRTY uncommitted addition\n", encoding="utf-8",
    )
    cid = f"cross-repo/inbox/{name}"

    result = _run(_actioned_memos_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["acted"] == [{"id": cid, "archived": True}], result
    dest_rel = f"cross-repo/archive/{name}"
    status = _porcelain_status(fleet_repo.root, dest_rel)
    assert status.startswith(" M"), (
        f"expected dest to be an UNSTAGED modification; got {status!r}"
    )
    assert "DIRTY uncommitted addition" not in _head_blob(fleet_repo.root, dest_rel)


def test_row4_archive_handoffs_handler_end_to_end_dirty_src(fleet_repo):
    """Real fleet.archive_completed_handoffs `_handler()` (row4, non-heir
    branch — restage_src=is_heir=False) against a dirty consumed handoff.
    Reuses fleet_repo.seed_handoff (conftest.py FleetRepo helper) with the
    same "claimed" shape test_archive_handoffs.py's own non-heir act test
    uses.
    """
    name = "2026-01-01-dirty-handoff.md"
    path = fleet_repo.seed_handoff(name, "claimed")
    cid = f"state/handoffs/{name}"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nDIRTY uncommitted addition\n", encoding="utf-8",
    )

    result = _run(_archive_handoffs_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["acted"] == [{"id": cid, "archived": True}], result
    dest_rel = "archive/handoffs/2026-01/" + name
    status = _porcelain_status(fleet_repo.root, dest_rel)
    assert status.startswith(" M"), (
        f"expected dest to be an UNSTAGED modification; got {status!r}"
    )
    assert "DIRTY uncommitted addition" not in _head_blob(fleet_repo.root, dest_rel)


def test_row6_archive_plans_handler_end_to_end_dirty_sidecar(fleet_repo):
    """Real fleet.archive_completed_plans `_archive_completed_plans()` handler
    (row6, sidecar — the LIVE portion of the MIXED row) against a dirty
    sidecar, invoked exactly as the wire op would be (not the hand-built Move
    list test_row6_archive_plans_sidecar_move_is_the_live_portion above uses)
    — stronger fidelity, kept as an ADDITIONAL case rather than a replacement
    since the hand-built version isolates the mechanism against the primary's
    T3-guard interaction more precisely.
    """
    stem = "2026-01-01-dirty-plan"
    fleet_repo.seed_plan(f"{stem}.md", "implemented", title="Dirty Plan")
    sidecar_path = fleet_repo.seed_plan_sidecar(stem, sidecar_suffix=".review.md")
    sidecar_path.write_text(
        sidecar_path.read_text(encoding="utf-8") + "\nDIRTY uncommitted addition\n", encoding="utf-8",
    )
    candidate_id = f"docs/plans/{stem}.md"

    result = _run(_archive_completed_plans(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    assert any(a["id"] == candidate_id for a in result["acted"]), result
    sidecar_dest_rel = f"archive/specs/2026-01/{stem}.review.md"
    status = _porcelain_status(fleet_repo.root, sidecar_dest_rel)
    assert status.startswith(" M"), (
        f"expected sidecar dest to be an UNSTAGED modification; got {status!r}"
    )
    assert "DIRTY uncommitted addition" not in _head_blob(fleet_repo.root, sidecar_dest_rel)


def test_row9_archive_shipped_handoffs_handler_end_to_end_dirty_src(fleet_repo):
    """Real fleet.archive_shipped_handoffs `_handler()` (row9) against a dirty
    shipped handoff. Reuses test_archive_shipped_handoffs.py's own
    `_seed_shipped_handoff`/`_head_sha` helpers.
    """
    sha = _shipped_handoff_head_sha(fleet_repo)
    name = "2026-01-01-dirty-shipped.md"
    cid = _seed_shipped_handoff(fleet_repo, name, sha)
    path = fleet_repo.root / cid
    path.write_text(
        path.read_text(encoding="utf-8") + "\nDIRTY uncommitted addition\n", encoding="utf-8",
    )

    result = _run(_archive_shipped_handoffs_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["acted"] == [{"id": cid, "archived": True}], result
    dest_rel = "archive/handoffs/2026-01/" + name
    status = _porcelain_status(fleet_repo.root, dest_rel)
    assert status.startswith(" M"), (
        f"expected dest to be an UNSTAGED modification; got {status!r}"
    )
    assert "DIRTY uncommitted addition" not in _head_blob(fleet_repo.root, dest_rel)


def test_row1_handoff_archive_transition_chain_mode_handler_end_to_end_dirty_src(tmp_path):
    """Real handoff.archive_transition `_handler()` in chain mode (row1,
    do_stamp=False branch — restage_src=do_stamp=False) against a dirty
    already-terminal handoff.

    Drives coordinator_core.ops.tests.conftest.HandoffRepo directly against
    this test's own `tmp_path` rather than via that module's `handoff_repo`
    pytest fixture — the fixture function is not visible here (this file's
    directory has no conftest.py ancestor relationship to
    coordinator_core/ops/tests/), so the class (git-init + seed_handoff) is
    reused, and the git-init is the only part inlined (mirrors
    coordinator_core/ops/tests/conftest.py's `handoff_repo` fixture body,
    ~10 lines).
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git"] + list(args), cwd=str(repo_root), capture_output=True, check=True)

    _git("init", "-b", "main")
    _git("config", "user.email", "handoff-test@claude-klabauter.test")
    _git("config", "user.name", "Handoff Test")
    _git("config", "commit.gpgsign", "false")
    (repo_root / "state" / "handoffs").mkdir(parents=True)
    (repo_root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    repo = HandoffRepo(repo_root)
    name = "2026-01-01-dirty-chain.md"
    path = repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )
    path.write_text(path.read_text(encoding="utf-8") + "\nDIRTY uncommitted addition\n", encoding="utf-8")

    result = _run(_handoff_archive_transition_handler(
        {"handoff_path": f"state/handoffs/{name}"}, repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["mode"] == "chain"
    assert result["moved"] is True
    assert result["stamped"] is False
    dest_candidates = [
        p for p in (repo_root / "archive" / "handoffs").rglob("*.md") if p.name == name
    ]
    assert len(dest_candidates) == 1, dest_candidates
    dest_rel = dest_candidates[0].relative_to(repo_root).as_posix()
    status = _porcelain_status(repo_root, dest_rel)
    assert status.startswith(" M"), (
        f"expected dest to be an UNSTAGED modification; got {status!r}"
    )
    assert "DIRTY uncommitted addition" not in _head_blob(repo_root, dest_rel)


# ---------------------------------------------------------------------------
# archive_release_accumulator — end-to-end `_handler()` coverage (bonus:
# stronger fidelity than the shared parametrized case above, cheap here
# because the handler is a ~15-line thin wrapper with no candidate-discovery
# guards) + the "no invoker" structural-property assertion the exemption
# depends on.
# ---------------------------------------------------------------------------


def test_archive_release_accumulator_handler_end_to_end_dirty_src(fleet_repo):
    """Calls the REAL fleet.archive_release_accumulator `_handler()` (not a
    hand-built Move) against a dirty accumulator file, proving the actual
    call site (archive_release_accumulator.py:185) — not merely a Move shape
    that mimics it — is closed by the C1 fix.
    """
    accumulator = _write_and_commit(
        fleet_repo, "state/week-changelog/2026-01-01-pending-release.md",
        "## Added\n- thing\n",
    )
    _dirty(accumulator, "## Added\n- thing\n- DIRTY uncommitted addition\n")

    result = _run(_release_accumulator_handler(
        {"tag": "v9.9.9", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is True
    dest_rel = "archive/release-notes/2026-01-01-v9.9.9-pending-release.md"
    assert result["dest"] == dest_rel

    dest_status = _status_line_for(fleet_repo, dest_rel)
    assert dest_status.startswith(" M"), (
        f"expected dest to be an UNSTAGED modification; got {dest_status!r}"
    )
    head_dest = fleet_repo._git("show", f"HEAD:{dest_rel}").stdout.decode()
    assert head_dest == "## Added\n- thing\n"


def _grep_for_invocation(root: Path, needle: str) -> list:
    """Return repo-relative paths under root whose content matches needle,
    via `git grep` scoped to tracked files (skips archive/, .git internals,
    binary noise) — an EXECUTED search, not a prose claim."""
    if not (root / ".git").exists():
        return []
    proc = subprocess.run(
        ["git", "grep", "-l", "-F", needle, "--", ".", ":!archive/"],
        cwd=str(root), capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):
        # rc 1 == no matches (a genuine, informative result); anything else
        # (128 etc.) is a real tooling failure this test must not swallow.
        raise RuntimeError(f"git grep failed in {root}: {proc.stderr!r}")
    return [l for l in proc.stdout.splitlines() if l.strip()]


# Review: coordinator:code-reviewer (Finding 1) — the dispatch shape this repo
# actually uses is `cc_invoke.route("<op-name>", ...)` (verified against
# coordinator/bin/archive-paper-trail.py, which calls
# `cc_invoke.route("fleet.archive_paper_trail", ...)`). A hit inside the
# cross-repo/ or docs/decisions/ prose exemption below is still checked for
# this shape, so a real invocation pasted into prose still fails loudly —
# only bare prose mentions of the op name stay exempt.
_DISPATCH_SHAPE_RE_TEMPLATE = r'route\(\s*["\']{needle}["\']'


def _has_dispatch_shape(root: Path, rel_path: str, needle: str) -> bool:
    """True if `rel_path` under `root` contains an actual dispatch call
    (`route("<needle>"` / `.route('<needle>'` etc.), not just a prose mention
    of the op's name."""
    import re

    pattern = re.compile(_DISPATCH_SHAPE_RE_TEMPLATE.format(needle=re.escape(needle)))
    full_path = root / rel_path
    if not full_path.is_file():
        return False
    try:
        text = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(pattern.search(text))


# Files that legitimately NAME the op (registration/scoping/authz metadata,
# its own module, its own tests, this file, and plan/audit prose) without
# being an INVOKER (a call site that actually dispatches the op at runtime).
_KNOWN_NON_INVOKER_SUFFIXES = (
    "op_scopes.py",
    "DIRECTORY.md",
    "ops/__init__.py",
    "_registry_map.py",
    "ops/fleet/archive_release_accumulator.py",
    "bash_guards/block_subagent_commit.py",
    "authz/registration_quad.py",
    "ops/fleet/tests/test_archive_release_accumulator.py",
    "ops/fleet/tests/test_archive_and_commit_call_site_coverage.py",
    ".github/op-inventory.json",
    "bash_guards/tests/test_subagent_commit_prefilter_and_flags.py",
)


def test_archive_release_accumulator_no_invoker_in_either_repo():
    """The structural property the archive_release_accumulator exemption
    depends on ("registered but has NO invoker anywhere in either repo"),
    itself asserted — not merely stated in prose. Every file naming the op
    string across both repos is enumerated; each MUST be a known non-invoker
    (registry/scope/authz metadata, its own module/tests, or prose) — a
    genuine invocation call site (the `route("fleet.archive_release_accumulator"`
    / `invoke_op("fleet.archive_release_accumulator"` / equivalent dispatch
    shape `fleet.archive_queue_entry` has via prune-closed-improvements.py's
    `route(...)` call) would fail this test.
    """
    needle = "fleet.archive_release_accumulator"
    # (display_label, root, rel_path) — root/rel_path kept alongside the display
    # label so a cross-repo/docs/decisions hit can be re-read from disk and
    # checked for an actual dispatch shape, not just excluded by location.
    hit_records = [(h, _CLAUDE_KLABAUTER_ROOT, h) for h in _grep_for_invocation(_CLAUDE_KLABAUTER_ROOT, needle)]
    doe_root = _EXAMPLE_DOCTRINE_REPO_ROOT
    if doe_root is not None:
        hit_records += [
            (f"{doe_root.name}/{h}", doe_root, h) for h in _grep_for_invocation(doe_root, needle)
        ]
    else:
        pytest.skip(
            "sibling repo not resolvable via the machine-local doe-root pointer — "
            "cannot assert the 'no invoker in either repo' half of this claim "
            "on this checkout; claude-klabauter-side half still ran above this skip."
        )

    unexpected = [
        h for h, root, rel in hit_records
        if not any(h.endswith(suffix) or f"/{suffix}" in h for suffix in _KNOWN_NON_INVOKER_SUFFIXES)
        and "docs/plans/" not in h
        and "state/" not in h
        and "scratchpad/" not in h
        # Prose that DISCUSSES the op is not a call site. Cross-repo memo bodies
        # are the case that exposed this: a memo naming the op landed in the
        # sibling repo's inbox and turned this assertion red, which meant it had
        # been asserting "nothing MENTIONS the op" rather than "nothing INVOKES
        # it" — a weaker property that any document could falsify. Excluded by
        # location (memo channel + decision records), the same way docs/plans
        # is — UNLESS the hit itself carries a real dispatch shape (`route(
        # "fleet.archive_release_accumulator"`), in which case a prose mention
        # cannot explain it and the exclusion does not apply.
        #
        # Review: coordinator:code-reviewer (Finding 1) — narrows the
        # directory-level exclusion so a genuine invocation pasted into
        # cross-repo/docs/decisions prose still fails loudly.
        and not (
            ("cross-repo/" in h or "docs/decisions/" in h)
            and not _has_dispatch_shape(root, rel, needle)
        )
    ]
    assert unexpected == [], (
        f"found a candidate invoker for fleet.archive_release_accumulator: "
        f"{unexpected!r} — if this is a genuine new call site, the "
        f"'no invoker' exemption in this file's module docstring is stale "
        f"and archive_release_accumulator should move into the live-coverage "
        f"parametrized test above instead"
    )

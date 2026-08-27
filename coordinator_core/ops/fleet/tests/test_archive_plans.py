"""
coordinator_core.ops.fleet.tests.test_archive_plans

Tier-T tests for `fleet.archive_completed_plans` (K-051 rebuild). Imports the
module's own functions directly — never resolved by op key, since a
concurrent sibling chunk owns the registry wiring that would race a
by-key lookup.

Coverage:
  - Terminality: a plan whose status is in PLAN_ARCHIVABLE_STATUS is
    archived; one that is not is skipped with a NAMED `not-terminal` reason.
  - Live-claim refusal: a terminal-status plan whose execute-plan claim dir
    is held by a live session is refused (`live-claim-holder`), never
    archived out from under the session working it.
  - Cannot-derive-date: a plan filename with no YYYY-MM-DD prefix is skipped
    with a named reason rather than silently dropped or force-archived to a
    flat directory.
  - Dest-collision vs idempotent replay: a byte-identical dst converges
    (force-move); a byte-different dst is refused with `_REASON_DEST_CONFLICT`.
  - Cap enforcement: over-cap candidates are deferred with a named
    `deferred-cap` reason, never silently truncated.
  - `apply_sweep` spawns ZERO git processes (subprocess-spy, ratchet on the
    exact count, not a bound).
  - Failure path: a sweep that cannot complete records a `failed` outcome to
    the receipt (`_sweep_receipt.record_sweep_outcome`) rather than
    returning silently.

Real on-disk detritus throughout (tmp_path plan corpus with genuinely
terminal/non-terminal files) — never an empty tree.

Negative-spec:
  - Does NOT exercise the single-flight O_EXCL lock rail as a concurrency
    property (mirrors test_archive_terminal_handoffs.py's own scope note) —
    only that `_acquire_sweep_lock`/`_release_sweep_lock` round-trip cleanly.
  - Does NOT stand up a real git repo for the standalone `_handler` op path
    (no test here exercises `check_repo_root`/`main_worktree_root` against a
    real `.git`); `_handle_act` is exercised directly with `archive_and_commit`
    monkeypatched, since this chunk's scope is `plan_sweep`/`apply_sweep` plus
    the observability artifact, not the standalone commit mechanics already
    covered by the shared `_common.archive_and_commit` test suite.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet import archive_plans as m
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT
from coordinator_core.ops.fleet._sweep_receipt import receipt_path

pytestmark = [pytest.mark.cadence]

_CS_CLAIM_HOLDER_LIVE_PATCH = "coordinator_core.ops.fleet.archive_plans.cs_claim_holder_live"


def _write_plan(root: Path, name: str, status: str, extra: str = "") -> Path:
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / name
    path.write_text(
        f"---\ntitle: \"{name}\"\nstatus: {status}\n{extra}---\n\nbody\n",
        encoding="utf-8",
    )
    return path


def _read_last_receipt_row(common_dir: Path) -> dict:
    path = receipt_path(common_dir)
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def test_terminal_plan_archived_non_terminal_skipped(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    _write_plan(worktree, "2026-08-01-done.md", "implemented")
    _write_plan(worktree, "2026-08-02-still-going.md", "executing")

    skipped: list = []
    moves, plan_skipped = m.plan_sweep(worktree, common_dir, cap=10, scan_skipped=skipped)

    ids = {mv.candidate_id for mv in moves}
    assert "docs/plans/2026-08-01-done.md" in ids
    assert not any(mv.candidate_id.endswith("still-going.md") for mv in moves)

    reasons = {row["id"]: row["reason"] for row in skipped}
    assert "not-terminal" in reasons["docs/plans/2026-08-02-still-going.md"]


def test_live_claim_holder_refuses_archival(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    plan_path = _write_plan(worktree, "2026-08-03-claimed.md", "implemented")
    claim_dir = m.plan_claim_dir(common_dir, plan_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    skipped: list = []
    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        moves, plan_skipped = m.plan_sweep(worktree, common_dir, cap=10, scan_skipped=skipped)

    assert not moves
    reasons = {row["id"]: row["reason"] for row in skipped}
    assert reasons["docs/plans/2026-08-03-claimed.md"] == m._SCAN_REASON_LIVE_CLAIM


def test_cannot_derive_date_is_named_not_silently_dropped(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    _write_plan(worktree, "no-date-prefix.md", "implemented")

    skipped: list = []
    moves, _ = m.plan_sweep(worktree, common_dir, cap=10, scan_skipped=skipped)

    assert not moves
    reasons = {row["id"]: row["reason"] for row in skipped}
    assert reasons["docs/plans/no-date-prefix.md"].startswith(m._SCAN_REASON_CANNOT_DERIVE_DATE)


def test_dest_collision_vs_idempotent_replay(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    plan_path = _write_plan(worktree, "2026-08-04-dupe.md", "implemented")
    dest = worktree / "archive" / "specs" / "2026-08" / "2026-08-04-dupe.md"
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Byte-identical dst -> converge (force-move planned, no dest-conflict skip).
    dest.write_bytes(plan_path.read_bytes())
    moves, skipped = m.plan_sweep(worktree, common_dir, cap=10, candidate_ids=["docs/plans/2026-08-04-dupe.md"])
    assert len(moves) == 1
    assert moves[0].force is True
    assert not any(row["reason"] == _REASON_DEST_CONFLICT for row in skipped)

    # Byte-different dst -> refused, never clobbered, never "already-archived".
    dest.write_text("different content", encoding="utf-8")
    moves2, skipped2 = m.plan_sweep(worktree, common_dir, cap=10, candidate_ids=["docs/plans/2026-08-04-dupe.md"])
    assert not moves2
    assert any(row["reason"] == _REASON_DEST_CONFLICT for row in skipped2)


def test_cap_defers_excess_with_named_reason(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    for i in range(3):
        _write_plan(worktree, f"2026-08-0{i+1}-plan{i}.md", "implemented")

    moves, skipped = m.plan_sweep(worktree, common_dir, cap=1)
    assert len(moves) == 1
    deferred_reasons = [row["reason"] for row in skipped if row["reason"].startswith("deferred-cap")]
    assert len(deferred_reasons) == 2


def test_apply_sweep_moves_and_spawns_zero_git_processes(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    _write_plan(worktree, "2026-08-05-move-me.md", "implemented")
    moves, _skipped = m.plan_sweep(worktree, common_dir, cap=10)
    assert len(moves) == 1

    with patch("subprocess.run") as spy, patch("subprocess.Popen") as popen_spy:
        acted, failed = m.apply_sweep(moves)

    assert spy.call_count == 0
    assert popen_spy.call_count == 0
    assert not failed
    assert acted == [{"id": "docs/plans/2026-08-05-move-me.md", "archived": True}]
    assert not moves[0].src.exists()
    assert moves[0].dst.is_file()


def test_apply_sweep_is_idempotent_on_replay(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    _write_plan(worktree, "2026-08-06-replay.md", "implemented")
    moves, _ = m.plan_sweep(worktree, common_dir, cap=10)
    acted1, failed1 = m.apply_sweep(moves)
    assert acted1 and not failed1

    # A second fire over the same (now-gone) source is classified as
    # already-archived by plan_sweep, never re-attempted by apply_sweep.
    moves2, skipped2 = m.plan_sweep(
        worktree, common_dir, cap=10, candidate_ids=["docs/plans/2026-08-06-replay.md"]
    )
    assert not moves2
    assert any(row["reason"] == "already-archived" for row in skipped2)


def test_failed_sweep_records_receipt_outcome(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    _write_plan(worktree, "2026-08-07-boom.md", "implemented")

    async def _raise(*_a, **_kw):
        raise RuntimeError("simulated commit failure")

    with patch("coordinator_core.ops.fleet.archive_plans.archive_and_commit", side_effect=_raise):
        result = m._handle_act(
            "already-terminal", worktree, common_dir,
            ["docs/plans/2026-08-07-boom.md"], cap=10,
        )

    assert result["failed"]
    row = _read_last_receipt_row(common_dir)
    assert row["sweep"] == "fleet.archive_completed_plans"
    assert row["outcome"] == "failed"


def test_sidecar_with_live_primary_is_never_archived_alone(tmp_path: Path) -> None:
    # The verified corpus defect: a primary plan still `status: draft` (live)
    # whose review sidecar independently reads a terminal status must NOT be
    # archived out from under its still-live primary.
    worktree = tmp_path
    common_dir = tmp_path

    _write_plan(worktree, "2026-08-08-live-primary.md", "draft")
    _write_plan(worktree, "2026-08-08-live-primary.prior-art-check.md", "implemented")

    skipped: list = []
    moves, _ = m.plan_sweep(worktree, common_dir, cap=10, scan_skipped=skipped)

    ids = {mv.candidate_id for mv in moves}
    assert not any("prior-art-check" in cid for cid in ids)
    assert not any(cid.endswith("live-primary.md") for cid in ids)

    reasons = {row["id"]: row["reason"] for row in skipped}
    sidecar_reason = reasons["docs/plans/2026-08-08-live-primary.prior-art-check.md"]
    assert sidecar_reason.startswith(m._SCAN_REASON_SIDECAR_FOLLOWS_PRIMARY)


def test_sidecar_with_terminal_primary_is_archived(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    _write_plan(worktree, "2026-08-09-done-primary.md", "implemented")
    _write_plan(worktree, "2026-08-09-done-primary.review.md", "implemented")

    moves, skipped = m.plan_sweep(worktree, common_dir, cap=10)
    ids = {mv.candidate_id for mv in moves}
    assert "docs/plans/2026-08-09-done-primary.md" in ids
    assert "docs/plans/2026-08-09-done-primary.review.md" in ids


def test_orphan_sidecar_with_no_primary_is_refused(tmp_path: Path) -> None:
    worktree = tmp_path
    common_dir = tmp_path

    _write_plan(worktree, "2026-08-10-ghost.review.md", "implemented")

    skipped: list = []
    moves, _ = m.plan_sweep(worktree, common_dir, cap=10, scan_skipped=skipped)
    assert not moves
    reasons = {row["id"]: row["reason"] for row in skipped}
    assert reasons["docs/plans/2026-08-10-ghost.review.md"].startswith(m._SCAN_REASON_SIDECAR_ORPHAN)


def test_setup_errors_record_receipt_rows(tmp_path: Path) -> None:
    common_dir = tmp_path

    # Bad cap, with a resolvable repo_root -> receipt row expected.
    result = m._handler({"dry_run": True, "cap": 0}, repo_root=common_dir)
    assert result.get("exit_code") == 1 or "cap" in json.dumps(result)
    row = _read_last_receipt_row(common_dir)
    assert row["outcome"] == "failed"

    # repo_root None -> setup error, no common_dir to write to, must not raise.
    m._handler({"dry_run": True, "cap": 1}, repo_root=None)


def test_sweep_lock_round_trips(tmp_path: Path) -> None:
    common_dir = tmp_path
    lock1 = m._acquire_sweep_lock(common_dir)
    assert lock1 is not None
    assert lock1.is_file()

    # Contended: a second acquire while the first is held returns None.
    lock2 = m._acquire_sweep_lock(common_dir)
    assert lock2 is None

    m._release_sweep_lock(lock1)
    assert not lock1.exists()

    lock3 = m._acquire_sweep_lock(common_dir)
    assert lock3 is not None
    m._release_sweep_lock(lock3)

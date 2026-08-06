"""Regression tests — goals.py dedup key must be goal-identity-keyed, not period-keyed.

Pins the fix for the 2026-07-21 break-class bug: the dedup key used to be (repo,
coordinator_root_path, period, period_value) with no identity component, so every goal
sharing a period silently collapsed to the single latest-declared_at row — verified to
drop 58% of declared goals (55 raw rows -> 10 keys) against the real state tree. The fix
keys on ``goal_id`` (falling back to (repo, coordinator_root_path, period, period_value,
text) for legacy rows with no goal_id) so parallel goals within one period all survive,
while re-declaring the SAME goal (same goal_id, or same legacy-identity tuple) still
collapses to the latest ``declared_at`` record (supersession).

Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § P06
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import goals as goals_section


def _make_ctx(tmp_path: Path, repo_name: str = "test-org/test-repo") -> EmitContext:
    central = tmp_path / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-21T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _write_goal_log(ctx: EmitContext, machine: str, records: list[dict]) -> None:
    log_path = ctx.central_state_root / f"goals-log.{machine}.jsonl"
    log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _base_goal_record(**overrides) -> dict:
    record = {
        "goal_id": "goal-dedup-base",
        "repo": "test-org/test-repo",
        "coordinator_root_path": ".",
        "period": "day",
        "period_value": "2026-06-24",
        "declared_by_machine": "test-host",
        "declared_at": "2026-06-24T09:00:00Z",
        "text": "Base dedup fixture goal.",
        "status": "active",
    }
    record.update(overrides)
    return record


def test_distinct_goal_ids_sharing_period_all_survive(tmp_path: Path) -> None:
    """N distinct goals sharing (repo, root, period, period_value) but differing in
    text/goal_id must ALL be emitted — this is the exact shape of the bug: a ceremony
    declaring 15 daily goals in one batch previously collapsed to 1."""
    ctx = _make_ctx(tmp_path)
    records_in = [
        _base_goal_record(goal_id=f"goal-{i:02d}", text=f"Distinct goal #{i}", declared_at=f"2026-06-24T09:0{i}:00Z")
        for i in range(5)
    ]
    _write_goal_log(ctx, "test-host", records_in)

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 5, f"expected all 5 distinct goals to survive, got {len(records)}"
    emitted_ids = {r["goal_id"] for r in records}
    assert emitted_ids == {f"goal-{i:02d}" for i in range(5)}


def test_same_goal_id_different_declared_at_collapses_to_latest(tmp_path: Path) -> None:
    """Two rows sharing the SAME goal_id but different declared_at must still collapse
    to the later one — supersession semantics are preserved by the fix."""
    ctx = _make_ctx(tmp_path)
    records_in = [
        _base_goal_record(goal_id="goal-super", text="Superseded text", declared_at="2026-06-24T09:00:00Z"),
        _base_goal_record(goal_id="goal-super", text="Superseded text", declared_at="2026-06-24T10:30:00Z"),
    ]
    _write_goal_log(ctx, "test-host", records_in)

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 1, f"expected supersession to collapse to 1 record, got {len(records)}"
    assert records[0]["declared_at"] == "2026-06-24T10:30:00Z"


def test_legacy_row_without_goal_id_is_not_dropped(tmp_path: Path) -> None:
    """A legacy row with no goal_id must still be emitted (fallback identity key), not
    silently dropped for want of the field."""
    ctx = _make_ctx(tmp_path)
    legacy_record = _base_goal_record(text="Legacy goal, no id")
    del legacy_record["goal_id"]
    _write_goal_log(ctx, "test-host", [legacy_record])

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 1, f"expected the legacy row to survive, got {len(records)}"
    # Review: code-reviewer (Finding 1) — collect() now emits the reader's resolved
    # deterministic-hash fallback id (row.goal_id) instead of "" for legacy rows, so the
    # emit consumer and the close-out consumer agree on identity for the same wire row.
    assert records[0]["goal_id"] != ""
    assert records[0]["text"] == "Legacy goal, no id"


def test_legacy_rows_with_distinct_identity_all_survive(tmp_path: Path) -> None:
    """Multiple legacy (no-goal_id) rows sharing (repo, root, period, period_value) but
    differing in text must all survive via the fallback key — not collapse to one."""
    ctx = _make_ctx(tmp_path)
    rec_a = _base_goal_record(text="Legacy goal A")
    del rec_a["goal_id"]
    rec_b = _base_goal_record(text="Legacy goal B")
    del rec_b["goal_id"]
    _write_goal_log(ctx, "test-host", [rec_a, rec_b])

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 2, f"expected both distinct legacy goals to survive, got {len(records)}"
    texts = {r["text"] for r in records}
    assert texts == {"Legacy goal A", "Legacy goal B"}


def test_legacy_rows_same_identity_different_declared_at_collapse(tmp_path: Path) -> None:
    """Two legacy (no-goal_id) rows with the SAME identity tuple but different
    declared_at must still collapse to the later one via the fallback key."""
    ctx = _make_ctx(tmp_path)
    rec_early = _base_goal_record(text="Re-declared legacy goal", declared_at="2026-06-24T09:00:00Z")
    del rec_early["goal_id"]
    rec_late = _base_goal_record(text="Re-declared legacy goal", declared_at="2026-06-24T11:00:00Z")
    del rec_late["goal_id"]
    _write_goal_log(ctx, "test-host", [rec_early, rec_late])

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 1, f"expected legacy supersession to collapse to 1 record, got {len(records)}"
    assert records[0]["declared_at"] == "2026-06-24T11:00:00Z"


# ---------------------------------------------------------------------------
# KNOWN CROSS-CUTOVER GAP (2026-07-22, goal.append explicit-goal_id feature).
#
# goal_append.py's append_goal() now accepts an EXPLICIT goal_id (validated to
# the same 12-hex shape _goal_id() emits — see goal_append.py's _GOAL_ID_RE).
# This section's dedup key groups on the row's OWN goal_id when present, and
# ONLY falls back to recomputing _goal_id() when goal_id is absent-in-row
# (see collect()'s `if not goal_id:` branch above). Consequence, NAMED here
# rather than silently absorbed: a LEGACY row for a logical goal (written
# before this feature existed, no goal_id in the row -> falls back to the
# content-hash) and a LATER row for the SAME logical goal that arrives with a
# DIFFERENT explicit goal_id (any valid 12-hex string not equal to that
# content-hash) do NOT unify — they are two distinct dedup keys, so both
# survive as separate records instead of the later one superseding the
# earlier one. This is exactly the identity-fragmentation hazard the whole
# goal.append memo exists to prevent, now showing up one layer over: at the
# READ (dedup) side instead of the write side, for any goal with emitted
# history that a caller later re-declares under an explicit id. Fixing the
# dedup key is a direction-class call (which identity should win, and how to
# reconcile two colliding shards) outside this task's scope — this test only
# makes the current, un-fixed behaviour visible and citable.
# ---------------------------------------------------------------------------

def test_legacy_content_hash_row_does_not_unify_with_later_explicit_goal_id_row(
    tmp_path: Path,
) -> None:
    """KNOWN GAP: a legacy (no-goal_id) row and a later row for the same logical
    goal but carrying a DIFFERENT explicit goal_id do NOT collapse via
    supersession — both survive as two records. See module-level comment block
    immediately above for the full explanation; this is current reality, not
    the desired end-state."""
    ctx = _make_ctx(tmp_path)
    legacy = _base_goal_record(
        text="Same logical goal, legacy shape",
        declared_at="2026-06-24T09:00:00Z",
    )
    del legacy["goal_id"]  # legacy row: writer predates goal_id stamping

    explicit = _base_goal_record(
        goal_id="abc123def456",  # explicit id, unrelated to the content-hash below
        text="Same logical goal, legacy shape",
        declared_at="2026-06-24T11:00:00Z",
    )
    _write_goal_log(ctx, "test-host", [legacy, explicit])

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    # Desired end-state would be 1 (supersession); current reality is 2 — this
    # assertion pins CURRENT (gap) behaviour, not the goal.
    assert len(records) == 2, (
        "if this now returns 1, the dedup-key gap named above has been fixed — "
        "update this test's assertion and comment to match, don't just relax it"
    )


def test_non_dict_json_line_is_quarantined_not_raised(tmp_path: Path) -> None:
    """Review: code-reviewer (Finding 5) — a syntactically valid JSON line that isn't an
    object (bare int, bare list) must be skipped, not crash collect() via AttributeError
    on the non-dict value (Finding 2's regression test)."""
    ctx = _make_ctx(tmp_path)
    good_record = _base_goal_record(goal_id="goal-good", text="Well-formed goal")
    log_path = ctx.central_state_root / "goals-log.test-host.jsonl"
    lines = [
        json.dumps(good_record),
        "5",
        "[1,2,3]",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 1, f"expected only the well-formed record to survive, got {len(records)}"
    assert records[0]["goal_id"] == "goal-good"


def test_cross_machine_dedup_winner_names_its_own_shard_in_provenance(tmp_path: Path) -> None:
    """Review: code-reviewer (Finding 4) — two DIFFERENT machine shards each declaring the
    same goal_id at different declared_at must collapse to the later row, and the winning
    record's provenance.path must name that machine's own shard file, not the other
    machine's shard and not the glob pattern used to find them."""
    ctx = _make_ctx(tmp_path)
    early = _base_goal_record(
        goal_id="goal-cross-machine",
        text="Cross-machine goal",
        declared_by_machine="machine-a",
        declared_at="2026-06-24T09:00:00Z",
    )
    late = _base_goal_record(
        goal_id="goal-cross-machine",
        text="Cross-machine goal",
        declared_by_machine="machine-b",
        declared_at="2026-06-24T11:00:00Z",
    )
    _write_goal_log(ctx, "machine-a", [early])
    _write_goal_log(ctx, "machine-b", [late])

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 1, f"expected cross-machine collapse to 1 record, got {len(records)}"
    winner = records[0]
    assert winner["declared_at"] == "2026-06-24T11:00:00Z"
    assert winner["declared_by_machine"] == "machine-b"
    assert winner["provenance"]["path"] == "state/goals-log.machine-b.jsonl"


# ---------------------------------------------------------------------------
# Unreadable central_state_root — silent-success guard.
#
# ``Path.glob()`` silently swallows ``PermissionError`` while walking (an unreadable
# dir yields an empty iterator, no exception), which would otherwise make a
# permission-denied central_state_root indistinguishable from "no goal logs here" and
# collapse into the same zero-goals ``([], [])`` shape as a genuinely goal-less repo.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_central_state_root_raises_not_zero_goals(tmp_path: Path) -> None:
    """An unscannable central_state_root must raise GoalsStateRootUnreadable, never
    silently degrade to the zero-goals ``([], [])`` shape — this section has no
    envelope-visible malformed channel, so silently returning ``[]`` records would be
    indistinguishable from "this repo genuinely has zero declared goals"."""
    ctx = _make_ctx(tmp_path)
    _write_goal_log(ctx, "test-host", [_base_goal_record()])

    original_mode = ctx.central_state_root.stat().st_mode
    os.chmod(ctx.central_state_root, 0o000)
    try:
        with pytest.raises(goals_section.GoalsStateRootUnreadable):
            goals_section.collect(ctx)
    finally:
        os.chmod(ctx.central_state_root, original_mode)

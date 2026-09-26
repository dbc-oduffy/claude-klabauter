
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
    ctx = _make_ctx(tmp_path)
    legacy_record = _base_goal_record(text="Legacy goal, no id")
    del legacy_record["goal_id"]
    _write_goal_log(ctx, "test-host", [legacy_record])

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 1, f"expected the legacy row to survive, got {len(records)}"
    assert records[0]["goal_id"] != ""
    assert records[0]["text"] == "Legacy goal, no id"


def test_legacy_rows_with_distinct_identity_all_survive(tmp_path: Path) -> None:
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


# KNOWN CROSS-CUTOVER GAP (2026-07-22, goal.append explicit-goal_id feature).
# goal_append.py's append_goal() now accepts an EXPLICIT goal_id (validated to
# the same 12-hex shape _goal_id() emits — see goal_append.py's _GOAL_ID_RE).
# DIFFERENT explicit goal_id (any valid 12-hex string not equal to that

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
    del legacy["goal_id"]

    explicit = _base_goal_record(
        goal_id="abc123def456",
        text="Same logical goal, legacy shape",
        declared_at="2026-06-24T11:00:00Z",
    )
    _write_goal_log(ctx, "test-host", [legacy, explicit])

    records, malformed = goals_section.collect(ctx)
    assert malformed == []
    assert len(records) == 2, (
        "if this now returns 1, the dedup-key gap named above has been fixed — "
        "update this test's assertion and comment to match, don't just relax it"
    )


def test_non_dict_json_line_is_quarantined_not_raised(tmp_path: Path) -> None:
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
    """Two DIFFERENT machine shards each declaring the
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


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_central_state_root_raises_not_zero_goals(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    _write_goal_log(ctx, "test-host", [_base_goal_record()])

    original_mode = ctx.central_state_root.stat().st_mode
    os.chmod(ctx.central_state_root, 0o000)
    try:
        with pytest.raises(goals_section.GoalsStateRootUnreadable):
            goals_section.collect(ctx)
    finally:
        os.chmod(ctx.central_state_root, original_mode)

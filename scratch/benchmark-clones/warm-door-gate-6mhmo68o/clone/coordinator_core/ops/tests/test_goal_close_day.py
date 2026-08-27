"""
Tests for coordinator_core.ops.goal_close_day — open period="day" enumeration
scoped to (repo, coordinator_root_path), partitioned today/stale (§ C2), and the
close-out write leg that re-appends a row at its same goal_id with a terminal
status (§ C3).

Spec backlink: pln-day-scoped-goal-close-out-life-69a25c § C2/C3
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.goals.wire_read import read_and_collapse
from coordinator_core.ops.goal_close_day import (
    GoalCloseDayLostSupersession,
    GoalCloseDayRootUnreadable,
    _resolve_today,
    close_day_goals,
    collect_open_day_goals,
)

TODAY = "2026-07-25"
YESTERDAY = "2026-07-24"
TOMORROW = "2026-07-26"

REPO = "dbc-oduffy/claude-klabauter"
OTHER_REPO = "dbc-oduffy/.example-doctrine-mirror-repo"


def _row(**overrides) -> dict:
    row = {
        "goal_id": "abc123def456",
        "repo": REPO,
        "coordinator_root_path": ".",
        "period": "day",
        "period_value": TODAY,
        "declared_by_machine": "test-machine",
        "declared_at": "2026-07-25T09:00:00Z",
        "text": "ship the thing",
        "status": "active",
    }
    row.update(overrides)
    return row


def _write_shard(tmp_path: Path, rows: list[dict], name: str = "goals-log.test-machine.jsonl") -> Path:
    shard = tmp_path / name
    shard.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    return shard


def test_returns_only_open_in_scope_day_rows_partitioned_today_vs_stale(tmp_path):
    rows = [
        _row(goal_id="today-open", period_value=TODAY, text="today open"),
        _row(goal_id="stale-open", period_value=YESTERDAY, text="stale open"),
        _row(goal_id="future-goal", period_value=TOMORROW, text="future goal"),
        _row(goal_id="done-goal", period_value=YESTERDAY, status="done", text="done goal"),
        _row(goal_id="dropped-goal", period_value=TODAY, status="dropped", text="dropped goal"),
        _row(goal_id="other-repo-goal", period_value=YESTERDAY, repo=OTHER_REPO, text="other repo"),
        _row(goal_id="week-goal", period="week", period_value="2026-W30", text="a week goal"),
        _row(
            goal_id="other-root-goal",
            period_value=TODAY,
            coordinator_root_path="subdir",
            text="other root",
        ),
    ]
    _write_shard(tmp_path, rows)

    result = collect_open_day_goals(tmp_path, REPO, coordinator_root_path=".", today=TODAY)

    today_ids = {r["goal_id"] for r in result["today"]}
    stale_ids = {r["goal_id"] for r in result["stale"]}

    assert today_ids == {"today-open"}
    assert stale_ids == {"stale-open"}
    assert result["unreadable_error"] is None


def test_future_period_value_is_excluded(tmp_path):
    _write_shard(tmp_path, [_row(goal_id="future-goal", period_value=TOMORROW)])

    result = collect_open_day_goals(tmp_path, REPO, today=TODAY)

    assert result["today"] == []
    assert result["stale"] == []


def test_done_and_dropped_rows_excluded(tmp_path):
    _write_shard(
        tmp_path,
        [
            _row(goal_id="done-today", status="done"),
            _row(goal_id="dropped-stale", period_value=YESTERDAY, status="dropped"),
        ],
    )

    result = collect_open_day_goals(tmp_path, REPO, today=TODAY)

    assert result["today"] == []
    assert result["stale"] == []


def test_different_repo_in_same_shard_is_not_returned(tmp_path):
    _write_shard(
        tmp_path,
        [
            _row(goal_id="mine", repo=REPO),
            _row(goal_id="theirs", repo=OTHER_REPO),
        ],
    )

    result = collect_open_day_goals(tmp_path, REPO, today=TODAY)
    result_other = collect_open_day_goals(tmp_path, OTHER_REPO, today=TODAY)

    assert {r["goal_id"] for r in result["today"]} == {"mine"}
    assert {r["goal_id"] for r in result_other["today"]} == {"theirs"}


def test_row_carries_identity_and_provenance_fields(tmp_path):
    _write_shard(
        tmp_path,
        [
            _row(
                goal_id="abc123def456",
                parent_goal_id="parent123456",
                key_results_status=[{"id": "kr1", "status": "on_track"}],
                weekly_perceptible=True,
            )
        ],
    )

    result = collect_open_day_goals(tmp_path, REPO, today=TODAY)
    row = result["today"][0]

    assert row["goal_id"] == "abc123def456"
    assert row["text"] == "ship the thing"
    assert row["repo"] == REPO
    assert row["coordinator_root_path"] == "."
    assert row["period"] == "day"
    assert row["period_value"] == TODAY
    assert row["parent_goal_id"] == "parent123456"
    assert row["key_results_status"] == [{"id": "kr1", "status": "on_track"}]
    assert row["weekly_perceptible"] is True


def test_writes_nothing(tmp_path):
    shard = _write_shard(tmp_path, [_row(goal_id="one"), _row(goal_id="two", status="done")])
    before = shard.read_bytes()

    collect_open_day_goals(tmp_path, REPO, today=TODAY)

    after = shard.read_bytes()
    assert after == before


def test_unparseable_today_override_falls_back_to_real_utc_today():
    # Review: code-reviewer — no prior test exercised the try/except ValueError
    # fallback branch; every other test passes a valid ISO `today=`.
    from datetime import datetime, timezone

    result = _resolve_today("not-a-date")

    assert result == datetime.now(timezone.utc).date()


def test_unreadable_central_state_root_degrades_to_empty(tmp_path, monkeypatch):
    unreadable_root = tmp_path / "unreadable"
    unreadable_root.mkdir()
    (unreadable_root / "goals-log.test-machine.jsonl").write_text(
        json.dumps(_row()) + "\n"
    )

    import os

    real_scandir = os.scandir

    def _boom(path):
        if Path(path) == unreadable_root:
            raise PermissionError("simulated permission denial")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _boom)

    result = collect_open_day_goals(unreadable_root, REPO, today=TODAY)

    assert result["today"] == []
    assert result["stale"] == []
    assert result["unreadable_error"] is not None


# ---------------------------------------------------------------------------
# close_day_goals (§ C3 — the mutating close-out write leg)
# ---------------------------------------------------------------------------


def _collapse_record(tmp_path: Path, goal_id: str, repo: str = REPO) -> dict | None:
    result = read_and_collapse(tmp_path)
    for log_row in result.rows:
        if log_row.goal_id == goal_id and log_row.record.get("repo") == repo:
            return log_row.record
    return None


def test_round_trip_declare_close_reread_reports_terminal_status(tmp_path):
    _write_shard(tmp_path, [_row(goal_id="abc123def456", status="active")])

    result = close_day_goals(tmp_path, REPO, {"abc123def456": "done"}, hostname="test-machine")

    assert result["closed"] == [
        {
            "goal_id": "abc123def456",
            "status": "done",
            "log_file": str(tmp_path / "goals-log.test-machine.jsonl"),
        }
    ]
    record = _collapse_record(tmp_path, "abc123def456")
    assert record is not None
    assert record["status"] == "done"


def test_empty_decisions_writes_nothing(tmp_path):
    shard = _write_shard(tmp_path, [_row(goal_id="one")])
    before = shard.read_bytes()

    result = close_day_goals(tmp_path, REPO, {}, hostname="test-machine")

    assert result == {"closed": []}
    assert shard.read_bytes() == before


def test_absent_decisions_default_writes_nothing(tmp_path):
    shard = _write_shard(tmp_path, [_row(goal_id="one")])
    before = shard.read_bytes()

    result = close_day_goals(tmp_path, REPO, None, hostname="test-machine")

    assert result == {"closed": []}
    assert shard.read_bytes() == before


def test_prior_row_byte_intact_after_close(tmp_path):
    shard = _write_shard(tmp_path, [_row(goal_id="abc123def456", status="active")])
    before = shard.read_bytes()

    close_day_goals(tmp_path, REPO, {"abc123def456": "done"}, hostname="test-machine")

    after = shard.read_bytes()
    assert after.startswith(before)
    assert after != before


def test_identity_carry_through_on_close(tmp_path):
    _write_shard(
        tmp_path,
        [
            _row(
                goal_id="abc123def456",
                text="ship the widget",
                coordinator_root_path="subdir",
                period_value=YESTERDAY,
                parent_goal_id="parent123456",
                key_results_status=[{"id": "kr1", "status": "on_track"}],
                weekly_perceptible=True,
            )
        ],
    )

    close_day_goals(
        tmp_path,
        REPO,
        {"abc123def456": "done"},
        coordinator_root_path="subdir",
        hostname="test-machine",
    )

    record = _collapse_record(tmp_path, "abc123def456")
    assert record is not None
    assert record["repo"] == REPO
    assert record["coordinator_root_path"] == "subdir"
    assert record["period"] == "day"
    assert record["period_value"] == YESTERDAY
    assert record["text"] == "ship the widget"
    assert record["parent_goal_id"] == "parent123456"
    assert record["key_results_status"] == [{"id": "kr1", "status": "on_track"}]
    assert record["weekly_perceptible"] is True
    assert record["status"] == "done"


def test_collapse_yields_exactly_one_record_after_close(tmp_path):
    _write_shard(tmp_path, [_row(goal_id="abc123def456", status="active")])

    close_day_goals(tmp_path, REPO, {"abc123def456": "done"}, hostname="test-machine")

    result = read_and_collapse(tmp_path)
    matches = [
        r for r in result.rows if r.goal_id == "abc123def456" and r.record.get("repo") == REPO
    ]
    assert len(matches) == 1


def test_decision_not_named_done_closes_dropped(tmp_path):
    _write_shard(tmp_path, [_row(goal_id="abc123def456", status="active")])

    result = close_day_goals(
        tmp_path, REPO, {"abc123def456": "abandoned"}, hostname="test-machine"
    )

    assert result["closed"][0]["status"] == "dropped"
    record = _collapse_record(tmp_path, "abc123def456")
    assert record["status"] == "dropped"


def test_unknown_goal_id_raises_value_error(tmp_path):
    _write_shard(tmp_path, [_row(goal_id="abc123def456", status="active")])

    with pytest.raises(ValueError):
        close_day_goals(tmp_path, REPO, {"nonexistent": "done"}, hostname="test-machine")


def test_different_repo_row_is_out_of_scope_for_close(tmp_path):
    _write_shard(
        tmp_path,
        [_row(goal_id="abc123def456", repo=OTHER_REPO, status="active")],
    )

    with pytest.raises(ValueError):
        close_day_goals(tmp_path, REPO, {"abc123def456": "done"}, hostname="test-machine")


def test_reclosing_already_closed_row_raises_value_error(tmp_path):
    # Review: code-reviewer — a decision naming an already-done/dropped goal_id
    # must not resolve to a source row; accepting it would silently overwrite
    # the terminal status already on the wire via the latest-wins collapse.
    _write_shard(tmp_path, [_row(goal_id="abc123def456", status="done")])

    with pytest.raises(ValueError):
        close_day_goals(tmp_path, REPO, {"abc123def456": "dropped"}, hostname="test-machine")

    record = _collapse_record(tmp_path, "abc123def456")
    assert record["status"] == "done"


def test_unreadable_central_state_root_fails_loud_on_write_leg(tmp_path, monkeypatch):
    # Review: code-reviewer — the write leg's "fails loud on unreadable root"
    # claim previously fired only incidentally via the generic "missing" check;
    # this asserts the dedicated, correctly-diagnosed exception.
    unreadable_root = tmp_path / "unreadable"
    unreadable_root.mkdir()
    (unreadable_root / "goals-log.test-machine.jsonl").write_text(
        json.dumps(_row(goal_id="abc123def456")) + "\n"
    )

    import os

    real_scandir = os.scandir

    def _boom(path):
        if Path(path) == unreadable_root:
            raise PermissionError("simulated permission denial")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _boom)

    with pytest.raises(GoalCloseDayRootUnreadable):
        close_day_goals(
            unreadable_root, REPO, {"abc123def456": "done"}, hostname="test-machine"
        )


def test_lost_supersession_fails_loud(tmp_path):
    # A source row whose declared_at is set far in the future beats any
    # real-clock append written by close_day_goals — reproducing DEC-3 hazard 2
    # (a close written on a machine whose clock trails the declaring machine).
    _write_shard(
        tmp_path,
        [_row(goal_id="abc123def456", status="active", declared_at="9999-12-31T23:59:59Z")],
    )

    with pytest.raises(GoalCloseDayLostSupersession):
        close_day_goals(tmp_path, REPO, {"abc123def456": "done"}, hostname="test-machine")

    # The append still landed on disk (fail loud, not silently dropped) — but the
    # collapse still reports the row open, matching the hazard being guarded.
    record = _collapse_record(tmp_path, "abc123def456")
    assert record["status"] == "active"

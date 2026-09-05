"""Unit coverage for ``rollups.collect``'s ``fact_window`` — the field naming the window a
rollup row's facts were computed over, so a consumer no longer has to infer it from
``max_observed_at`` and deployment topology.

Spec backlink: docs/plans/2026-09-04-rollup-rows-name-their-own-fact-window.md
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from coordinator_core.contract.cockpit_schema.entities.rollup import DayRollup, WeekRollup
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.rollups import collect

_COMPLETED_DIR = "archive/completed/2026-03"


def _make_ctx(observed_at: str, tmp_path: Path) -> EmitContext:
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=tmp_path,
        git_branch="test-branch",
        git_sha="deadbeef" * 5,
        git_sha_short="deadbeef",
        observed_at=observed_at,
        hostname="test-host",
        repo_name="test/repo",
    )


def _write_completion(
    tmp_path: Path,
    name: str,
    created,
    *,
    chain: str = "chain-a",
    commit: str = "aaaaaaa",
    dirname: str = _COMPLETED_DIR,
) -> None:
    path = tmp_path / dirname / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"title: \"{name}\"\n"
        f"created: {created}\n"
        f"chain: \"{chain}\"\n"
        "loe:\n"
        "  tshirt: S\n"
        "commits:\n"
        f"  - \"{commit}\"\n"
        "status: done\n"
        "---\n\n"
        "Test completion fixture.\n"
    )
    path.write_text(body, encoding="utf-8")


def _rollups(ctx: EmitContext):
    records, malformed = collect(ctx)
    assert malformed == []
    week = [r for r in records if r["grain"] == "week"][0]
    day = [r for r in records if r["grain"] == "day"][0]
    return week, day


def test_week_row_fact_window_matches_iso_week_of_observed_at_not_wall_clock(tmp_path):
    """``observed_at`` is deliberately NOT today, so a wall-clock regression fails this."""
    observed_at = "2026-03-18T12:00:00Z"  # Wednesday of 2026-W12
    _write_completion(tmp_path, "inside.md", "2026-03-16")
    ctx = _make_ctx(observed_at, tmp_path)

    week, _day = _rollups(ctx)

    assert week["period"] == "2026-W12"
    assert week["fact_window"] == {
        "kind": "iso-week",
        "start": "2026-03-16",  # Monday of 2026-W12
        "end": "2026-03-22",  # Sunday of 2026-W12
    }
    # The bound is NOT derived from the real-world today of whenever this test happens to run.
    assert week["fact_window"]["start"] != datetime.date.today().isoformat()


def test_week_row_fact_window_agrees_with_the_iso_year_boundary_selection(tmp_path):
    """2027-01-01 is ISO 2026-W53; the emitted bounds must agree with the tuple-compared filter
    that actually selected the records (``collect``'s WEEK narrowing), not a calendar-year
    recomputation."""
    observed_at = "2027-01-01T12:00:00Z"  # ISO 2026-W53, calendar year 2027
    _write_completion(tmp_path, "boundary.md", "2026-12-31", chain="chain-a")
    ctx = _make_ctx(observed_at, tmp_path)

    week, _day = _rollups(ctx)

    assert week["period"] == "2026-W53"
    assert week["fact_window"] == {
        "kind": "iso-week",
        "start": "2026-12-28",  # Monday of ISO 2026-W53
        "end": "2027-01-03",  # Sunday of ISO 2026-W53
    }


def test_day_row_carries_its_own_fact_window(tmp_path):
    observed_at = "2026-03-18T12:00:00Z"
    today = "2026-03-18"
    _write_completion(tmp_path, "today.md", today)
    ctx = _make_ctx(observed_at, tmp_path)

    _week, day = _rollups(ctx)

    assert day["period"] == today
    assert day["fact_window"] == {"kind": "day", "start": today, "end": today}


def test_day_and_week_rows_disagree_in_kind_but_both_are_present(tmp_path):
    """Both grains get a window — a day row without one while the week row has one would
    invite a consumer to infer absent means 30-day."""
    observed_at = "2026-03-18T12:00:00Z"
    _write_completion(tmp_path, "today.md", "2026-03-18")
    ctx = _make_ctx(observed_at, tmp_path)

    week, day = _rollups(ctx)

    assert week["fact_window"] is not None
    assert day["fact_window"] is not None
    assert week["fact_window"]["kind"] != day["fact_window"]["kind"]


def test_entity_accepts_fact_window_absent_and_does_not_default_it(tmp_path):
    """The rollout case: a row emitted before this field existed. Absence must round-trip as
    ``None``, never be filled in with any default."""
    ctx = _make_ctx("2026-03-18T12:00:00Z", tmp_path)
    row = _minimal_day_row(ctx, fact_window_present=False)
    model = DayRollup.model_validate(row)
    assert model.fact_window is None


def test_entity_accepts_fact_window_present_on_day_and_week(tmp_path):
    ctx = _make_ctx("2026-03-18T12:00:00Z", tmp_path)
    day_row = _minimal_day_row(ctx, fact_window_present=True)
    day_model = DayRollup.model_validate(day_row)
    assert day_model.fact_window is not None
    assert day_model.fact_window.kind == "day"

    week_row = _minimal_week_row(ctx, fact_window_present=True)
    week_model = WeekRollup.model_validate(week_row)
    assert week_model.fact_window is not None
    assert week_model.fact_window.kind == "iso-week"


def test_fact_window_rejects_extra_keys(tmp_path):
    """Every rollup model is ``extra=forbid``; ``FactWindow`` must be too."""
    ctx = _make_ctx("2026-03-18T12:00:00Z", tmp_path)
    row = _minimal_day_row(ctx, fact_window_present=True)
    row["fact_window"]["surprise"] = "nope"
    with pytest.raises(ValidationError):
        DayRollup.model_validate(row)


def _minimal_day_row(ctx: EmitContext, *, fact_window_present: bool) -> dict:
    row = {
        "grain": "day",
        "period": "2026-03-18",
        "repo": "test/repo",
        "coordinator_root_path": ".",
        "deterministic_facts": {
            "chains_completed": 0,
            "tshirt_counts": {},
            "opus_dispatches": 0,
            "commits": 0,
        },
        "narrative": None,
        "input_watermark": {
            "max_observed_at": "2026-03-18T12:00:00Z",
            "max_commit_sha": "0000000",
            "source_count": 0,
        },
        "freshness": "stale",
        "provenance": ctx.provenance(
            "coordinator_artifact", path="archive/completed", derivation="rolled_up"
        ),
    }
    if fact_window_present:
        row["fact_window"] = {"kind": "day", "start": "2026-03-18", "end": "2026-03-18"}
    return row


def _minimal_week_row(ctx: EmitContext, *, fact_window_present: bool) -> dict:
    row = {
        "grain": "week",
        "period": "2026-W12",
        "repo": "test/repo",
        "coordinator_root_path": ".",
        "deterministic_facts": {
            "chains_completed": 0,
            "tshirt_counts": {},
            "opus_dispatches": 0,
            "commits": 0,
            "reviews_conducted": 0,
            "verdicts": {},
        },
        "narrative": None,
        "input_watermark": {
            "max_observed_at": "2026-03-18T12:00:00Z",
            "max_commit_sha": "0000000",
            "source_count": 0,
        },
        "freshness": "stale",
        "provenance": ctx.provenance(
            "coordinator_artifact", path="archive/completed", derivation="rolled_up"
        ),
    }
    if fact_window_present:
        row["fact_window"] = {
            "kind": "iso-week",
            "start": "2026-03-16",
            "end": "2026-03-22",
        }
    return row

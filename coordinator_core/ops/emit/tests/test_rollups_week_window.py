"""Unit coverage for ``rollups.collect``'s WEEK-grain window narrowing — the fix for the
week rollup counting the 30-day ``_since_cutoff`` set wholesale instead of narrowing to the
ISO week its own ``period`` names.

Spec backlink: docs/plans/2026-09-04-the-weekly-completion-count-means-the-week.md § C1/C2
"""

from __future__ import annotations

import datetime
from pathlib import Path

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.rollups import collect

# completion glob is 'archive/completed/*/*.md' (records_query._TYPE_TO_GLOB) — one
# wildcard subdirectory level, delimited YAML frontmatter.
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
) -> None:
    path = tmp_path / _COMPLETED_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    created_line = created if created is not None else ""
    body = (
        "---\n"
        f"title: \"{name}\"\n"
        + (f"created: {created_line}\n" if created is not None else "")
        + f"chain: \"{chain}\"\n"
        "loe:\n"
        "  tshirt: S\n"
        "commits:\n"
        f"  - \"{commit}\"\n"
        "status: done\n"
        "---\n\n"
        "Test completion fixture.\n"
    )
    path.write_text(body, encoding="utf-8")


def _week_rollup(ctx: EmitContext):
    records, malformed = collect(ctx)
    assert malformed == []
    week = [r for r in records if r["grain"] == "week"][0]
    day = [r for r in records if r["grain"] == "day"][0]
    return week, day


def test_completion_inside_observed_week_is_counted(tmp_path):
    """An unquoted ``created: 2026-03-16`` (Monday of the observed ISO week) is counted.

    NOTE, corrected after review: this does NOT arrive as a ``datetime.date``. The emit path
    reads completions through ``ops.ceremony.records_query``, whose hand-rolled frontmatter
    parser returns strings and never constructs a date — measured at 486/486 ``str`` over
    this repo's corpus on 2026-09-04. ``schema_validate``'s ``_coerce_dates_to_strings``
    leniency, which does see YAML dates, is on the VALIDATION path, not this one. An earlier
    revision of this test asserted the date-object rationale and was wrong about the
    pipeline while still passing, which is why the claim is spelled out here rather than
    left implicit. ``_created_date`` normalizes via ``str()`` regardless, so both shapes work
    — that is the seam's own contract, not a coincidence."""
    observed_at = "2026-03-18T12:00:00Z"  # Wednesday of 2026-W12
    _write_completion(tmp_path, "inside.md", "2026-03-16")  # Monday of same ISO week
    ctx = _make_ctx(observed_at, tmp_path)

    week, _day = _week_rollup(ctx)

    assert week["period"] == "2026-W12"
    assert week["deterministic_facts"]["chains_completed"] == 1
    assert week["deterministic_facts"]["tshirt_counts"] == {"S": 1}
    assert week["deterministic_facts"]["commits"] == 1
    assert week["input_watermark"]["source_count"] == 1


def test_completion_8_days_before_observed_at_is_inside_cutoff_but_outside_week(tmp_path):
    """This is the regression that would have caught the original defect: 8 days back is
    inside the 30-day ``_since_cutoff`` window (so the old, unfiltered week rollup counted
    it) but outside the ISO week named by ``period`` — the corrected rollup must NOT count
    it."""
    observed_at = "2026-03-18T12:00:00Z"  # Wednesday of 2026-W12
    _write_completion(tmp_path, "eight-days-back.md", "2026-03-10")  # W11, 8 days earlier
    ctx = _make_ctx(observed_at, tmp_path)

    week, _day = _week_rollup(ctx)

    assert week["period"] == "2026-W12"
    assert week["deterministic_facts"]["chains_completed"] == 0
    assert week["deterministic_facts"]["tshirt_counts"] == {}
    assert week["deterministic_facts"]["commits"] == 0
    assert week["input_watermark"]["source_count"] == 0


def test_day_rollup_and_max_commit_sha_still_reach_the_full_30_day_set(tmp_path):
    """Proves ``_since_cutoff`` was not narrowed: an old-week completion still contributes
    to ``max_commit_sha`` (computed across ALL entries) even though it is excluded from the
    week rollup's facts, and today's completion still reaches the day rollup."""
    observed_at = "2026-03-18T12:00:00Z"
    today = "2026-03-18"
    _write_completion(tmp_path, "today.md", today, commit="1111111")
    _write_completion(
        tmp_path, "eight-days-back.md", "2026-03-10", chain="chain-b", commit="fffffff"
    )
    ctx = _make_ctx(observed_at, tmp_path)

    week, day = _week_rollup(ctx)

    assert day["period"] == today
    assert day["deterministic_facts"]["chains_completed"] == 1
    assert day["deterministic_facts"]["commits"] == 1
    assert day["input_watermark"]["source_count"] == 1
    # max_commit_sha spans ALL entries in the 30-day set, including the out-of-week one.
    assert week["input_watermark"]["max_commit_sha"] == "fffffff"
    assert day["input_watermark"]["max_commit_sha"] == "fffffff"



def test_completion_with_missing_created_is_excluded_without_raising(tmp_path):
    observed_at = "2026-03-18T12:00:00Z"
    _write_completion(tmp_path, "no-created.md", None)
    ctx = _make_ctx(observed_at, tmp_path)

    week, _day = _week_rollup(ctx)

    assert week["deterministic_facts"]["chains_completed"] == 0
    assert week["input_watermark"]["source_count"] == 0


def test_completion_with_malformed_created_is_excluded_without_raising(tmp_path):
    observed_at = "2026-03-18T12:00:00Z"
    _write_completion(tmp_path, "malformed.md", '"not-a-date"')
    ctx = _make_ctx(observed_at, tmp_path)

    week, _day = _week_rollup(ctx)

    assert week["deterministic_facts"]["chains_completed"] == 0
    assert week["input_watermark"]["source_count"] == 0


def test_created_spanning_the_iso_year_boundary_is_counted(tmp_path):
    """2027-01-01 belongs to ISO week 2026-W53 — its ISO year is the PREVIOUS calendar year.

    This is the case the tuple comparison exists for. A filter keyed on the calendar year
    would put a 2027-01-01 completion in 2027 and drop it from the 2026-W53 row it belongs
    to; one that formatted ``YYYY-Www`` and string-compared would have the same bug wearing
    a label. Both records below are in ISO 2026-W53 despite falling in different calendar
    years, and both must be counted.

    (An earlier revision of this test asserted 2026-12-28 was 2027-W01. It is 2026-W53 —
    2026 is a 53-week ISO year. The test failed and is kept pointed at a verified boundary.)"""
    observed_at = "2027-01-01T12:00:00Z"  # ISO 2026-W53, calendar year 2027
    # Distinct chains: _dedup_by_chain would otherwise collapse these to one, which is
    # correct behaviour and would mask what this test is actually asking about.
    _write_completion(tmp_path, "boundary-prev-year.md", "2026-12-31", chain="chain-a")
    _write_completion(tmp_path, "boundary-this-year.md", "2027-01-02", chain="chain-b")
    _write_completion(tmp_path, "boundary-out.md", "2027-01-04", chain="chain-c")  # W01
    ctx = _make_ctx(observed_at, tmp_path)

    week, _day = _week_rollup(ctx)

    assert datetime.date(2027, 1, 1).isocalendar()[:2] == (2026, 53)
    assert datetime.date(2027, 1, 4).isocalendar()[:2] == (2027, 1)
    assert week["period"] == "2026-W53"
    assert week["deterministic_facts"]["chains_completed"] == 2
    assert week["input_watermark"]["source_count"] == 2


def test_timestamp_shaped_created_is_counted_not_silently_dropped(tmp_path):
    """A full-timestamp ``created`` must be counted, matching the records seam.

    The seam's ``since`` filter is ``str(created) >= cutoff`` — a lexicographic compare that
    lets ``"2026-03-16T12:00:00Z"`` through. ``date.fromisoformat`` rejects every timestamp
    form, so parsing the whole value would drop a record the seam had already counted, with
    no signal — silent data loss in a number a human reads. ``_created_date`` takes the
    leading ``YYYY-MM-DD`` for exactly this reason. Not a shape in the corpus today; this
    keeps the two filters from diverging if it ever appears."""
    observed_at = "2026-03-18T12:00:00Z"
    _write_completion(tmp_path, "timestamp-created.md", "2026-03-16T12:00:00Z")
    ctx = _make_ctx(observed_at, tmp_path)

    week, _day = _week_rollup(ctx)

    assert week["deterministic_facts"]["chains_completed"] == 1
    assert week["input_watermark"]["source_count"] == 1



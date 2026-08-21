"""
Tests for `coordinator_core.engine_root_census` -- the durable sink that
makes C14's item 4 ("no site has read the fallback in N days") answerable
at all.

Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md
               chunk C14 item 4 / AC24 clause 2.

The load-bearing test in this file is
`test_fresh_series_does_not_evidence_absence`: every other property here is
ordinary bookkeeping, but a census that reports a day-old empty sink as
"zero reads for 7 days" would licence closing the dual-read window on
evidence that does not exist -- the exact defect the sink was built to
remove, one layer down.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core import engine_root, engine_root_census

_DAY = 86400.0
_NOW = 1_760_000_000.0


@pytest.fixture
def sink(tmp_path):
    return tmp_path / "settings-home"


def _census(sink, **kw):
    kw.setdefault("now", _NOW)
    return engine_root_census.census(sink_root=sink, **kw)


# --- the absence verdict --------------------------------------------------


def test_missing_series_does_not_evidence_absence(sink):
    """No prior sink data reads as "we started watching just now", never
    "nothing happened". C24: calling `census()` at all starts the watch
    clock (the sentinel), so `series_present` flips True on the FIRST call
    even with zero reads ever recorded -- but the window has not elapsed
    yet, so the verdict still cannot clear."""
    report = _census(sink)
    assert report["series_present"] is True
    assert report["watch_start_ts"] == pytest.approx(_NOW)
    assert report["observed_days"] == pytest.approx(0.0)
    assert report["evidences_absence"] is False
    assert report["total_reads"] == 0
    assert report["days_since_last"] is None


def test_fresh_series_does_not_evidence_absence(sink):
    """A series younger than the window CANNOT evidence absence over that
    window, however quiet it is.

    This is the whole point of the module. The sink here has one read a day
    old, so the 7-day window is empty of reads for its last ~6 days -- and
    the verdict must still be False, because the series was not watching
    for the other six.
    """
    engine_root_census.record_fallback_read(
        "some.site", sink_root=sink, now=_NOW - (1 * _DAY)
    )
    report = _census(sink, window_days=7)

    assert report["series_present"] is True
    assert report["observed_days"] == pytest.approx(1.0)
    assert report["evidences_absence"] is False


def test_quiet_mature_series_evidences_absence(sink):
    """Observing for longer than the window with zero reads inside it is
    the one shape that clears."""
    engine_root_census.record_fallback_read(
        "some.site", sink_root=sink, now=_NOW - (30 * _DAY)
    )
    report = _census(sink, window_days=7)

    assert report["observed_days"] == pytest.approx(30.0)
    assert report["reads_in_window"] == 0
    assert report["total_reads"] == 1
    assert report["days_since_last"] == pytest.approx(30.0)
    assert report["evidences_absence"] is True


def test_a_read_inside_the_window_denies_absence(sink):
    """A mature series still fails the verdict while anything reads."""
    engine_root_census.record_fallback_read(
        "old.site", sink_root=sink, now=_NOW - (30 * _DAY)
    )
    engine_root_census.record_fallback_read(
        "live.site", sink_root=sink, now=_NOW - (2 * _DAY)
    )
    report = _census(sink, window_days=7)

    assert report["reads_in_window"] == 1
    assert report["evidences_absence"] is False


def test_window_days_is_echoed_back(sink):
    """The N asked cannot be lost between call and report."""
    assert _census(sink, window_days=14)["window_days"] == 14


# --- C24: watch-start sentinel ---------------------------------------------
#
# RAISED BY CODE REVIEW 2026-08-20 (session 8211c764): before this fix,
# `observed_days` was measured from `series_first_ts` -- the first RECORDED
# READ -- so a box that never had a stale pin never created the series, and
# `evidences_absence` could NEVER become True. The verdict was reachable
# only on a box that once had the defect. `test_clean_box_reaches_evidences_absence_after_window_elapses`
# is the load-bearing test in this section: it is the exact shape a
# cleanly-converged box takes, and it must clear.


def test_clean_box_reaches_evidences_absence_after_window_elapses(sink):
    """THE BUG THIS CHUNK FIXES. Zero calls to `record_fallback_read` ever
    -- the series file never gets created -- and the verdict must still be
    able to clear once the watch itself has run long enough."""
    first = _census(sink, window_days=7, now=_NOW)
    assert first["series_present"] is True
    assert first["total_reads"] == 0
    assert first["evidences_absence"] is False, (
        "the watch just started -- not enough elapsed time yet"
    )

    later = _census(sink, window_days=7, now=_NOW + (8 * _DAY))
    assert later["series_present"] is True
    assert later["total_reads"] == 0
    assert later["observed_days"] == pytest.approx(8.0)
    assert later["evidences_absence"] is True


def test_window_not_elapsed_since_watch_start_denies_absence(sink):
    """A watch started 3 days ago cannot clear a 7-day window, even with
    zero reads -- the series-age guard, one layer down from the reads
    themselves."""
    _census(sink, window_days=7, now=_NOW)  # starts the clock
    report = _census(sink, window_days=7, now=_NOW + (3 * _DAY))

    assert report["observed_days"] == pytest.approx(3.0)
    assert report["evidences_absence"] is False


def test_sentinel_is_not_counted_as_a_read(sink):
    """The watch-start sentinel must never inflate the read counters --
    inverting this would mean the sink NEVER evidences absence, since every
    census would see at least one "read"."""
    engine_root_census.ensure_watch_start(sink_root=sink, now=_NOW - (30 * _DAY))
    report = _census(sink, window_days=7)

    assert report["total_reads"] == 0
    assert report["reads_in_window"] == 0
    assert report["sites"] == {}
    assert report["series_first_ts"] is None


def test_sentinel_never_lands_in_corruption_buckets(sink):
    """The sentinel lives in a separate file from the jsonl series, so the
    series-parsing loop -- and its corruption guard -- never sees it."""
    engine_root_census.ensure_watch_start(sink_root=sink, now=_NOW - (30 * _DAY))
    report = _census(sink, window_days=7)

    assert report["unparsable_rows"] == 0
    assert report["undatable_rows"] == 0
    assert report["evidences_absence"] is True


def test_ensure_watch_start_is_idempotent_and_race_safe(sink):
    """Concurrent/repeated initialization keeps the EARLIEST timestamp --
    this machine races 50-70 concurrent sessions, and none of them should
    be able to reset another's clock."""
    first = engine_root_census.ensure_watch_start(sink_root=sink, now=_NOW - _DAY)
    second = engine_root_census.ensure_watch_start(sink_root=sink, now=_NOW)

    assert first == pytest.approx(_NOW - _DAY)
    assert second == pytest.approx(_NOW - _DAY)


def test_record_fallback_read_starts_the_clock_at_its_own_timestamp(sink):
    """A box that DOES see a stale pin keeps `observed_days` measured from
    that earliest read, not reset to whenever a report is later pulled."""
    engine_root_census.record_fallback_read(
        "some.site", sink_root=sink, now=_NOW - (30 * _DAY)
    )
    report = _census(sink, window_days=7)

    assert report["watch_start_ts"] == pytest.approx(_NOW - (30 * _DAY))
    assert report["observed_days"] == pytest.approx(30.0)


# --- the record side ------------------------------------------------------


def test_record_appends_one_json_line_per_call(sink):
    engine_root_census.record_fallback_read("a.site", sink_root=sink, now=_NOW)
    engine_root_census.record_fallback_read("b.site", sink_root=sink, now=_NOW)

    lines = (
        engine_root_census.series_path(sink)
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 2
    assert [json.loads(ln)["site"] for ln in lines] == ["a.site", "b.site"]


def test_record_captures_the_root_that_answered(sink):
    """The 2026-08-20 measurement's real finding was the CAUSE, not the
    count -- every read traced to a hand-pinned old name. A recurrence
    should be self-diagnosing."""
    engine_root_census.record_fallback_read(
        "a.site", root_value="X:/claude-klabauter", sink_root=sink, now=_NOW
    )
    entry = json.loads(
        engine_root_census.series_path(sink).read_text(encoding="utf-8").strip()
    )
    assert entry["root"] == "X:/claude-klabauter"


def test_record_never_raises_on_an_unwritable_sink(tmp_path):
    """Negative-spec: this runs on the commit hot path. A sink whose parent
    is a FILE cannot be created, and that must degrade to "not recorded"."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    engine_root_census.record_fallback_read("a.site", sink_root=blocker, now=_NOW)


def test_census_never_raises_on_a_corrupt_series(sink):
    """A corrupt series degrades to what it can prove -- and critically
    NOT to a false clear."""
    path = engine_root_census.series_path(sink)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{not json at all\n"
        + json.dumps({"site": "ok.site", "ts": _NOW - (30 * _DAY)})
        + "\n"
        + json.dumps({"site": "no-ts.site"})
        + "\n"
        + "\n",
        encoding="utf-8",
    )
    report = _census(sink, window_days=7)

    assert report["total_reads"] == 1
    assert report["sites"]["ok.site"]["count"] == 1

    # THIS ASSERTION WAS INVERTED ON 2026-08-20, and the inversion is the
    # point of the test rather than a relaxation of it. It previously
    # asserted `evidences_absence is True` — i.e. a file containing a torn
    # line and an undatable row was allowed to CLEAR C14's exit condition —
    # under a docstring promising "critically NOT to a false clear". The
    # docstring was right and the assertion was wrong: skipping a bad row
    # lowers `reads_in_window` without lowering `observed_days`, so a single
    # torn line that happened to be the only in-window read manufactures a
    # clear. Unreadable rows now block the verdict in both directions.
    assert report["unparsable_rows"] == 1
    assert report["undatable_rows"] == 1
    assert report["evidences_absence"] is False


def test_sites_are_reported_per_site(sink):
    """C14's exit wants to know WHICH site still reads, not only that one
    does -- the 2026-08-20 measurement's value was the single named site."""
    engine_root_census.record_fallback_read("a.site", sink_root=sink, now=_NOW - _DAY)
    engine_root_census.record_fallback_read(
        "a.site", sink_root=sink, now=_NOW - (3 * _DAY)
    )
    engine_root_census.record_fallback_read("b.site", sink_root=sink, now=_NOW)

    sites = _census(sink)["sites"]
    assert sites["a.site"]["count"] == 2
    assert sites["a.site"]["first_ts"] == pytest.approx(_NOW - (3 * _DAY))
    assert sites["a.site"]["last_ts"] == pytest.approx(_NOW - _DAY)
    assert sites["b.site"]["count"] == 1


# --- the wiring into the accessor ----------------------------------------


def test_accessor_retired_name_read_reaches_the_sink(sink, monkeypatch):
    """The hook and the sink fire together, so the sink cannot silently stop
    tracking the advisory.

    POST-C14 THIS MEASURES SOMETHING DIFFERENT and better. Before C14 it
    recorded a read that ANSWERED; now the old name answers nothing, so a row
    here is a STALE PIN — an operator or ancestor process still exporting a
    retired name. That inverts the census from "evidence a window may close"
    into "regression detector for a stale pin", which is the residual risk the
    close-without-a-soak accepted."""
    recorded = []
    monkeypatch.setattr(
        engine_root_census,
        "record_fallback_read",
        lambda site, **kw: recorded.append((site, kw.get("root_value"))),
    )
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "X:/somewhere")
    engine_root._reset_engine_root_env_advisories()

    assert engine_root.coordinator_engine_root_env("test.site") is None
    assert recorded == [("test.site", "X:/somewhere")], (
        "the retired name must still be OBSERVED even though it no longer answers"
    )


def test_accessor_records_once_per_site_per_process(sink, monkeypatch):
    """The sink inherits the advisory's dedupe rather than adding a write
    per read -- the cost property that keeps this off the hot path's
    budget."""
    recorded = []
    monkeypatch.setattr(
        engine_root_census,
        "record_fallback_read",
        lambda site, **kw: recorded.append(site),
    )
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "X:/somewhere")
    engine_root._reset_engine_root_env_advisories()

    for _ in range(5):
        engine_root.coordinator_engine_root_env("test.site")

    assert recorded == ["test.site"]


def test_new_name_winning_records_nothing(sink, monkeypatch):
    """A read answered by the new name is convergence. Recording it would
    drown the stale-pin signal this sink now exists to carry."""
    recorded = []
    monkeypatch.setattr(
        engine_root_census,
        "record_fallback_read",
        lambda site, **kw: recorded.append(site),
    )
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", "X:/new")
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "X:/old")
    engine_root._reset_engine_root_env_advisories()

    assert engine_root.coordinator_engine_root_env("test.site") == "X:/new"
    assert recorded == []

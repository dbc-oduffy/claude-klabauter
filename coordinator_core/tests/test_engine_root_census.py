"""
Tests for `coordinator_core.engine_root_census` -- the durable stale-pin
regression detector for the retired `CLAUDE_KLABAUTER_ROOT` fallback.

Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md
               chunk C14 item 4 / AC24 clause 2 -- SUPERSEDED by C23's
               three-leg ratchet at `02ef8ae9de77`; this module reports
               observations only, never a verdict. See the module
               docstring.
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


# --- observation counters --------------------------------------------------


def test_missing_series_reports_no_observations(sink):
    """No prior sink data reads as "never observed", not an error."""
    report = _census(sink)
    assert report["series_present"] is False
    assert report["total_reads"] == 0
    assert report["days_since_last"] is None


def test_a_read_inside_the_window_is_counted(sink):
    """A read inside the window is reflected in `reads_in_window` -- the
    stale-pin regression signal this module now exists to carry."""
    engine_root_census.record_fallback_read(
        "old.site", sink_root=sink, now=_NOW - (30 * _DAY)
    )
    engine_root_census.record_fallback_read(
        "live.site", sink_root=sink, now=_NOW - (2 * _DAY)
    )
    report = _census(sink, window_days=7)

    assert report["reads_in_window"] == 1
    assert report["total_reads"] == 2


def test_window_days_is_echoed_back(sink):
    """The N asked cannot be lost between call and report."""
    assert _census(sink, window_days=14)["window_days"] == 14


def test_no_verdict_field_in_report(sink):
    """Regression test: `census()` must never re-grow a verdict field.

    C14 item 4 was discharged elsewhere (C23's three-leg ratchet, a proven
    property of the code); a returned `evidences_absence`-shaped key here
    would invite a future reader to close that item a second time by
    waiting for a field that measures operator shell hygiene, not code --
    the exact close-by-waiting trap the field's removal exists to forbid.

    This is an absence assertion, so its reachable case is checked too:
    the rest of the report must still be populated (`reads_in_window` and
    `total_reads` present), proving the report was actually computed and
    not merely empty.
    """
    report = _census(sink)
    assert "evidences_absence" not in report
    assert "reads_in_window" in report
    assert "total_reads" in report


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

    # A torn line and an undatable row are preserved in their own buckets,
    # not folded into `total_reads`/`reads_in_window` -- see the in-loop
    # comment in `census()`. A reader must check these buckets alongside
    # `reads_in_window`, since a live signal can hide in a torn row that
    # neither main counter reflects.
    assert report["unparsable_rows"] == 1
    assert report["undatable_rows"] == 1


def test_days_since_last_computes_the_real_elapsed_gap(sink):
    """`days_since_last` must compute a real, non-None value off an actual
    recorded read, not just report None on an empty sink -- the only case
    the rest of the suite otherwise exercises."""
    engine_root_census.record_fallback_read(
        "a.site", sink_root=sink, now=_NOW - (30 * _DAY)
    )
    report = _census(sink)

    assert report["days_since_last"] is not None
    assert report["days_since_last"] == pytest.approx(30.0)


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

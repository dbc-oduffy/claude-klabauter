"""Outage durations out of `listener_availability.report` are BOUNDS, and a
single-sample outage is never reported as zero.

WHY THIS FILE EXISTS. `report()` used to publish each outage run's duration as
`(last_down_sample - first_down_sample)`. Two defects, both of which reached a
cross-repo decision record before anyone noticed:

  - A one-sample outage has `end == start`, so it printed **0.0 minutes** -- a
    real outage rounding to no outage at all.
  - A multi-sample run excluded the entire unobserved interval on each side, so
    at a 30s interval a genuine 31s outage and a genuine 89s outage both
    printed ~30s. The figure was a lower bound wearing a point estimate's
    clothes, and it was quoted as "outage ... for 30.2s".

The sampler cannot see inside an interval. For k consecutive down samples it
establishes down-for-at-least `(k-1)*interval` and at-most `(k+1)*interval`,
and that is all it establishes.

NEGATIVE SPEC. These tests assert nothing about whether the listener is
actually available, about p(listener up), or about any cause of an outage. They
pin the arithmetic that turns samples into a claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import listener_availability as la


def _sink(tmp_path: Path, outcomes, *, interval: float = 30.0, t0: float = 1000.0) -> Path:
    p = tmp_path / "avail.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for i, outcome in enumerate(outcomes):
            fh.write(
                json.dumps({"t": t0 + i * interval, "outcome": outcome, "probe_ms": 1.0})
                + "\n"
            )
    return p


UP = la.UP
DOWN = "dead_record"


class TestSingleSampleOutage:
    def test_is_not_reported_as_zero(self, tmp_path: Path) -> None:
        rep = la.report(_sink(tmp_path, [UP, UP, DOWN, UP, UP]))
        (run,) = rep["outage_runs"]
        assert run["samples"] == 1
        assert run["at_most_secs"] == pytest.approx(60.0)
        assert rep["longest_outage_at_most_secs"] == pytest.approx(60.0)

    def test_its_lower_bound_is_zero_and_is_labelled_as_a_bound(
        self, tmp_path: Path
    ) -> None:
        rep = la.report(_sink(tmp_path, [UP, DOWN, UP]))
        (run,) = rep["outage_runs"]
        assert run["at_least_secs"] == pytest.approx(0.0)
        assert "at_least_secs" in run and "at_most_secs" in run
        assert "minutes" not in run, "the point-estimate field must not come back"


class TestMultiSampleOutage:
    def test_bounds_bracket_the_observed_span(self, tmp_path: Path) -> None:
        rep = la.report(_sink(tmp_path, [UP, DOWN, DOWN, UP]))
        (run,) = rep["outage_runs"]
        assert run["samples"] == 2
        assert run["observed_span_secs"] == pytest.approx(30.0)
        assert run["at_least_secs"] == pytest.approx(30.0)
        assert run["at_most_secs"] == pytest.approx(90.0)
        assert run["at_least_secs"] <= run["observed_span_secs"] <= run["at_most_secs"]

    def test_the_reported_headline_is_the_upper_bound(self, tmp_path: Path) -> None:
        rep = la.report(_sink(tmp_path, [UP, DOWN, UP, DOWN, DOWN, DOWN, UP]))
        assert rep["longest_outage_at_most_secs"] == pytest.approx(120.0)
        assert "longest_outage_minutes" not in rep


class TestIntervalDerivation:
    def test_interval_comes_off_the_samples_not_a_default(self, tmp_path: Path) -> None:
        rep = la.report(_sink(tmp_path, [UP, DOWN, DOWN, UP], interval=10.0))
        assert rep["sample_interval_secs"] == pytest.approx(10.0)
        (run,) = rep["outage_runs"]
        assert run["at_most_secs"] == pytest.approx(30.0)

    def test_a_single_long_gap_does_not_set_the_interval(self, tmp_path: Path) -> None:
        """One stalled sample must not inflate every outage bound -- the median
        delta is used precisely so a lone outlier cannot become the yardstick.
        """
        p = tmp_path / "avail.jsonl"
        ts = [0.0, 30.0, 60.0, 600.0, 630.0, 660.0]
        outcomes = [UP, UP, DOWN, DOWN, UP, UP]
        with p.open("w", encoding="utf-8") as fh:
            for t, outcome in zip(ts, outcomes):
                fh.write(json.dumps({"t": t, "outcome": outcome}) + "\n")
        assert la.report(p)["sample_interval_secs"] == pytest.approx(30.0)


def test_the_caveat_travels_with_the_numbers(tmp_path: Path) -> None:
    """The bounds are only honest if the reader is told they are bounds. This
    text is quoted into cross-repo memos, so its absence is a real regression.
    """
    rep = la.report(_sink(tmp_path, [UP, DOWN, UP]))
    caveat = rep["outage_duration_caveat"]
    assert "at least" in caveat and "at most" in caveat
    assert "observed_span_secs" in caveat


def test_an_all_up_sink_reports_no_outages_and_no_headline(tmp_path: Path) -> None:
    rep = la.report(_sink(tmp_path, [UP, UP, UP]))
    assert rep["outage_runs"] == []
    assert rep["longest_outage_at_most_secs"] == 0.0
    assert rep["p_listener_up"] == 1.0

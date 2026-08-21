"""coordinator_core.op_census.tests.test_line_count — tests for the
line-count axis and its frozen high-water ratchet (C5).

Named `test_line_count.py`, not `test_line_count_ratchet.py`, per the
dispatch-emit terminal-test-scope resolver's exact-stem-match rule
(`coordinator_core/ops/dispatch_emit/pathspec.py :: _candidate_test_targets`
maps `line_count.py` -> `tests/test_line_count.py` only) — the ratchet's
descriptive intent lives in `test_line_count_ratchet` below instead, as a
test *function* name inside this stem-matched module.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C5.md
"""

from __future__ import annotations

import pytest

from coordinator_core.op_census.line_count import (
    FROZEN_HIGH_WATER_LINES,
    FROZEN_HIGH_WATER_MODULES,
    FROZEN_HIGH_WATER_OVER_BAR,
    PER_MODULE_LINE_BAR,
    LineCountDistribution,
    RatchetError,
    compute_distribution,
    evaluate_ratchet,
    ratchet_check,
)
from coordinator_core.op_census.module_summary import ModuleSummary


def _summary(path: str, line_count: int) -> ModuleSummary:
    return ModuleSummary(
        path=path,
        stamp="deadbeef",
        function_names=(),
        spawn_call_sites=0,
        line_count=line_count,
        parse_error=None,
    )


def test_compute_distribution_aggregates_totals_and_over_bar_set():
    summaries = [
        _summary("a.py", 10),
        _summary("b.py", PER_MODULE_LINE_BAR),
        _summary("c.py", PER_MODULE_LINE_BAR + 1),
        _summary("d.py", 5000),
    ]

    distribution = compute_distribution(summaries)

    assert distribution.module_count == 4
    assert distribution.total_lines == 10 + PER_MODULE_LINE_BAR + (PER_MODULE_LINE_BAR + 1) + 5000
    assert distribution.over_bar_count == 2
    assert set(distribution.over_bar_modules) == {"c.py", "d.py"}
    assert distribution.over_bar_modules["d.py"] == 5000


def test_compute_distribution_empty_corpus():
    distribution = compute_distribution([])

    assert distribution.module_count == 0
    assert distribution.total_lines == 0
    assert distribution.over_bar_count == 0
    assert distribution.over_bar_modules == {}


def test_compute_distribution_includes_parse_error_line_count():
    summaries = [_summary("broken.py", 42)]
    broken = ModuleSummary(
        path="broken.py",
        stamp="deadbeef",
        function_names=(),
        spawn_call_sites=0,
        line_count=42,
        parse_error="SyntaxError: bad",
    )

    distribution = compute_distribution([broken])

    assert distribution.module_count == 1
    assert distribution.total_lines == 42


def test_distribution_to_dict_round_trips_fields():
    distribution = LineCountDistribution(
        module_count=2,
        total_lines=100,
        over_bar_count=1,
        over_bar_modules={"x.py": 60},
    )

    as_dict = distribution.to_dict()

    assert as_dict == {
        "module_count": 2,
        "total_lines": 100,
        "over_bar_count": 1,
        "over_bar_modules": {"x.py": 60},
    }


def test_ratchet_check_allows_at_exactly_the_frozen_high_water():
    distribution = LineCountDistribution(
        module_count=FROZEN_HIGH_WATER_MODULES,
        total_lines=FROZEN_HIGH_WATER_LINES,
        over_bar_count=FROZEN_HIGH_WATER_OVER_BAR,
        over_bar_modules={},
    )

    ratchet_check(distribution)  # must not raise


def test_ratchet_check_allows_a_shrink():
    distribution = LineCountDistribution(
        module_count=FROZEN_HIGH_WATER_MODULES - 1,
        total_lines=FROZEN_HIGH_WATER_LINES - 1000,
        over_bar_count=FROZEN_HIGH_WATER_OVER_BAR - 1,
        over_bar_modules={},
    )

    ratchet_check(distribution)  # must not raise


def test_line_count_ratchet():
    """The AC4-shaped ratchet: the corpus may shrink or hold, never grow
    past its frozen high-water, without a deliberate, reasoned edit to
    FROZEN_HIGH_WATER_* — refuses (raises), never degrades or skips."""
    distribution = LineCountDistribution(
        module_count=FROZEN_HIGH_WATER_MODULES + 1,
        total_lines=FROZEN_HIGH_WATER_LINES,
        over_bar_count=FROZEN_HIGH_WATER_OVER_BAR,
        over_bar_modules={},
    )

    with pytest.raises(RatchetError, match="module_count"):
        ratchet_check(distribution)


def test_line_count_ratchet_trips_on_total_lines_growth_alone():
    distribution = LineCountDistribution(
        module_count=FROZEN_HIGH_WATER_MODULES,
        total_lines=FROZEN_HIGH_WATER_LINES + 1,
        over_bar_count=FROZEN_HIGH_WATER_OVER_BAR,
        over_bar_modules={},
    )

    with pytest.raises(RatchetError, match="total_lines"):
        ratchet_check(distribution)


def test_line_count_ratchet_trips_on_over_bar_count_growth_alone():
    distribution = LineCountDistribution(
        module_count=FROZEN_HIGH_WATER_MODULES,
        total_lines=FROZEN_HIGH_WATER_LINES,
        over_bar_count=FROZEN_HIGH_WATER_OVER_BAR + 1,
        over_bar_modules={},
    )

    with pytest.raises(RatchetError, match="over_bar_count"):
        ratchet_check(distribution)


def test_evaluate_ratchet_never_raises_and_reports_tripped_true_on_growth():
    """Staff-eng Finding 5: measurement separated from verdict.
    `evaluate_ratchet` returns a `RatchetOutcome` even when the corpus has
    grown past the frozen high-water -- `ratchet_check` (raising) is built
    on top of this, never the reverse."""
    distribution = LineCountDistribution(
        module_count=FROZEN_HIGH_WATER_MODULES + 1,
        total_lines=FROZEN_HIGH_WATER_LINES,
        over_bar_count=FROZEN_HIGH_WATER_OVER_BAR,
        over_bar_modules={},
    )

    outcome = evaluate_ratchet(distribution)

    assert outcome.tripped is True
    assert outcome.module_count == {
        "frozen": FROZEN_HIGH_WATER_MODULES,
        "measured": FROZEN_HIGH_WATER_MODULES + 1,
    }
    assert outcome.total_lines == {"frozen": FROZEN_HIGH_WATER_LINES, "measured": FROZEN_HIGH_WATER_LINES}
    assert outcome.over_bar_count == {
        "frozen": FROZEN_HIGH_WATER_OVER_BAR,
        "measured": FROZEN_HIGH_WATER_OVER_BAR,
    }
    assert outcome.to_dict()["tripped"] is True


def test_evaluate_ratchet_reports_tripped_false_on_shrink():
    distribution = LineCountDistribution(
        module_count=FROZEN_HIGH_WATER_MODULES - 1,
        total_lines=FROZEN_HIGH_WATER_LINES - 1000,
        over_bar_count=FROZEN_HIGH_WATER_OVER_BAR - 1,
        over_bar_modules={},
    )

    outcome = evaluate_ratchet(distribution)

    assert outcome.tripped is False

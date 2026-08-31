"""coordinator_core.telemetry.tests.test_breach_summary — the budget-breach
view's contract.

Purpose: guards the properties that make an over-budget op FINDABLE rather
than survivable — the three breach kinds staying separate, the damage
ranking being cost-weighted rather than count-weighted, the bar not being
something a caller can widen, and the trend axis refusing rather than
guessing. Each of these is a way the surface could quietly become useless
while still returning a well-formed dict.
"""

from __future__ import annotations

import pytest

from coordinator_core.op_census.timing import PROCESS_TIME_BAR_MS
from coordinator_core.telemetry.op_latency import (
    BREACH_KINDS,
    DEFAULT_BREACH_BAR_MS,
    TREND_MIN_ATTEMPTS_PER_HALF,
    breach_summary,
)

BASE_T = 1_755_000_000.0


def _complete(op, elapsed_ms, *, t_start=BASE_T, outcome="ok", corr_id=None):
    return {
        "kind": "complete",
        "op": op,
        "t_start": t_start,
        "elapsed_ms": elapsed_ms,
        "outcome": outcome,
        "corr_id": corr_id,
    }


def _started(op, *, t_start=BASE_T, corr_id):
    return {"kind": "started", "op": op, "t_start": t_start, "corr_id": corr_id}


def _row_for(summary, op):
    for row in summary["ops"]:
        if row["op"] == op:
            return row
    raise AssertionError(f"{op!r} absent from {[r['op'] for r in summary['ops']]}")


def test_bar_default_agrees_with_the_one_place_it_is_stated():
    """`op_latency` mirrors the brightline rather than importing it (it must
    stay import-free on the hot path); the two numbers must not drift."""
    assert DEFAULT_BREACH_BAR_MS == PROCESS_TIME_BAR_MS


def test_the_three_breach_kinds_are_counted_separately():
    """A caller timeout, an over-bar completion, and a vanished invocation
    are three different reconcile hazards. Merging any two of them into one
    count is the failure this whole surface exists to prevent."""
    entries = [
        _complete("op.a", 5_000.0, outcome="ok"),
        _complete("op.a", 5_000.0, outcome="timeout"),
        _started("op.a", corr_id="gone", t_start=BASE_T),
    ]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T + 10_000.0)
    row = _row_for(summary, "op.a")

    assert row["over_bar"] == 1
    assert row["caller_timeout"] == 1
    assert row["vanished"] == 1
    assert row["breaches"] == 3
    assert set(BREACH_KINDS) <= set(row)


def test_a_timeout_row_is_never_reclassified_as_over_bar():
    """A `timeout` outcome is its own kind whatever its elapsed says — the
    handler may still have committed, which an `over_bar` row does not
    imply. Classifying by elapsed first would lose exactly that."""
    entries = [_complete("op.a", 30_000.0, outcome="timeout")]
    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["caller_timeout"] == 1
    assert row["over_bar"] == 0


def test_a_fast_timeout_row_still_counts_as_a_breach():
    """A caller that gave up under the bar is still a breach — the caller's
    own budget was shorter than the bar, and the row records an abandoned
    invocation either way."""
    entries = [_complete("op.a", 12.0, outcome="timeout")]
    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["caller_timeout"] == 1
    assert row["stolen_ms"] == 0.0


def test_a_vanished_row_contributes_no_fabricated_cost():
    """A vanished invocation carries no `elapsed_ms`. It must not be given
    one — its cost is unknown from this sink, and inventing a number would
    put a guess into the ranking."""
    entries = [_started("op.a", corr_id="gone", t_start=BASE_T)]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T + 10_000.0)
    row = _row_for(summary, "op.a")

    assert row["vanished"] == 1
    assert row["breaches"] == 1
    assert row["stolen_ms"] == 0.0
    assert row["p50_ms"] is None
    assert row["p95_ms"] is None
    assert row["max_ms"] is None
    assert summary["totals"]["vanished"] == 1
    assert summary["totals"]["stolen_ms"] == 0.0


def test_an_in_flight_started_row_is_not_vanished():
    """A started row younger than the staleness cutoff is an invocation
    still running, not a killed one."""
    entries = [_started("op.a", corr_id="live", t_start=BASE_T)]
    summary = breach_summary(entries, bar_ms=500.0, staleness_cutoff_secs=40.0, now=BASE_T + 5.0)

    assert summary["totals"]["in_flight"] == 1
    assert summary["totals"]["vanished"] == 0
    assert summary["ops"] == []


def test_ranking_is_by_damage_not_by_raw_count():
    """One 30s breach outranks fifty 520ms ones. A raw count inverts that,
    and the inverted order sends the reader to delete the cheap op."""
    entries = [_complete("op.rare", 30_500.0)]
    entries += [_complete("op.frequent", 520.0) for _ in range(50)]

    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T)

    assert [row["op"] for row in summary["ops"]] == ["op.rare", "op.frequent"]
    assert _row_for(summary, "op.frequent")["breaches"] > _row_for(summary, "op.rare")["breaches"]


def test_stolen_ms_counts_only_time_past_the_bar():
    entries = [_complete("op.a", 1_500.0), _complete("op.a", 700.0)]
    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["stolen_ms"] == pytest.approx(1_200.0)


def test_a_caller_timeout_contributes_no_fabricated_cost_to_stolen_ms():
    """A `caller_timeout` row's `elapsed_ms` is the caller's own deadline, not
    a measurement of the handler — which may still be running or may still
    have committed. It must not be summed into `stolen_ms`; an `over_bar` row
    on the same op still contributes normally."""
    entries = [
        _complete("op.a", 30_000.0, outcome="timeout"),
        _complete("op.a", 1_500.0, outcome="ok"),
    ]
    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["stolen_ms"] == pytest.approx(1_000.0)


def test_a_caller_timeout_row_is_excluded_from_the_percentile_pool():
    """The timeout row carries no true duration, so it must not enter the
    percentile pool — `p50_ms`/`p95_ms`/`max_ms` must read as if only the
    non-timeout rows existed."""
    entries = [
        _complete("op.a", 30_000.0, outcome="timeout"),
        _complete("op.a", 1_500.0, outcome="ok"),
    ]
    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["p50_ms"] == pytest.approx(1_500.0)
    assert row["p95_ms"] == pytest.approx(1_500.0)
    assert row["max_ms"] == pytest.approx(1_500.0)


def test_a_caller_timeout_is_still_counted_as_a_breach_despite_zero_cost():
    """The fix must not make a timeout disappear — that would be a different
    bug in the opposite direction. `caller_timeout` is still incremented,
    `breaches` still includes it, and the op still surfaces in `breaching_ops`
    and `totals`."""
    entries = [_complete("op.a", 30_000.0, outcome="timeout")]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T)
    row = _row_for(summary, "op.a")

    assert row["caller_timeout"] == 1
    assert row["breaches"] == 1
    assert summary["totals"]["caller_timeout"] == 1
    assert summary["totals"]["breaching_ops"] == 1


def test_an_op_with_only_timeout_breaches_still_ranks_via_the_count_tiebreaker():
    """An op whose only breaches are timeouts has `stolen_ms == 0.0` but must
    not drop out of the ranked list — it still ranks, via the `-breaches`
    tiebreaker, alongside a genuinely `stolen_ms`-bearing op."""
    entries = [_complete("op.timeout_only", 30_000.0, outcome="timeout") for _ in range(3)]
    entries.append(_complete("op.slow", 9_000.0))

    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T)

    assert {row["op"] for row in summary["ops"]} == {"op.timeout_only", "op.slow"}
    row = _row_for(summary, "op.timeout_only")
    assert row["stolen_ms"] == 0.0
    assert row["breaches"] == 3


def test_a_clean_op_is_absent_from_the_ranked_list():
    """The list is a deletion queue. Padding it with compliant ops buries
    the ones that need action."""
    entries = [_complete("op.clean", 12.0), _complete("op.slow", 9_000.0)]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T)

    assert [row["op"] for row in summary["ops"]] == ["op.slow"]
    assert summary["totals"]["complete_rows"] == 2


def test_top_n_never_shrinks_the_reported_population():
    """A truncated list must not read as a clean box — `breaching_ops`
    stays the untruncated count."""
    entries = [_complete(f"op.{i}", 1_000.0 + i) for i in range(10)]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T, top_n=3)

    assert len(summary["ops"]) == 3
    assert summary["totals"]["breaching_ops"] == 10
    assert summary["totals"]["over_bar"] == 10


def test_breach_rate_denominator_includes_vanished_attempts():
    """A vanished invocation was an attempt that produced no completion.
    Counting it only in the numerator would report a rate above 1.0."""
    entries = [_complete("op.a", 12.0) for _ in range(3)]
    entries.append(_started("op.a", corr_id="gone", t_start=BASE_T))

    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T + 10_000.0), "op.a")

    assert row["attempts"] == 4
    assert row["breach_rate"] == pytest.approx(0.25)


def test_trend_refuses_rather_than_guessing_on_thin_data():
    entries = [_complete("op.a", 9_000.0, t_start=BASE_T + i) for i in range(4)]
    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["trend"] == "insufficient_data"


def test_trend_reports_worsening_when_the_late_half_breaches_more():
    n = TREND_MIN_ATTEMPTS_PER_HALF
    entries = [_complete("op.a", 12.0, t_start=BASE_T + i) for i in range(n)]
    entries += [_complete("op.a", 9_000.0, t_start=BASE_T + n + i) for i in range(n)]

    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["trend"] == "worsening"


def test_trend_reports_improving_when_the_late_half_breaches_less():
    n = TREND_MIN_ATTEMPTS_PER_HALF
    entries = [_complete("op.a", 9_000.0, t_start=BASE_T + i) for i in range(n)]
    entries += [_complete("op.a", 12.0, t_start=BASE_T + n + i) for i in range(n)]

    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["trend"] == "improving"


def test_a_near_epoch_row_does_not_blind_the_trend_axis():
    """Regression: the live sink carries a handful of rows with a near-epoch
    `t_start`. Splitting the window at (first + last) / 2 put every real row
    in one half and darkened the trend for every op — measured 5 rows early
    against 46,411 late on 2026-08-21. The median split is immune, and the
    bad rows are counted rather than dropped."""
    n = TREND_MIN_ATTEMPTS_PER_HALF
    entries = [_complete("op.a", 12.0, t_start=BASE_T + i) for i in range(n)]
    entries += [_complete("op.a", 9_000.0, t_start=BASE_T + n + i) for i in range(n)]
    entries.append(_complete("op.a", 12.0, t_start=1.0))

    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T)

    assert _row_for(summary, "op.a")["trend"] == "worsening"
    assert summary["window"]["implausible_t_start_rows"] == 1


def test_malformed_rows_never_raise():
    entries = [
        "not a dict",
        {"kind": "complete"},
        {"kind": "complete", "op": "op.a", "elapsed_ms": "slow"},
        {"kind": "composition", "op": "op.a", "elapsed_secs": 12.0},
        _complete("op.a", 9_000.0),
    ]
    row = _row_for(breach_summary(entries, bar_ms=500.0, now=BASE_T), "op.a")

    assert row["over_bar"] == 1
    assert row["invocations"] == 2


def test_a_composition_row_is_not_an_op_invocation():
    """`kind: "composition"` is a whole composition's span, not one op's —
    counting it here would add a phantom invocation with a seconds-scaled
    cost."""
    entries = [{"kind": "composition", "op": "op.a", "name": "c", "elapsed_secs": 90.0}]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T)

    assert summary["totals"]["complete_rows"] == 0
    assert summary["ops"] == []


def test_a_row_with_no_kind_reads_as_complete():
    """`op_latency`'s backward-reading rule: rows written before the `kind`
    field existed carry none, and must never be read as `started`."""
    entries = [{"op": "op.a", "t_start": BASE_T, "elapsed_ms": 9_000.0, "outcome": "ok"}]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T)

    assert _row_for(summary, "op.a")["over_bar"] == 1
    assert summary["totals"]["vanished"] == 0


def test_breach_summary_does_not_mutate_its_input_rows():
    entries = [_complete("op.a", 9_000.0)]
    before = dict(entries[0])
    breach_summary(entries, bar_ms=500.0, now=BASE_T)

    assert entries[0] == before


# ---------------------------------------------------------------------------
# origin filtering -- the field exists so a census does not convict an op on
# harness traffic, and until 2026-08-26 this census read it not at all.
# ---------------------------------------------------------------------------


def _origin(row, origin):
    row = dict(row)
    row["origin"] = origin
    return row


def test_declared_benchmark_rows_do_not_convict_an_op():
    """The motivating case, at its real shape: a harness op that sleeps 12-20s
    BY DESIGN dominated `stolen_ms` and ranked worst on the whole box, ahead of
    an op genuinely breaching its budget."""
    entries = [_origin(_complete("sandbox.sleep", 20_000.0), "benchmark")]
    entries += [_complete("ceremony.commit", 2_008.0) for _ in range(28)]

    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T + 10_000.0)

    assert [r["op"] for r in summary["ops"]] == ["ceremony.commit"]
    assert summary["totals"]["breaching_ops"] == 1


def test_declared_test_rows_do_not_convict_an_op():
    """`invocation_origin` stamps TEST from a live pytest run, so every suite
    that dispatches would otherwise land in the census that convicts ops."""
    entries = [_origin(_complete("op.a", 30_000.0), "test")]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T + 10_000.0)
    assert summary["ops"] == []
    assert summary["totals"]["breaching_ops"] == 0


def test_absent_origin_still_counts():
    """Every row written before the field existed lacks the key. Dropping them
    would silently delete real traffic from the census meant to surface it --
    contaminated but visible beats clean but blind."""
    entries = [_complete("op.a", 30_000.0)]  # no "origin" key at all
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T + 10_000.0)
    assert _row_for(summary, "op.a")["breaches"] == 1


def test_declared_production_counts():
    entries = [_origin(_complete("op.a", 30_000.0), "production")]
    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T + 10_000.0)
    assert _row_for(summary, "op.a")["breaches"] == 1


def test_filtered_rows_leave_no_trace_in_totals():
    """A benchmark row must not inflate `attempts` either -- a breach RATE
    computed against a contaminated denominator is wrong in the other
    direction, and `attempts` is that denominator."""
    entries = [_origin(_complete("op.a", 100.0), "benchmark") for _ in range(50)]
    entries += [_complete("op.a", 30_000.0)]

    summary = breach_summary(entries, bar_ms=500.0, now=BASE_T + 10_000.0)

    row = _row_for(summary, "op.a")
    assert row["attempts"] == 1
    assert row["breach_rate"] == 1.0

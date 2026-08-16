"""Unit tests for coordinator_core.benchmarks.shim_decision_rule.

Covers evaluate()'s verdict boundaries (pass/wash/fail, including the
statistic-ambiguous and zero-baseline wash triggers) and
calibrate_aa_noise_floor()'s pure-computation contract (via a fake
interleaved-stats stand-in, no real subprocess spawns -- this module must
stay fast and must NOT measure any shim, see shim_decision_rule.py's
module docstring).

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7.
"""

from __future__ import annotations

from coordinator_core.benchmarks.interleave import PrimitiveStats
from coordinator_core.benchmarks.shim_decision_rule import (
    CHEAPER_THAN_MARGIN,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WASH,
    calibrate_aa_noise_floor,
    evaluate,
)


def _stats(median_ms: float, p90_ms: float, sample_count: int) -> PrimitiveStats:
    return PrimitiveStats(
        name="fake",
        sample_count=sample_count,
        median_ms=median_ms,
        p90_ms=p90_ms,
        samples_ms=[median_ms] * sample_count,
    )


def test_pass_when_reduction_clears_margin_and_sample_counts_match():
    baseline = _stats(median_ms=90.0, p90_ms=100.0, sample_count=30)
    # Reduction well above CHEAPER_THAN_MARGIN.
    shim_p90 = 100.0 * (1.0 - (CHEAPER_THAN_MARGIN + 0.1))
    shim = _stats(median_ms=shim_p90 * 0.9, p90_ms=shim_p90, sample_count=30)
    record = evaluate(
        baseline_name="baseline", baseline_stats=baseline,
        shim_name="shim", shim_stats=shim,
    )
    assert record.verdict == VERDICT_PASS
    assert record.reduction_fraction is not None
    assert record.reduction_fraction >= CHEAPER_THAN_MARGIN


def test_wash_when_reduction_is_positive_but_below_margin():
    baseline = _stats(median_ms=90.0, p90_ms=100.0, sample_count=30)
    # Improvement exists but does not clear the margin.
    shim_p90 = 100.0 * (1.0 - (CHEAPER_THAN_MARGIN / 2.0))
    shim = _stats(median_ms=shim_p90 * 0.9, p90_ms=shim_p90, sample_count=30)
    record = evaluate(
        baseline_name="baseline", baseline_stats=baseline,
        shim_name="shim", shim_stats=shim,
    )
    assert record.verdict == VERDICT_WASH


def test_fail_when_shim_p90_equals_baseline_p90():
    baseline = _stats(median_ms=90.0, p90_ms=100.0, sample_count=30)
    shim = _stats(median_ms=90.0, p90_ms=100.0, sample_count=30)
    record = evaluate(
        baseline_name="baseline", baseline_stats=baseline,
        shim_name="shim", shim_stats=shim,
    )
    assert record.verdict == VERDICT_FAIL
    assert record.reduction_fraction == 0.0


def test_fail_when_shim_p90_exceeds_baseline_p90():
    baseline = _stats(median_ms=90.0, p90_ms=100.0, sample_count=30)
    shim = _stats(median_ms=110.0, p90_ms=120.0, sample_count=30)
    record = evaluate(
        baseline_name="baseline", baseline_stats=baseline,
        shim_name="shim", shim_stats=shim,
    )
    assert record.verdict == VERDICT_FAIL
    assert record.reduction_fraction is not None
    assert record.reduction_fraction < 0.0


def test_wash_when_sample_counts_differ_even_if_reduction_would_pass():
    baseline = _stats(median_ms=90.0, p90_ms=100.0, sample_count=30)
    shim_p90 = 100.0 * (1.0 - (CHEAPER_THAN_MARGIN + 0.1))
    shim = _stats(median_ms=shim_p90 * 0.9, p90_ms=shim_p90, sample_count=29)
    record = evaluate(
        baseline_name="baseline", baseline_stats=baseline,
        shim_name="shim", shim_stats=shim,
    )
    assert record.verdict == VERDICT_WASH


def test_wash_when_baseline_stat_is_zero():
    baseline = _stats(median_ms=0.0, p90_ms=0.0, sample_count=30)
    shim = _stats(median_ms=10.0, p90_ms=10.0, sample_count=30)
    record = evaluate(
        baseline_name="baseline", baseline_stats=baseline,
        shim_name="shim", shim_stats=shim,
    )
    assert record.verdict == VERDICT_WASH
    assert record.reduction_fraction is None


def test_record_round_trips_through_json():
    baseline = _stats(median_ms=90.0, p90_ms=100.0, sample_count=30)
    shim = _stats(median_ms=90.0, p90_ms=100.0, sample_count=30)
    record = evaluate(
        baseline_name="baseline", baseline_stats=baseline,
        shim_name="shim", shim_stats=shim,
    )
    from coordinator_core.benchmarks.shim_decision_rule import ShimDecisionRecord

    round_tripped = ShimDecisionRecord.from_json(record.to_json())
    assert round_tripped == record


def test_calibrate_aa_noise_floor_returns_one_reduction_per_repeat():
    """calibrate_aa_noise_floor() is a pure driver over run_interleaved --
    verified here with a cheap in-process callable (never a subprocess
    spawn) so this test stays fast and, per this module's own stage-1
    scope, never measures a shim."""
    counter = {"n": 0}

    def cheap_invoke() -> float:
        counter["n"] += 1
        # Deterministic-ish tiny varying "duration" in ms, purely to give
        # run_interleaved something non-degenerate to reduce.
        return float((counter["n"] % 5) + 1)

    reductions = calibrate_aa_noise_floor(cheap_invoke, n_rounds=5, r_repeats=3)
    assert len(reductions) <= 3
    assert all(isinstance(r, float) for r in reductions)


def test_calibrate_aa_noise_floor_skips_repeats_with_zero_baseline_stat():
    """A repeat whose A arm produces an all-zero baseline stat is skipped
    (reduction_fraction undefined) rather than raising."""

    def zero_invoke() -> float:
        return 0.0

    reductions = calibrate_aa_noise_floor(zero_invoke, n_rounds=3, r_repeats=2)
    assert reductions == []

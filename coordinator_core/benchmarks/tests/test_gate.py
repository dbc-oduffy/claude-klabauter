"""Unit tests for coordinator_core.benchmarks.gate.evaluate().

Covers AC3's gate contract: advisory-below-N, relative vs absolute tolerance
bands, min-vs-target pass/fail boundaries, and the invariant that gate.py
never consumes a floor_delta (floor subtraction is out of scope for this
module — see gate.py's module docstring).

Spec backlink: docs/plans/2026-07-10-qsub-01-latency-benchmark-harness.md § C8 (AC3).
"""

from __future__ import annotations

import inspect

from coordinator_core.benchmarks.gate import (
    VERDICT_ADVISORY,
    VERDICT_FAIL,
    VERDICT_PASS,
    evaluate,
)
from coordinator_core.benchmarks.record import Tolerance


def test_advisory_when_sample_count_below_min_gating_sample_count():
    """An underpowered sample never gates green or red, even if the observed
    statistic would clearly pass or fail the band."""
    tolerance = Tolerance(kind="relative", value=0.2)
    verdict = evaluate(
        observed_statistic=10.0,
        target_ms=1000.0,
        tolerance=tolerance,
        sample_count=4,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_ADVISORY


def test_advisory_at_sample_count_equal_to_min_is_not_advisory():
    """sample_count == min_gating_sample_count is sufficient to gate (only
    strictly-below is advisory)."""
    tolerance = Tolerance(kind="relative", value=0.2)
    verdict = evaluate(
        observed_statistic=50.0,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=5,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_PASS


def test_relative_tolerance_band_pass_at_boundary():
    """band = target_ms * (1 + value); observed == band passes (<=)."""
    tolerance = Tolerance(kind="relative", value=0.2)
    verdict = evaluate(
        observed_statistic=120.0,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=10,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_PASS


def test_relative_tolerance_band_fail_just_above_boundary():
    tolerance = Tolerance(kind="relative", value=0.2)
    verdict = evaluate(
        observed_statistic=120.01,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=10,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_FAIL


def test_absolute_tolerance_band_pass_at_boundary():
    """band = target_ms + value; observed == band passes (<=)."""
    tolerance = Tolerance(kind="absolute", value=25.0)
    verdict = evaluate(
        observed_statistic=125.0,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=10,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_PASS


def test_absolute_tolerance_band_fail_just_above_boundary():
    tolerance = Tolerance(kind="absolute", value=25.0)
    verdict = evaluate(
        observed_statistic=125.01,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=10,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_FAIL


def test_min_vs_target_pass_well_under_band():
    tolerance = Tolerance(kind="relative", value=0.5)
    verdict = evaluate(
        observed_statistic=10.0,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=20,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_PASS


def test_min_vs_target_fail_well_over_band():
    tolerance = Tolerance(kind="relative", value=0.1)
    verdict = evaluate(
        observed_statistic=500.0,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=20,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_FAIL


def test_tolerance_accepts_dict_shape_not_only_dataclass():
    """gate.py is decoupled from record.py's concrete Tolerance type — a
    plain dict with kind/value keys must work identically."""
    tolerance = {"kind": "relative", "value": 0.2}
    verdict = evaluate(
        observed_statistic=100.0,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=10,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_PASS


def test_unknown_tolerance_kind_raises():
    tolerance = Tolerance(kind="quadratic", value=0.2)
    try:
        evaluate(
            observed_statistic=100.0,
            target_ms=100.0,
            tolerance=tolerance,
            sample_count=10,
            min_gating_sample_count=5,
        )
    except ValueError as exc:
        assert "quadratic" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown tolerance.kind")


def test_negative_floor_delta_never_changes_verdict():
    """gate.evaluate() takes no floor_delta parameter at all -- floor
    subtraction is categorically out of scope for this module (see gate.py's
    module docstring: "There is no floor subtraction anywhere in this
    module").

    Review: code-reviewer (Slice C F1, P2) -- the previous version of this
    test called evaluate() twice with IDENTICAL args and asserted the two
    results were equal, which proves only that evaluate() is deterministic
    (a fact true even if a latent bug consumed a floor value from module
    state or a global) -- it never exercised two distinguishable scenarios.
    The actually falsifiable claim is that evaluate()'s SIGNATURE has no slot
    for a floor-related parameter at all; assert that directly via
    inspect.signature, then separately assert the correct boundary verdict
    for a single real call.
    """
    signature = inspect.signature(evaluate)
    floor_related_params = {
        name for name in signature.parameters if "floor" in name.lower()
    }
    assert not floor_related_params, (
        f"evaluate() unexpectedly gained a floor-related parameter: "
        f"{floor_related_params!r} -- floor subtraction must stay out of gate.py"
    )

    tolerance = Tolerance(kind="relative", value=0.2)
    verdict = evaluate(
        observed_statistic=90.0,
        target_ms=100.0,
        tolerance=tolerance,
        sample_count=10,
        min_gating_sample_count=5,
    )
    assert verdict == VERDICT_PASS

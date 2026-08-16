"""Asserts the committed C7 stage-2 measurement record exists and is
internally consistent with the decision rule it was judged against.

Does NOT re-run the measurement (no subprocess spawns, stays fast) --
that is `shim_prototype_measure.py`'s job, run live once to produce the
committed `shim_decision_record.json`. This test only checks the
committed artifact's shape: it names the correct primitives, carries the
rule's own gating statistic/margin/N, and its `verdict` field is one of
the three values `evaluate()` can produce and is internally consistent
with its own `reduction_fraction`/`sample_count` fields per
`shim_decision_rule.evaluate()`'s documented rules.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7,
AC6.
"""

from __future__ import annotations

import os

from coordinator_core.benchmarks.shim_decision_rule import (
    BASELINE_PRIMITIVE_NAME,
    CHEAPER_THAN_MARGIN,
    GATING_STATISTIC,
    N_ROUNDS,
    SHIM_PRIMITIVE_NAME,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WASH,
    ShimDecisionRecord,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_RECORD_PATH = os.path.join(os.path.dirname(_HERE), "shim_decision_record.json")


def _load_record() -> ShimDecisionRecord:
    with open(_RECORD_PATH, "r", encoding="utf-8") as f:
        return ShimDecisionRecord.from_json(f.read())


def test_record_file_exists():
    assert os.path.isfile(_RECORD_PATH), (
        f"expected a committed C7 stage-2 measurement record at {_RECORD_PATH!r}"
    )


def test_record_matches_rule_constants():
    record = _load_record()
    assert record.gating_statistic == GATING_STATISTIC
    assert record.margin == CHEAPER_THAN_MARGIN
    assert record.n_rounds == N_ROUNDS
    assert record.baseline_name == BASELINE_PRIMITIVE_NAME
    assert record.shim_name == SHIM_PRIMITIVE_NAME


def test_record_verdict_is_one_of_the_three_permitted_values():
    record = _load_record()
    assert record.verdict in (VERDICT_PASS, VERDICT_WASH, VERDICT_FAIL)


def test_record_verdict_is_internally_consistent_with_its_own_fields():
    """Re-derives what `evaluate()` would have returned given the record's
    OWN carried baseline/shim stats and sample counts, and asserts it
    matches the carried `verdict` -- catches a hand-edited or stale
    record without re-running the measurement."""
    record = _load_record()
    sample_counts_match = record.baseline_sample_count == record.shim_sample_count

    if record.baseline_stat_ms == 0:
        expected_reduction = None
    else:
        expected_reduction = 1.0 - (record.shim_stat_ms / record.baseline_stat_ms)

    if expected_reduction is None or not sample_counts_match:
        expected_verdict = VERDICT_WASH
    elif expected_reduction <= 0.0:
        expected_verdict = VERDICT_FAIL
    elif expected_reduction < CHEAPER_THAN_MARGIN:
        expected_verdict = VERDICT_WASH
    else:
        expected_verdict = VERDICT_PASS

    assert record.verdict == expected_verdict
    assert record.reduction_fraction == expected_reduction

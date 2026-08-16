"""Asserts the committed C7 stage-2 measurement records exist and are
internally consistent with the decision rule they were judged against.

Does NOT re-run any measurement (no subprocess spawns, stays fast) -- that
is each `shim_*_measure.py` module's job, run live once to produce its
committed record. This test only checks each committed artifact's shape:
it names the correct primitives, carries the rule's own gating
statistic/margin/N, and its `verdict` field is one of the three values
`evaluate()` can produce and is internally consistent with its own
`reduction_fraction`/`sample_count` fields per
`shim_decision_rule.evaluate()`'s documented rules.

Three records, three measurements, all judged by the SAME unchanged
`shim_decision_rule.evaluate()`:
  - `shim_decision_record.json` -- forwarder+dispatcher subprocess shim vs.
    direct entry point (the original, uninvalidated FAIL record).
  - `shim_decision_record_inprocess.json` -- in-process shim (mirroring
    `exec_cli`'s Windows leg) vs. direct entry point (the corrected
    measurement of the shape C8 actually specs).
  - `shim_decision_record_fanin.json` -- N separate processes vs. one
    process evaluating the same N predicates (the plan's fan-in thesis,
    § Problem M-05).

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7,
AC6 (corrected 2nd measurement).
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
    AA_CALIBRATION_NOISE_FLOOR,
    ShimDecisionRecord,
    is_free,
)
from coordinator_core.benchmarks.shim_fanin_measure import (
    FAN_IN_ONE_PROCESS_PRIMITIVE_NAME,
    N_SEPARATE_PROCESSES_PRIMITIVE_NAME,
)
from coordinator_core.benchmarks.shim_fanin_measure import RECORD_PATH as _FANIN_RECORD_PATH
from coordinator_core.benchmarks.shim_inprocess_measure import (
    INPROCESS_SHIM_PRIMITIVE_NAME,
)
from coordinator_core.benchmarks.shim_inprocess_measure import (
    RECORD_PATH as _INPROCESS_RECORD_PATH,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_RECORD_PATH = os.path.join(os.path.dirname(_HERE), "shim_decision_record.json")


def _load_record(path: str) -> ShimDecisionRecord:
    with open(path, "r", encoding="utf-8") as f:
        return ShimDecisionRecord.from_json(f.read())


def _assert_verdict_internally_consistent(record: ShimDecisionRecord) -> None:
    """Re-derives what `evaluate()` would have returned given the record's
    OWN carried baseline/shim stats and sample counts, and asserts it
    matches the carried `verdict` -- catches a hand-edited or stale
    record without re-running the measurement."""
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


def test_record_file_exists():
    assert os.path.isfile(_RECORD_PATH), (
        f"expected a committed C7 stage-2 measurement record at {_RECORD_PATH!r}"
    )


def test_record_matches_rule_constants():
    record = _load_record(_RECORD_PATH)
    assert record.gating_statistic == GATING_STATISTIC
    assert record.margin == CHEAPER_THAN_MARGIN
    assert record.n_rounds == N_ROUNDS
    assert record.baseline_name == BASELINE_PRIMITIVE_NAME
    assert record.shim_name == SHIM_PRIMITIVE_NAME


def test_record_verdict_is_one_of_the_three_permitted_values():
    record = _load_record(_RECORD_PATH)
    assert record.verdict in (VERDICT_PASS, VERDICT_WASH, VERDICT_FAIL)


def test_record_verdict_is_internally_consistent_with_its_own_fields():
    _assert_verdict_internally_consistent(_load_record(_RECORD_PATH))


def test_inprocess_record_file_exists():
    assert os.path.isfile(_INPROCESS_RECORD_PATH), (
        f"expected the corrected in-process shim measurement record at "
        f"{_INPROCESS_RECORD_PATH!r}"
    )


def test_inprocess_record_matches_rule_constants():
    record = _load_record(_INPROCESS_RECORD_PATH)
    assert record.gating_statistic == GATING_STATISTIC
    assert record.margin == CHEAPER_THAN_MARGIN
    assert record.n_rounds == N_ROUNDS
    assert record.baseline_name == BASELINE_PRIMITIVE_NAME
    assert record.shim_name == INPROCESS_SHIM_PRIMITIVE_NAME


def test_inprocess_record_verdict_is_one_of_the_three_permitted_values():
    record = _load_record(_INPROCESS_RECORD_PATH)
    assert record.verdict in (VERDICT_PASS, VERDICT_WASH, VERDICT_FAIL)


def test_inprocess_record_verdict_is_internally_consistent_with_its_own_fields():
    _assert_verdict_internally_consistent(_load_record(_INPROCESS_RECORD_PATH))


def test_fanin_record_file_exists():
    assert os.path.isfile(_FANIN_RECORD_PATH), (
        f"expected the fan-in measurement record at {_FANIN_RECORD_PATH!r}"
    )


def test_fanin_record_matches_rule_constants():
    record = _load_record(_FANIN_RECORD_PATH)
    assert record.gating_statistic == GATING_STATISTIC
    assert record.margin == CHEAPER_THAN_MARGIN
    assert record.n_rounds == N_ROUNDS
    assert record.baseline_name == N_SEPARATE_PROCESSES_PRIMITIVE_NAME
    assert record.shim_name == FAN_IN_ONE_PROCESS_PRIMITIVE_NAME


def test_fanin_record_verdict_is_one_of_the_three_permitted_values():
    record = _load_record(_FANIN_RECORD_PATH)
    assert record.verdict in (VERDICT_PASS, VERDICT_WASH, VERDICT_FAIL)


def test_fanin_record_verdict_is_internally_consistent_with_its_own_fields():
    _assert_verdict_internally_consistent(_load_record(_FANIN_RECORD_PATH))


# --- AC6a / AC6b / AC6c discharge, docs/plans/2026-08-16-a-process-per-predicate.md ---
#
# Three records, three DIFFERENT questions. The plan's original AC6 asked one
# question ("is the shim cheaper?") of two things that need different ones,
# and demanded a 69% reduction from a backward-compatibility layer -- which
# nothing correct could ever deliver. These three pin what each record is for,
# so a later reader cannot silently re-merge them.


def test_ac6a_fan_in_is_cheaper_than_n_processes():
    """The WIN. Batched dispatch must beat N separate processes by the rule."""
    record = _load_record(_FANIN_RECORD_PATH)
    assert record.verdict == VERDICT_PASS, (
        f"fan-in must clear the {CHEAPER_THAN_MARGIN} margin; got "
        f"{record.reduction_fraction} ({record.verdict})"
    )


def test_ac6b_compat_shim_is_free_not_cheaper():
    """The COMPAT LAYER. Its job is to cost nothing, never to be cheaper --
    so it is judged by `is_free` (symmetric, noise-floor bounded), NOT by
    `evaluate`'s one-sided cheaper-than verdict. `evaluate` returns `fail`
    here and is right about its own question; it is simply not the question
    AC6b asks."""
    record = _load_record(_INPROCESS_RECORD_PATH)
    assert is_free(record), (
        f"in-process shim must be indistinguishable from free at this box's "
        f"noise floor ({AA_CALIBRATION_NOISE_FLOOR}); got "
        f"reduction={record.reduction_fraction}"
    )


def test_ac6c_spawning_forwarder_is_rejected_on_evidence():
    """The NEGATIVE. A forwarder that spawns a child pays two process starts
    where the current path pays one, and the record says so -- so the shape
    is rejected on measurement rather than on taste."""
    record = _load_record(_RECORD_PATH)
    assert record.verdict == VERDICT_FAIL
    assert not is_free(record), (
        "a spawning forwarder's regression must be LARGER than the noise "
        "floor, otherwise this record cannot carry the rejection"
    )

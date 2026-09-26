
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


# Three records, three DIFFERENT questions. The plan's original AC6 asked one


def test_ac6a_fan_in_is_cheaper_than_n_processes():
    record = _load_record(_FANIN_RECORD_PATH)
    assert record.verdict == VERDICT_PASS, (
        f"fan-in must clear the {CHEAPER_THAN_MARGIN} margin; got "
        f"{record.reduction_fraction} ({record.verdict})"
    )


def test_ac6b_compat_shim_is_free_not_cheaper():
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

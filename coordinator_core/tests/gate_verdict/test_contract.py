"""Unit tests for the gate-verdict contract's disagreement()."""

from coordinator_core.tests.gate_verdict.contract import (
    GateExpectation,
    GateObservation,
    VerdictClass,
    disagreement,
)


def _obs(exit_code=0, verdict="ok", **mag):
    return GateObservation(exit_code, verdict, mag)


def _exp(cls=VerdictClass.CLEAN, verdict="ok", **mag):
    return GateExpectation(verdict, cls, mag)


def test_clean_exit_zero_agrees():
    assert disagreement("g", _obs(loc=3), _exp(loc=3)) is None


def test_advisory_exit_zero_agrees():
    assert disagreement("g", _obs(), _exp(VerdictClass.ADVISORY)) is None


def test_advisory_with_exit_one_disagrees():
    msg = disagreement("g", _obs(exit_code=1), _exp(VerdictClass.ADVISORY))
    assert msg is not None and "exit_code: observed=1 expected=0" in msg
    assert "\n" not in msg and msg.startswith("g:")


def test_halt_exit_nonzero_agrees():
    assert disagreement("g", _obs(exit_code=2), _exp(VerdictClass.HALT)) is None


def test_unmeasured_with_exit_zero_disagrees():
    msg = disagreement("g", _obs(exit_code=0, verdict=None), _exp(VerdictClass.UNMEASURED, None))
    assert msg is not None and "exit_code" in msg and "verdict" not in msg


def test_verdict_token_disagrees():
    msg = disagreement("g", _obs(verdict="bad"), _exp())
    assert msg is not None and "verdict: observed=bad expected=ok" in msg
    assert "exit_code" not in msg


def test_magnitude_disagrees():
    msg = disagreement("g", _obs(loc=4), _exp(loc=3))
    assert msg is not None and "loc: observed=4 expected=3" in msg


def test_missing_magnitude_key_disagrees():
    msg = disagreement("g", _obs(), _exp(loc=3))
    assert msg is not None and "loc: observed=None expected=3" in msg


def test_extra_observed_key_ignored():
    assert disagreement("g", _obs(loc=3, commits=9), _exp(loc=3)) is None


def test_all_axes_reported_in_one_line():
    msg = disagreement("g", _obs(exit_code=1, verdict="bad", loc=4), _exp(loc=3))
    assert msg is not None and "\n" not in msg
    assert msg.count("observed=") == 3

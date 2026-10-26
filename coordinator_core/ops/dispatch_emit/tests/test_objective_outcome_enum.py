"""objective_loop.outcome is exactly met|impossible|doctrine_conflict|exhausted; exhausted re-enters the loop."""

from coordinator_core.ops.dispatch_emit import objective_contract as oc

_RETIRED = ("OUTCOME_PASSED_FIRST_TIME", "OUTCOME_SAVED_BY_LOOP", "OUTCOME_ESCALATED", "OUTCOME_CLOSED_PARTIAL")


def _check(axes_pass):
    return {"kind": oc.LINE_CHECK, "wave": 1, "iteration": 1, "axes": [{"axis": k, "pass": v} for k, v in axes_pass.items()]}


def test_enum_is_exactly_the_four_members():
    assert set(oc.OUTCOMES) == {"met", "impossible", "doctrine_conflict", "exhausted"}
    assert len(oc.OUTCOMES) == 4
    assert not [n for n in _RETIRED if hasattr(oc, n)]


def test_only_three_are_terminal_and_exhausted_reenters():
    assert set(oc.TERMINAL_OUTCOMES) == {"met", "impossible", "doctrine_conflict"}
    assert oc.is_terminal(oc.OUTCOME_EXHAUSTED) is False
    assert all(oc.is_terminal(o) for o in oc.TERMINAL_OUTCOMES)
    unmet = oc.objective_loop_result([_check({"a": False})])
    assert unmet["outcome"] == oc.OUTCOME_EXHAUSTED and not oc.is_terminal(unmet["outcome"])


def test_unknown_outcome_refused():
    for bad in ("closed_partial", "escalated", "saved_by_loop", "passed_first_time"):
        try:
            oc.objective_loop_result([_check({"a": False})], outcome=bad)
        except ValueError:
            continue
        raise AssertionError(bad)

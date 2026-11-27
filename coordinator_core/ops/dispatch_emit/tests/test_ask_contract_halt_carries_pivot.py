"""`halt_carries_pivot` is True only for a PIVOT halt record, and mirrors the PIVOT arm of the pull predicate."""
import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_PIVOT_FIELD, halt_carries_pivot


def _pull(*reviews, key="reviews"):
    return {key: list(reviews)}


def _rev(verdict, reviewer="staff-eng"):
    return {"reviewer": reviewer, "verdict": verdict}


def test_field_name():
    assert RESUME_LEDGER_PIVOT_FIELD == "pivot"


@pytest.mark.parametrize("verdict", ["PIVOT", "pivot", "Pivot"])
def test_blitz_pulled_pivot_is_true(verdict):
    assert halt_carries_pivot({"blitz": {"pulled": [_pull(_rev(verdict))]}}) is True


def test_regate_pulled_pivot_is_true():
    assert halt_carries_pivot({"blitz": {"regate": {"pulled": [_pull(_rev("PIVOT"))]}}}) is True


def test_review_verdicts_alias_is_read():
    assert halt_carries_pivot({"blitz": {"pulled": [_pull(_rev("PIVOT"), key="reviewVerdicts")]}}) is True


def test_reviews_wins_over_review_verdicts():
    entry = {"reviews": [_rev("OK")], "reviewVerdicts": [_rev("PIVOT")]}
    assert halt_carries_pivot({"blitz": {"pulled": [entry]}}) is False


@pytest.mark.parametrize("verdict", ["OK", "BLOCKED", "ABORT", "REJECTED", "", None])
def test_other_verdicts_are_false(verdict):
    assert halt_carries_pivot({"blitz": {"pulled": [_pull(_rev(verdict))]}}) is False


def test_premise_check_pivot_is_false():
    assert halt_carries_pivot({"blitz": {"pulled": [_pull(_rev("PIVOT", reviewer="premise-check"))]}}) is False


def test_top_level_marker_true():
    assert halt_carries_pivot({RESUME_LEDGER_PIVOT_FIELD: True}) is True


@pytest.mark.parametrize("value", [False, None, "true", 1, "yes"])
def test_top_level_marker_must_be_true(value):
    assert halt_carries_pivot({RESUME_LEDGER_PIVOT_FIELD: value}) is False


@pytest.mark.parametrize(
    "halted",
    [{}, {"blitz": None}, {"blitz": {}}, {"blitz": {"pulled": "x"}}, {"blitz": {"pulled": [None, 3, {"reviews": "PIVOT"}]}},
     {"blitz": {"regate": None}}, {"blitz": {"pulled": [{"reviews": [None, "PIVOT"]}]}}, None, [], "PIVOT"],
)
def test_malformed_shapes_are_false(halted):
    assert halt_carries_pivot(halted) is False

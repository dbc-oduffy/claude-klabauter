"""`halt_carries_pivot` is True only for a PIVOT halt record, and mirrors the PIVOT arm of the pull predicate."""
import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import _BLITZ_PIVOT_PULLED_JS
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


_PARITY_BLITZES = [
    {"pulled": [_pull(_rev("PIVOT"))]},
    {"pulled": [_pull(_rev("pivot"))]},
    {"regate": {"pulled": [_pull(_rev("PIVOT"))]}},
    {"pulled": [_pull(_rev("PIVOT"), key="reviewVerdicts")]},
    {"pulled": [{"reviews": [_rev("OK")], "reviewVerdicts": [_rev("PIVOT")]}]},
    {"pulled": [{"reviews": None, "reviewVerdicts": [_rev("PIVOT")]}]},
    {"pulled": [_pull(_rev("OK"), _rev("PIVOT", reviewer="x"))]},
    {"pulled": [_pull(_rev("PIVOT", reviewer="premise-check"))]},
    {"pulled": [_pull(_rev("OK")), _pull(_rev("PIVOT"))]},
    {"pulled": [_pull(_rev("ABORT"))], "regate": {"pulled": [_pull(_rev("PIVOT"))]}},
    {"pulled": [_pull(_rev("BLOCKED"))]},
    {"pulled": [_pull(_rev("ABORT"))]},
    {"pulled": [_pull(_rev(None))]},
    {"pulled": [_pull(_rev(""))]},
    {"pulled": [None, 3, {"reviews": "PIVOT"}]},
    {"pulled": [{"reviews": [None, "PIVOT"]}]},
    {"pulled": []},
    {"regate": {}},
    {},
]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_python_predicate_and_the_compose_js_twin_agree_on_the_same_halt_records():
    js = (
        f"const cases = {json.dumps(_PARITY_BLITZES)};"
        f"console.log(JSON.stringify(cases.map((_blitz) => {_BLITZ_PIVOT_PULLED_JS})));"
    )
    out = subprocess.run(
        ["node", "-"], input=js, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    js_verdicts = json.loads(out)
    py_verdicts = [halt_carries_pivot({"blitz": b}) for b in _PARITY_BLITZES]
    assert js_verdicts == py_verdicts
    assert True in py_verdicts and False in py_verdicts

"""The composed JS pivot marker and `halt_carries_pivot` give the same verdict for every halt record in one table."""
import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import _PIVOT_HALT_SPREAD_JS
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_PIVOT_FIELD, halt_carries_pivot


def _pull(*reviews, key="reviews"):
    return {key: list(reviews)}


def _rev(verdict, reviewer="staff-eng", **extra):
    return {"reviewer": reviewer, "verdict": verdict, **extra}


def _blitz(**kw):
    return {"blitz": kw}


_HALT_TABLE = {
    "pivot-in-pulled": _blitz(pulled=[_pull(_rev("PIVOT"))]),
    "pivot-in-regate-pulled": _blitz(regate={"pulled": [_pull(_rev("PIVOT"))]}),
    "review-verdicts-spelling": _blitz(pulled=[_pull(_rev("PIVOT"), key="reviewVerdicts")]),
    "lower-case-pivot": _blitz(pulled=[_pull(_rev("pivot"))]),
    "mixed-case-pivot": _blitz(pulled=[_pull(_rev("Pivot"))]),
    "pivot-after-ok-review": _blitz(pulled=[_pull(_rev("OK"), _rev("PIVOT", reviewer="x"))]),
    "pivot-in-second-pull": _blitz(pulled=[_pull(_rev("OK")), _pull(_rev("PIVOT"))]),
    "abort-pulled-pivot-regate": _blitz(pulled=[_pull(_rev("ABORT"))], regate={"pulled": [_pull(_rev("PIVOT"))]}),
    "reviews-wins-over-review-verdicts": _blitz(pulled=[{"reviews": [_rev("OK")], "reviewVerdicts": [_rev("PIVOT")]}]),
    "null-reviews-falls-to-review-verdicts": _blitz(pulled=[{"reviews": None, "reviewVerdicts": [_rev("PIVOT")]}]),
    "premise-check-pivot": _blitz(pulled=[_pull(_rev("PIVOT", reviewer="premise-check"))]),
    "ok": _blitz(pulled=[_pull(_rev("OK"))]),
    "blocked": _blitz(pulled=[_pull(_rev("BLOCKED"))]),
    "abort": _blitz(pulled=[_pull(_rev("ABORT"))]),
    "rejected-with-premise-failure": _blitz(pulled=[_pull(_rev("REJECTED", premiseFailure=["p1"]))]),
    "null-verdict": _blitz(pulled=[_pull(_rev(None))]),
    "empty-verdict": _blitz(pulled=[_pull(_rev(""))]),
    "malformed-pulled-entries": _blitz(pulled=[None, 3, {"reviews": "PIVOT"}]),
    "malformed-review-entries": _blitz(pulled=[{"reviews": [None, "PIVOT"]}]),
    "empty-pulled": _blitz(pulled=[]),
    "empty-regate": _blitz(regate={}),
    "null-blitz": {"blitz": None},
    "top-level-pivot-true": {RESUME_LEDGER_PIVOT_FIELD: True},
    "top-level-pivot-true-over-ok": {RESUME_LEDGER_PIVOT_FIELD: True, "blitz": {"pulled": [_pull(_rev("OK"))]}},
    "top-level-pivot-false": {RESUME_LEDGER_PIVOT_FIELD: False},
    "top-level-pivot-string": {RESUME_LEDGER_PIVOT_FIELD: "true"},
    "empty-record": {},
}

_EXPECTED_TRUE = {
    "pivot-in-pulled", "pivot-in-regate-pulled", "review-verdicts-spelling", "lower-case-pivot", "mixed-case-pivot",
    "pivot-after-ok-review", "pivot-in-second-pull", "abort-pulled-pivot-regate", "null-reviews-falls-to-review-verdicts",
    "top-level-pivot-true", "top-level-pivot-true-over-ok",
}


def _js_verdicts(records):
    js = (
        f"const cases = {json.dumps(records)};"
        "const verdict = (rec) => { const _blitz = rec.blitz; "
        f"const out = {{ blitz: _blitz, {_PIVOT_HALT_SPREAD_JS}}}; "
        f"return rec.{RESUME_LEDGER_PIVOT_FIELD} === true || out.{RESUME_LEDGER_PIVOT_FIELD} === true; }};"
        "console.log(JSON.stringify(cases.map(verdict)));"
    )
    out = subprocess.run(
        ["node", "-"], input=js, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    return json.loads(out)


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH: the JS predicate cannot be evaluated")
def test_js_pivot_marker_and_halt_carries_pivot_agree_on_every_halt_record():
    names = list(_HALT_TABLE)
    records = [_HALT_TABLE[n] for n in names]
    js = dict(zip(names, _js_verdicts(records)))
    py = {n: halt_carries_pivot(_HALT_TABLE[n]) for n in names}
    assert js == py
    assert {n for n, v in py.items() if v} == _EXPECTED_TRUE

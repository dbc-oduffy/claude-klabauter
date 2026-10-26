"""Pure tests for the remnant interface in ledger_recovery: constants, remnant_items, JS mirror, prompt."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.win_portability import no_console_creationflags


def _row(rid, **kw):
    return {"id": rid, "status": "not_met", "remaining": [f"{rid} gap"], **kw}


def test_constants_do_not_collide_with_the_one_verify_leg():
    assert lr.REMNANT_LABEL == "remnant-complete"
    assert lr.REMNANT_ATTEMPT_CAP == 1
    assert lr.REMNANT_REVERIFY_LABEL not in ("verify", lr.LEDGER_REVERIFY_LABEL)
    assert lr.REMNANT_PHASE


def test_all_clear_returns_empty():
    items = [_row("R1", status="met")]
    assert lr.remnant_items(items, [], [], {"status": "met"}, {"verdict": "PASS"}) == []
    assert lr.remnant_items(None, None, None, None, None) == []


def test_unattributed_and_capped_rows_carry_remaining():
    items = [_row("R1"), _row("R2"), _row("R3")]
    out = lr.remnant_items(items, ["R1"], ["R2"], None, None)
    assert [(i["kind"], i["id"], i["gap"]) for i in out] == [("ledger", "R1", "R1 gap"), ("ledger", "R2", "R2 gap")]


def test_withheld_owed_and_nonessential_rows_are_excluded():
    items = [_row("R1", withheld_by=["C2"]), _row("R2", owed_by=["em"]), _row("R3", essential=False), _row("R4", owed_by=[])]
    out = lr.remnant_items(items, ["R1", "R2", "R3", "R4"], [], None, None)
    assert [i["id"] for i in out] == ["R4"]


def test_criterion_prefers_remnant_object_and_falls_back_to_reason():
    crit = {"status": "not_met", "reason": "text", "remnant": {"gap": "g", "paths": ["./a/b.py"], "checks": ["pytest a"]}}
    out = lr.remnant_items([], [], [], crit, None)
    assert out == [{"kind": "criterion", "id": "criterion", "gap": "g", "paths": ["a/b.py"], "checks": ["pytest a"]}]
    out = lr.remnant_items([], [], [], {"status": "not_met", "reason": "text"}, None)
    assert out[0]["gap"] == "text" and out[0]["paths"] == []


def test_delivery_fail_only():
    assert lr.remnant_items([], [], [], None, {"verdict": "PASS"}) == []
    out = lr.remnant_items([], [], [], None, {"verdict": "FAIL", "reason": "DELIVERY.md absent"})
    assert out[0]["kind"] == "delivery" and out[0]["gap"] == "DELIVERY.md absent"


def test_prompt_splices_both_expressions():
    p = lr.remnant_prompt("_items", "_fp")
    assert "${_items}" in p and "${_fp}" in p


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    return node


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_js_mirror_matches_python():
    node = _node()
    items = [_row("R1"), _row("R2", withheld_by=["C2"]), _row("R3", owed_by=[]), _row("R4", owed_by=["em"])]
    crit = {"status": "not_met", "reason": "t", "remnant": {"gap": "g", "paths": ["./x.py"], "checks": ["c"]}}
    deliv = {"verdict": "FAIL", "summary": "s"}
    args = (items, ["R1", "R2", "R3", "R4"], ["R1"], crit, deliv)
    program = lr.REMNANT_ITEMS_JS + "\nprocess.stdout.write(JSON.stringify(_remnantItems(%s)));" % ", ".join(json.dumps(a) for a in args)
    out = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == lr.remnant_items(*args)

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


def test_constants_do_not_collide_with_the_one_verify_leg() -> None:
    assert lr.REMNANT_LABEL == "remnant-complete"
    assert lr.REMNANT_ATTEMPT_CAP == 1
    assert lr.REMNANT_REVERIFY_LABEL not in ("verify", lr.LEDGER_REVERIFY_LABEL)
    assert lr.REMNANT_PHASE


def test_all_clear_returns_empty() -> None:
    items = [_row("R1", status="met")]
    assert lr.remnant_items(items, [], [], {"status": "met"}, {"verdict": "PASS"}) == []
    assert lr.remnant_items(None, None, None, None, None) == []


def test_unattributed_and_capped_rows_carry_remaining() -> None:
    items = [_row("R1"), _row("R2"), _row("R3")]
    out = lr.remnant_items(items, ["R1"], ["R2"], None, None)
    assert [(i["kind"], i["id"], i["gap"]) for i in out] == [("ledger", "R1", "R1 gap"), ("ledger", "R2", "R2 gap")]


def test_withheld_owed_and_nonessential_rows_are_excluded() -> None:
    items = [_row("R1", withheld_by=["C2"]), _row("R2", owed_by=["em"]), _row("R3", essential=False), _row("R4", owed_by=[])]
    out = lr.remnant_items(items, ["R1", "R2", "R3", "R4"], [], None, None)
    assert [i["id"] for i in out] == ["R4"]


_HELD = [{"id": "C4", "register_rows": ["R9"]}]


def test_items_owned_by_an_em_run_or_needs_slot_row_are_excluded_capped_or_not() -> None:
    items = [_row("R1", remaining=["C4 install still to run"]), _row("R9"), _row("R2", evidence="see C40"), _row("R3")]
    ids = ["R1", "R9", "R2", "R3"]
    assert [i["id"] for i in lr.remnant_items(items, ids, [], None, None, _HELD)] == ["R2", "R3"]
    assert [i["id"] for i in lr.remnant_items(items, [], ids, None, None, _HELD)] == ["R2", "R3"]
    assert [i["id"] for i in lr.remnant_items(items, ids, [], None, None)] == ids
    assert lr.remnant_items([_row("R1", remaining=["C4 install"])], ["R1"], [], None, None, _HELD) == []


def test_prompt_forbids_em_run_and_slot_work_and_returns_blocked() -> None:
    p = lr.remnant_prompt("_items", "_fp")
    assert "em_run" in p and "needs_slot" in p and "UBT" in p and "BLOCKED" in p


def test_criterion_prefers_remnant_object_and_falls_back_to_reason() -> None:
    crit = {"status": "not_met", "reason": "text", "remnant": {"gap": "g", "paths": ["./a/b.py"], "checks": ["pytest a"]}}
    out = lr.remnant_items([], [], [], crit, None)
    assert out == [{"kind": "criterion", "id": "criterion", "gap": "g", "paths": ["a/b.py"], "checks": ["pytest a"]}]
    out = lr.remnant_items([], [], [], {"status": "not_met", "reason": "text"}, None)
    assert out[0]["gap"] == "text" and out[0]["paths"] == []


def test_delivery_fail_only() -> None:
    assert lr.remnant_items([], [], [], None, {"verdict": "PASS"}) == []
    out = lr.remnant_items([], [], [], None, {"verdict": "FAIL", "reason": "DELIVERY.md absent"})
    assert out[0]["kind"] == "delivery" and out[0]["gap"] == "DELIVERY.md absent"


def test_prompt_splices_both_expressions() -> None:
    p = lr.remnant_prompt("_items", "_fp")
    assert "${_items}" in p and "${_fp}" in p


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    return node


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_js_mirror_matches_python() -> None:
    node = _node()
    items = [_row("R1"), _row("R2", withheld_by=["C2"]), _row("R3", owed_by=[]), _row("R4", owed_by=["em"])]
    crit = {"status": "not_met", "reason": "t", "remnant": {"gap": "g", "paths": ["./x.py"], "checks": ["c"]}}
    deliv = {"verdict": "FAIL", "summary": "s"}
    items += [_row("R5", remaining=["C4 install"]), _row("R6"), _row("R9"), _row("R7", evidence="see C40")]
    args = (items, ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R9"], ["R1", "R9"], crit, deliv, _HELD)
    program = lr.REMNANT_ITEMS_JS + "\nprocess.stdout.write(JSON.stringify(_remnantItems(%s)));" % ", ".join(json.dumps(a) for a in args)
    out = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == lr.remnant_items(*args)

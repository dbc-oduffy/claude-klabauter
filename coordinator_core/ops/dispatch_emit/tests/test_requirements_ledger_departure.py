"""judge(): the `departed` status and the evidence rule for a PM-sourced `met`."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.requirements_ledger import Item, Ledger

EVIDENCE_MSG = "met without evidence: name met_by or evidence, or judge it departed with a reason"


def _ledger(*items: Item) -> Ledger:
    return Ledger(items=items, all_of_pm_ask=False, sizing_rel="state/sizings/x.yaml")


def _item(rid: str, origin: str, anchor: str) -> Item:
    return Item(rid, "words", origin, "", anchor=anchor)


PM = _item("R1", rl.ORIGIN_PM_VERBATIMS, "pm_verbatims")
DESIGN = _item("R2", "", "design-registry:board-1")
PLAN_ROW = _item("R3", rl.ORIGIN_PLAN_ROW, "plan-row:C1")


def _judge(ledger: Ledger, **row) -> dict:
    return rl.judge(ledger, {"items": [{"id": ledger.items[0].id, **row}]}, repo_root=None)


def test_departed_with_reason_passes_and_is_listed_with_it() -> None:
    out = _judge(_ledger(PM), status="departed", departure_reason="  the PM's bar contradicts DR-344  ")
    assert out["pass"] is True
    assert out["departures"] == [{"id": "R1", "reason": "the PM's bar contradicts DR-344"}]
    assert out["open"] == [{"id": "R1", "reason": "departed: the PM's bar contradicts DR-344", "departed": True}]


def test_departed_without_a_reason_is_refused() -> None:
    for extra in ({}, {"departure_reason": ""}, {"departure_reason": "   "}, {"departure_reason": 7}):
        out = _judge(_ledger(PM), status="departed", **extra)
        assert out["pass"] is False
        assert out["failures"] == [{"id": "R1", "reason": "departed without a recorded reason"}]
        assert "departures" not in out


def test_a_design_row_cannot_depart() -> None:
    out = _judge(_ledger(DESIGN), status="departed", departure_reason="because")
    assert out["pass"] is False and "departures" not in out


def test_no_departure_leaves_the_departures_key_absent() -> None:
    assert "departures" not in _judge(_ledger(PM), status="met", met_by="a.py")


def test_pm_sourced_met_needs_met_by_or_evidence() -> None:
    for origin in rl.PM_SOURCED_ORIGINS:
        anchor = origin if origin != rl.ORIGIN_REVISION else "revision@2026-11-22T00:00:00Z"
        item = _item("R1", origin, anchor)
        refused = _judge(_ledger(item), status="met")
        assert refused["failures"] == [{"id": "R1", "reason": EVIDENCE_MSG}], origin
        assert _judge(_ledger(item), status="met", met_by="a.py")["pass"] is True
        assert _judge(_ledger(item), status="met", met_by=["a.py"])["pass"] is True
        assert _judge(_ledger(item), status="met", evidence="a.py does it")["pass"] is True
        assert _judge(_ledger(item), status="met", met_by=[" "], evidence=" ")["pass"] is False


def test_non_pm_origin_met_needs_no_evidence() -> None:
    assert _judge(_ledger(PLAN_ROW), status="met")["pass"] is True


def test_departed_is_in_the_checker_schema() -> None:
    from coordinator_core.ops.dispatch_emit import ask_compose

    props = ask_compose._LEDGER_CHECK_SCHEMA["properties"]["items"]["items"]["properties"]
    assert "departed" in props["status"]["enum"]
    assert rl.DEPARTURE_REASON_KEY in props

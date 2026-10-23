"""pending_ruling_ids: the J<n> ruling ids the ledger still owes; GateVerdict.pending_rulings wire shape."""
from __future__ import annotations

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit.ask_contract import GateVerdict
from coordinator_core.ops.dispatch_emit.requirements_ledger import pending_ruling_ids


def _row(anchor: str, text: str, status: str = rr.STATUS_OPEN) -> dict:
    return {rr.ROW_ANCHOR: anchor, rr.ROW_TEXT: text, rr.ROW_STATUS: status}


REV = "revision@2026-10-23T00:00:00Z"


def test_revision_row_naming_ruling():
    assert pending_ruling_ids([_row(REV, "J1: use sonnet")]) == ("J1",)


def test_revision_without_ruling_id_contributes_nothing():
    assert pending_ruling_ids([_row(REV, "also handle the edge case")]) == ()


def test_non_revision_origins_ignored():
    rows = [_row("pm_verbatims", "J2 do it"), _row("ask", "J2 do it")]
    assert pending_ruling_ids(rows) == ()


def test_met_deferred_waived_revision_ignored():
    rows = [_row(REV, "J1 x", s) for s in (rr.STATUS_MET, rr.STATUS_DEFERRED, rr.STATUS_WAIVED)]
    assert pending_ruling_ids(rows) == ()


def test_partial_revision_counts():
    assert pending_ruling_ids([_row(REV, "J4 x", rr.STATUS_PARTIAL)]) == ("J4",)


def test_order_and_dedupe():
    rows = [_row(REV, "J1 first"), _row(REV, "J3 then J1"), _row(REV, "J1 again")]
    assert pending_ruling_ids(rows) == ("J1", "J3")


def test_word_boundary():
    assert pending_ruling_ids([_row(REV, "XJ1 and J12x and J12")]) == ("J12",)


def test_gate_verdict_wire_shape():
    assert "pending_rulings" not in GateVerdict(arm=None, halt=None).to_json()
    assert GateVerdict(arm=None, halt=None, pending_rulings=[]).to_json()["pending_rulings"] == []
    assert GateVerdict(arm=None, halt=None, pending_rulings=["J1"]).to_json()["pending_rulings"] == ["J1"]

"""`_resume_ledger` carries the ledger's item/withheld split from the live (post-revise) sizing."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger

OUT_OF_SCOPE = "workflow.revise:ask-20990101T000000-aaaaaa:out_of_scope"


def _row(rid: str, status: str, ref: str | None = None) -> dict:
    row = {"id": rid, "source_text": rid, "anchor": "ask", "surface": "internal", "status": status}
    if ref:
        row["ruling"] = {"source": "pm", "quote": "q", "ref": ref}
    return row


def _halt() -> LedgerHalt:
    return LedgerHalt(run_id="r", plan_rel="p", sizing_rel="s", blocking=("R1",))


def test_revise_after_halt_moves_the_row_to_withheld(tmp_path):
    before = _resume_ledger({"requirement_register": {"rows": [_row("R1", "open")]}}, _halt(), tmp_path)
    assert (before["ledger_item_ids"], before["withheld_ids"]) == (["R1"], [])
    live = {"requirement_register": {"rows": [_row("R1", "deferred", OUT_OF_SCOPE)]}}
    after = _resume_ledger(live, _halt(), tmp_path)
    assert after["ledger_item_ids"] == []
    assert after["withheld_ids"] == ["R1"]
    assert after["blocking"] == []


def test_mixed_register_splits_in_register_order(tmp_path):
    rows = [_row("R1", "deferred", OUT_OF_SCOPE), _row("R2", "open"), _row("R3", "deferred")]
    reply = _resume_ledger({"requirement_register": {"rows": rows}}, _halt(), tmp_path)
    # A plain-deferred row (no revise ruling) is neither judged nor withheld, as in `_ledger_of`.
    assert reply["ledger_item_ids"] == ["R2"]
    assert reply["withheld_ids"] == ["R1"]

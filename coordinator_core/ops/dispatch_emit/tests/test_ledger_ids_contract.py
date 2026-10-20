"""`ledger_ids` splits the register like `_ledger_of`; `StageManifest` carries the split through JSON."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest
from coordinator_core.ops.dispatch_emit.requirements_ledger import _ledger_of, ledger_ids

REVISE = "workflow.revise:ask-20990101T000000-aaaaaa:out_of_scope"


def _row(rid: str, status: str, ref: str | None = None) -> dict:
    row = {"id": rid, "source_text": rid, "anchor": "ask", "surface": "internal", "status": status}
    if ref:
        row["ruling"] = {"source": "pm", "quote": "q", "ref": ref}
    return row


ROWS = [
    _row("R1", "deferred", REVISE),
    _row("R2", "open"),
    _row("R3", "deferred"),
    _row("R4", "met"),
    _row("R5", "waived", REVISE),
]


def _manifest(**kw) -> StageManifest:
    return StageManifest(run_dir="d", rows=(), review_declared_paths=(), marker_path="m", **kw)


def test_ledger_ids_equals_ledger_of_split():
    ledger = _ledger_of(ROWS, False, None)
    items, withheld = ledger_ids(ROWS)
    assert items == tuple(i.id for i in ledger.items) == ("R2", "R4")
    assert withheld == tuple(w["id"] for w in ledger.withheld) == ("R1",)


def test_all_ruled_out_register_has_no_items():
    rows = [_row("R1", "deferred", REVISE)]
    assert ledger_ids(rows) == ((), ("R1",))


def test_manifest_round_trips_both_fields():
    m = _manifest(ledger_item_ids=("R2",), withheld_ids=("R1",))
    data = m.to_json()
    assert data["ledger_item_ids"] == ["R2"] and data["withheld_ids"] == ["R1"]
    back = StageManifest.from_json(data)
    assert back.ledger_item_ids == ("R2",) and back.withheld_ids == ("R1",)


def test_manifest_without_keys_reads_empty():
    data = _manifest().to_json()
    del data["ledger_item_ids"], data["withheld_ids"]
    back = StageManifest.from_json(data)
    assert back.ledger_item_ids == () and back.withheld_ids == ()

"""reconcile_open_rows: a ledger-met row nobody claimed is stamped met; a row still blocking is named."""
from __future__ import annotations

from datetime import datetime, timezone

import yaml

from coordinator_core.ops import requirement_register as rr

EV = {"command": "python -m consumer --run", "observed": "consumer reached the output"}
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
PLAN = "docs/plans/2026-11/later-run.md"
EARLIER = "docs/plans/2026-11/earlier-run.md"


def _row(rid, status="open", **kw):
    row = {
        "id": rid, "source_text": f"text {rid}", "anchor": "s1", "surface": "ui",
        "status": status, "claimed_by": [], "wired": False, "met_by": [],
        "last_progress_at": None, "ruling": None,
    }
    row.update(kw)
    return row


def _sizing(rows):
    block = yaml.safe_dump({"requirement_register": {"sources": [{"kind": "pm"}], "rows": rows}}, sort_keys=False)
    return "schema: sizing-object\nstatus: routed\n" + block


def _met(rid, **kw):
    return {"id": rid, "status": "met", "wired": True, **EV, **kw}


def test_apply_verdicts_alone_never_reaches_an_unclaimed_row():
    new = rr.apply_verdicts(_sizing([_row("R1")]), [_met("R1")], PLAN, "abc", NOW)
    assert rr.read_register(new).rows[0]["status"] == "open"


def test_unclaimed_and_earlier_run_rows_are_stamped_met_with_the_landing_sha():
    rows = [_row("R1"), _row("R2", "partial", claimed_by=[EARLIER]), _row("R3", "met", wired=False)]
    new, owed = rr.reconcile_open_rows(_sizing(rows), [_met("R1"), _met("R2"), _met("R3")], PLAN, "abc", NOW)
    got = {r["id"]: r for r in rr.read_register(new).rows}
    for rid in ("R1", "R2", "R3"):
        assert got[rid]["status"] == "met" and got[rid]["wired"] is True
        assert got[rid]["met_by"] == [f"{PLAN}@abc"]
    assert PLAN in got["R1"]["claimed_by"]
    assert owed == [] and rr.ship_refusal(rr.read_register(new)) is None


def test_row_the_ledger_did_not_meet_is_named_unreconciled_not_skipped():
    rows = [_row("R1"), _row("R2"), _row("R3", "partial")]
    judged = [_met("R1"), {"id": "R2", "status": "not_met"}]
    new, owed = rr.reconcile_open_rows(_sizing(rows), judged, PLAN, "abc", NOW)
    got = {r["id"]: r["status"] for r in rr.read_register(new).rows}
    assert got == {"R1": "met", "R2": "open", "R3": "partial"}
    assert [(o["id"], o["status"], o["ledger_met"]) for o in owed] == [("R2", "open", False), ("R3", "partial", False)]
    assert all(o["reason"] for o in owed)


def test_ledger_met_but_unwired_row_lands_partial_and_is_named_with_ledger_met():
    new, owed = rr.reconcile_open_rows(
        _sizing([_row("R1")]), [{"id": "R1", "status": "met", "wired": False}], PLAN, "abc", NOW)
    assert rr.read_register(new).rows[0]["status"] == "partial"
    assert [(o["id"], o["ledger_met"]) for o in owed] == [("R1", True)]


def test_ruled_deferred_row_is_untouched_and_not_owed():
    ruling = {"source": "pm", "quote": "defer it", "on": "2026-11-01"}
    rows = [_row("R1", "deferred", ruling=ruling)]
    new, owed = rr.reconcile_open_rows(_sizing(rows), [_met("R1")], PLAN, "abc", NOW)
    assert rr.read_register(new).rows[0]["status"] == "deferred" and owed == []


def test_repeat_at_same_sha_writes_nothing_and_no_register_is_identity():
    once, _ = rr.reconcile_open_rows(_sizing([_row("R1")]), [_met("R1")], PLAN, "abc", NOW)
    again, owed = rr.reconcile_open_rows(once, [_met("R1")], PLAN, "abc", NOW)
    assert again == once and owed == []
    assert rr.reconcile_open_rows("status: routed\n", [_met("R1")], PLAN, "abc", NOW) == ("status: routed\n", [])

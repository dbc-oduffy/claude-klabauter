"""Tests for register_tally and plan_register_tally."""
from __future__ import annotations

import yaml

from coordinator_core.ops import requirement_register as rr

RULING = {"source": "pm", "quote": "defer it", "on": "2026-10-09"}
SIZING_REL = "state/sizings/s.yaml"


def _row(rid, status="open", **kw):
    row = {
        "id": rid, "source_text": f"text {rid}", "anchor": "§1", "surface": "internal",
        "status": status, "claimed_by": [], "wired": False, "met_by": [],
        "last_progress_at": None, "ruling": None,
    }
    row.update(kw)
    return row


def _register(rows):
    return rr.Register(rows=rows)


def _sizing_text(rows):
    return "schema: sizing-object\n" + yaml.safe_dump(
        {"requirement_register": {"sources": [{"kind": "pm"}], "rows": rows}}, sort_keys=False)


def _plan_fm(sizing=SIZING_REL, rows=("A",)):
    return yaml.safe_dump({"register_claims": {"sizing": sizing, "rows": list(rows)}})


def test_claimed_counts_by_status():
    reg = _register([_row("A"), _row("B", "partial"), _row("C", "met"),
                     _row("D", "met", wired=True)])
    assert rr.register_tally(reg, ["A", "B", "C", "D"]) == {
        "unclaimed": 0, "open": 1, "partial": 1, "unwired": 1}


def test_unclaimed_met_and_wired_counts_zero():
    reg = _register([_row("A", "met", wired=True)])
    assert rr.register_tally(reg, [])["unclaimed"] == 0
    assert rr.blocking_unclaimed(reg, []) is None


def test_unclaimed_open_counts_one():
    reg = _register([_row("A")])
    assert rr.register_tally(reg, [])["unclaimed"] == 1
    assert rr.blocking_unclaimed(reg, []) == "A"


def test_ruled_deferred_counts_zero_unruled_counts_one():
    ruled = _register([_row("A", "deferred", ruling=RULING)])
    unruled = _register([_row("A", "deferred")])
    assert rr.register_tally(ruled, [])["unclaimed"] == 0
    assert rr.blocking_unclaimed(ruled, []) is None
    assert rr.register_tally(unruled, [])["unclaimed"] == 1
    assert rr.blocking_unclaimed(unruled, []) == "A"


def test_row_with_other_claimant_is_not_unclaimed():
    reg = _register([_row("A", claimed_by=["docs/plans/other.md"])])
    assert rr.register_tally(reg, [])["unclaimed"] == 0


def test_plan_tally_live_sizing(tmp_path):
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(_sizing_text([_row("A"), _row("B")]), encoding="utf-8")
    assert rr.plan_register_tally(_plan_fm(), tmp_path) == {
        "sizing": SIZING_REL, "unclaimed": 1, "open": 1, "partial": 0, "unwired": 0}


def test_plan_tally_resolves_archived_sizing(tmp_path):
    archive = tmp_path / "archive/sizings/2026-10"
    archive.mkdir(parents=True)
    (archive / "s.yaml").write_text(_sizing_text([_row("A", "met", wired=True)]), encoding="utf-8")
    assert rr.plan_register_tally(_plan_fm(), tmp_path) == {
        "sizing": SIZING_REL, "unclaimed": 0, "open": 0, "partial": 0, "unwired": 0}


def test_plan_tally_none_without_claims_sizing_or_register(tmp_path):
    assert rr.plan_register_tally("title: x\n", tmp_path) is None
    assert rr.plan_register_tally(_plan_fm(), tmp_path) is None
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text("schema: sizing-object\n", encoding="utf-8")
    assert rr.plan_register_tally(_plan_fm(), tmp_path) is None

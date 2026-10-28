"""A landed run whose ledger judged register row R1 met closes done, not partial [R1] (5c151b shape)."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import land_run, terminal_commit_closeout as tcc
from coordinator_core.ops.dispatch_emit.run_close_parts import close_from_sizing_text, ledger_met_ids

_SIZING = "state/sizings/2026-10-27-x.yaml"
_PLAN = "docs/plans/p.md"


def _sizing_text(status="open"):
    head = (
        "schema: sizing-object\nintent: x\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: e\n"
    )
    row = {"id": "R1", "source_text": "dedupe", "surface": "ui", "status": status, "claimed_by": []}
    reg = {"requirement_register": {"sources": [{"doc": "b.md", "ref": "r@abc"}], "rows": [row]}}
    return head + yaml.safe_dump(reg, sort_keys=False)


def _root(tmp_path):
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / _SIZING).write_text(_sizing_text(), encoding="utf-8")
    (tmp_path / _PLAN).write_text(f"---\ntitle: p\nsizing_object: {_SIZING}\n---\nb\n", encoding="utf-8")
    return tmp_path


def _close(root, ledger_met):
    ctx = {
        "root": str(root), "run_id": "r1", "applies": False, "legs": {}, "presets": {},
        "completion_entry": None, "kind": "xs-s-run", "plan_rel": _PLAN, "ledger_met": ledger_met,
    }
    return tcc._close_block(ctx, {"committed": True, "sha": "a" * 40})


def test_terminal_commit_close_reads_ledger_met_row(tmp_path):
    block = _close(_root(tmp_path), "ledger-met: R1")
    assert (block["kind"], block["outcome"], block["open_parts"]) == ("xs-s-run", "done", [])


def test_terminal_commit_close_without_ledger_met_stays_partial(tmp_path):
    block = _close(_root(tmp_path), None)
    assert (block["outcome"], block["open_parts"]) == ("partial", ["R1"])


def test_ledger_met_for_another_row_leaves_r1_open(tmp_path):
    block = _close(_root(tmp_path), "ledger-met: R2")
    assert (block["outcome"], block["open_parts"]) == ("partial", ["R1"])


def test_land_run_close_parts_read_ledger_met_row():
    assert land_run._close_parts_of(_sizing_text(), "ledger-met: R1").outcome == "done"
    close = land_run._close_parts_of(_sizing_text(), None)
    assert (close.outcome, close.open_parts) == ("partial", ("R1",))


def test_ledger_met_ids_parse():
    assert ledger_met_ids("ledger-met: R1, R2") == {"R1", "R2"}
    assert ledger_met_ids(None) == set()
    assert close_from_sizing_text(_sizing_text(), ledger_met="ledger-met: R1").open_parts == ()

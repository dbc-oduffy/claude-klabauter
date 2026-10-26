"""P-A replay: 08132c dropped part (a) at sizing and its close reported no open part."""

import yaml

from coordinator_core.ops.dispatch_emit.run_close import build_close_block
from coordinator_core.ops.dispatch_emit.run_close_parts import close_from_sizing_text, part_closes, with_parts

SHA = "a" * 40
MET = {"commit": SHA, "test": "t.py::test_x", "test_verdict": "pass"}


def _row(rid, status="met", evidence=MET):
    return {"id": rid, "status": status, "evidence": evidence}


def _close(close):
    return build_close_block("run-08132c", True, {}, parts=close)


def test_close_block_built_through_run_close_carries_outcome_and_open_parts():
    block = build_close_block("run-08132c", True, {}, parts=part_closes([_row("b")], stated_labels=["a", "b"]))
    assert (block["outcome"], block["open_parts"], block["missing_receipts"]) == ("partial", ["(a)"], [])
    assert "outcome" not in build_close_block("run-08132c", True, {})


def test_with_parts_is_the_block_decoration():
    block = with_parts(build_close_block("run-08132c", True, {}), part_closes([]))
    assert block["outcome"] == "done"


def test_close_from_sizing_text_reads_the_register_and_a_ruled_out_part_is_not_open():
    ruling = {"source": "pm", "quote": "drop", "ref": "r", "on": "2026-10-25"}
    rows = [
        {"id": "R1", "source_text": "(a) gating", "surface": "ui", "status": "deferred", "claimed_by": [], "ruling": ruling},
        {"id": "R2", "source_text": "(b) remnant", "surface": "ui", "status": "open", "claimed_by": []},
    ]
    text = "schema: sizing-object\n" + yaml.safe_dump(
        {"requirement_register": {"sources": [{"doc": "d", "ref": "r@a"}], "rows": rows}}, sort_keys=False
    )
    got = close_from_sizing_text(text, stated_labels=["a", "b"])
    assert got.open_parts == ("R2",)
    assert close_from_sizing_text(None).outcome == "done"


def test_part_a_dropped_at_sizing_closes_partial():
    block = _close(part_closes([_row("b")], stated_labels=["a", "b"]))
    assert block["outcome"] == "partial"
    assert block["open_parts"] == ["(a)"]


def test_all_met_with_evidence_closes_done():
    block = _close(part_closes([_row("a"), _row("b")], stated_labels=["a", "b"]))
    assert block["outcome"] == "done"
    assert block["open_parts"] == []


def test_met_row_lacking_commit_or_passed_test_stays_open():
    rows = [
        _row("a", evidence={"test": "t", "test_verdict": "pass"}),
        _row("b", evidence={"commit": SHA, "test": "t", "test_verdict": "fail"}),
        _row("c", status="partial"),
    ]
    assert part_closes(rows).open_parts == ("a", "b", "c")


def test_gitignored_commit_evidence_never_counts():
    paths = {SHA: [".coordinator-local/x.md"], "b" * 40: ["coordinator_core/x.py"]}
    rows = [_row("a"), _row("b", evidence={**MET, "commit": "b" * 40})]
    got = part_closes(rows, commit_paths=lambda c: paths[c])
    assert got.open_parts == ("a",)


def test_plan_row_missing_from_manifest_without_defer_reason_is_named():
    got = part_closes(
        [_row("a")],
        plan_row_ids=["C3", "C6", "C7"],
        staged_row_ids=["C7"],
        defer_reasons={"C6": "sibling holds ask_compose.py"},
    )
    assert got.outcome == "partial"
    assert got.open_parts == ("C3",)

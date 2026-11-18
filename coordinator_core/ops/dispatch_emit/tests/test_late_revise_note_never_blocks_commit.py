"""A late non-verbatim revise note never blocks the terminal commit; a late verbatim row still does."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as tcl


def _item(rid: str, text: str, anchor: str, origin: str) -> rl.Item:
    return rl.Item(id=rid, text=text, anchor=anchor, origin=origin, at="2026-11-18")


def _ledger(*items: rl.Item) -> rl.Ledger:
    return rl.Ledger(items=tuple(items), all_of_pm_ask=False, sizing_rel=None, withheld=())


_R1 = _item("R1", "the ask", "ask", "ask")
_MET_R1 = {"items": [{"id": "R1", "status": "met"}]}


def test_late_note_passes_and_is_listed_open_as_arrived_after_the_check() -> None:
    result = rl.judge(_ledger(_R1), _MET_R1, note_ids=["N1"])
    assert result["pass"] is True and result["failures"] == []
    assert [(r["id"], "arrived after the check" in r["reason"]) for r in result["open"]] == [("N1", True)]


def test_late_verbatim_revision_row_still_fails_unjudged() -> None:
    late = _item("R2", "the PM's later words", "revision@2026-11-18T10:00:00Z", rl.ORIGIN_REVISION)
    result = rl.judge(_ledger(_R1, late), _MET_R1, note_ids=[])
    assert result["pass"] is False
    assert result["failures"] == [{"id": "R2", "reason": rl.UNJUDGED_REASON}]


def test_late_note_beside_late_verbatim_row_still_refuses_for_the_row_only() -> None:
    late = _item("R2", "the PM's later words", "revision@2026-11-18T10:00:00Z", rl.ORIGIN_REVISION)
    result = rl.judge(_ledger(_R1, late), _MET_R1, note_ids=["N1"])
    assert result["pass"] is False
    assert [f["id"] for f in result["failures"]] == ["R2"]


def test_late_note_does_not_block_the_ledger_met_stamp() -> None:
    result = rl.judge(_ledger(_R1), _MET_R1, note_ids=["N1"])
    assert tcl._blocking_open_rows(_ledger(_R1), _MET_R1, result["open"]) == []
    assert tcl._ledger_met_observation(_ledger(_R1), _MET_R1, result["open"]) == "ledger-met: R1"

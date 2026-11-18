"""judge() carries a live revise note the verdict neither judges nor supersedes open, never as a failure."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl


def _ledger() -> rl.Ledger:
    return rl.Ledger(
        items=[rl.Item(id="R1", text="the ask", anchor="a", origin="pm", at="2026-11-18")],
        all_of_pm_ask=False, sizing_rel=None, withheld=[],
    )


def _failures(items: list[dict], note_ids: list[str]) -> list[dict]:
    return rl.judge(_ledger(), {"items": items}, note_ids=note_ids)["failures"]


def test_omitted_live_notes_are_carried_open_not_failed() -> None:
    result = rl.judge(_ledger(), {"items": [{"id": "R1", "status": "met"}]}, note_ids=["N2", "N3"])
    assert result["pass"] is True
    assert result["failures"] == []
    assert result["open"] == [
        {"id": "N2", "reason": rl.LATE_NOTE_REASON, rl.LATE_NOTE_KEY: True},
        {"id": "N3", "reason": rl.LATE_NOTE_REASON, rl.LATE_NOTE_KEY: True},
    ]
    assert "arrived after the check" in rl.LATE_NOTE_REASON


def test_judged_met_note_passes() -> None:
    items = [{"id": "R1", "status": "met"}, {"id": "N2", "status": "met"}]
    result = rl.judge(_ledger(), {"items": items}, note_ids=["N2"])
    assert result["failures"] == [] and result["open"] == []


def test_note_named_as_superseded_by_is_not_unjudged() -> None:
    items = [{"id": "R1", "status": "superseded", "superseded_by": "N2"}]
    result = rl.judge(_ledger(), {"items": items}, note_ids=["N2"])
    assert result["failures"] == [] and result["open"] == []


def test_judged_not_met_note_still_fails_not_met_once() -> None:
    items = [{"id": "R1", "status": "met"}, {"id": "N2", "status": "not_met"}]
    assert _failures(items, ["N2"]) == [{"id": "N2", "reason": rl.NOTE_NOT_MET_REASON}]


def test_no_live_notes_adds_no_failure() -> None:
    assert _failures([{"id": "R1", "status": "met"}], []) == []

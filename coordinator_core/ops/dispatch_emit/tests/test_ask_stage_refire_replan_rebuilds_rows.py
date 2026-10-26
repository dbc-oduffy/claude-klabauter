"""A refire after a re-plan stages the new spine, not the receipt's manifest of the old one (run c415fa shape)."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_stage_receipt, resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import (
    PLAN_REL,
    RUN,
    _dep,
    _plan,
    _row,
    _stage,
)

CREDITED = ["C0", "C2", "C3"]
GATE_ROWS = ["C4", "C7", "C5", "C8", "C9"]


def _old_plan() -> str:
    return "".join(_row(r) for r in [*CREDITED, "C4", "C7", "C5"]) + _row("C8", _dep("C5")) + _row("C9", _dep("C8"))


def _new_plan() -> str:
    return (
        "".join(_row(r) for r in [*CREDITED, "C4", "C7", "C5"])
        + _row("C8a", _dep("C5"))
        + _row("C8b", _dep("C8a"))
        + _row("C8c", _dep("C8a"))
        + _row("C8m", _dep("C8b", "C8c"))
        + _row("C9", _dep("C8m"))
    )


def test_refire_after_replan_stages_new_rows_and_retires_the_old_one(tmp_path):
    _plan(tmp_path, _old_plan())
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, CREDITED, GATE_ROWS, reasons={r: "landed" for r in CREDITED})
    first = _stage(tmp_path, GATE_ROWS)
    assert {r["id"] for r in first["rows"]} == set(GATE_ROWS)

    _plan(tmp_path, _new_plan())
    again = _stage(tmp_path, GATE_ROWS)

    assert "already_staged" not in again
    rows = {r["id"]: r for r in again["rows"]}
    assert set(rows) == {"C4", "C7", "C5", "C8a", "C8b", "C8c", "C8m", "C9"}
    assert "C8" not in rows
    assert again["retired_resume_rows"] == ["C8"]
    assert not set(CREDITED) & set(rows)
    assert rows["C8a"]["wave"] < rows["C8m"]["wave"] < rows["C9"]["wave"]
    receipt = ask_stage_receipt._read_receipt(record_dir(tmp_path, RUN))
    assert receipt is not None and receipt.get("spine_sha")


def test_refire_with_an_unchanged_plan_still_replies_already_staged(tmp_path):
    _plan(tmp_path, _old_plan())
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, CREDITED, GATE_ROWS, reasons={r: "landed" for r in CREDITED})
    _stage(tmp_path, GATE_ROWS)
    from coordinator_core.ops.dispatch_emit.ask_stage import _handler

    again = _handler({"run_id": RUN, "plan_path": PLAN_REL, "only_rows": GATE_ROWS}, repo_root=tmp_path)
    assert again.get("already_staged") is True

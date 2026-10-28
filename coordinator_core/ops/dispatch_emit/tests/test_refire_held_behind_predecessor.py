"""A refire reports a row held behind an unfinished epistemic-premise predecessor with a reason."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import (
    PLAN_REL,
    RUN,
    _plan,
    _row,
    _stage,
)

_HELD = (
    "- id: C4\n  title: tC4\n  change_kind: doc-edit\n"
    "  depends_on:\n    - chunk: C3\n      gate_kind: epistemic-premise\n"
)


def test_epistemic_held_row_is_reported_not_refused(tmp_path):
    _plan(tmp_path, _row("C3") + _HELD)
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], ["C3", "C4"])
    reply = _stage(tmp_path, ["C3", "C4"])
    assert "error" not in reply
    assert {r["id"] for r in reply["rows"]} == {"C3"}
    assert reply["dropped_rows"] == [{"id": "C4", "reason": "held behind C3 (epistemic-premise)"}]


def test_landed_predecessor_gives_no_held_behind_reason(tmp_path):
    _plan(tmp_path, _row("C3") + _row("C5") + _HELD)
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C3"], ["C4"], reasons={"C3": "landed"})
    reply = _stage(tmp_path, ["C4"])
    assert "error" not in reply
    assert "C4" in {r["id"] for r in reply["rows"]}
    assert all("held behind" not in d["reason"] for d in reply.get("dropped_rows", []))

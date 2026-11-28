"""Every stage manifest names each unscheduled, unfinished plan row with a reason; an unexplained one refuses."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, _handler, _remaining_rows
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/2026-11-21-remaining.md"

_PLAN = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: U1
  title: ungated
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: G1
  title: gated
  change_kind: code-edit
  surface: pkg/g.py
  writes:
    - pkg/g.py
  external_gate:
    - owner_repo: other
      requires: landed-work
- id: E1
  title: premise-gated, surface not declared
  change_kind: code-edit
  surface: pkg/e.py
  depends_on:
    - chunk: U1
      gate_kind: epistemic-premise
      note: judged on U1's verdict
```
"""


def _stage(tmp_path: Path, plan: str = _PLAN) -> dict:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return read_stage_reply(tmp_path, _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path))


def test_gated_and_held_out_rows_are_listed_with_their_reasons(tmp_path: Path) -> None:
    reply = _stage(tmp_path)
    assert "error" not in reply
    by_id = {r["id"]: r for r in reply["remaining_rows"]}
    assert by_id["G1"]["reason"] == "gated"
    assert by_id["E1"]["reason"] == "blocked_by", reply
    assert "epistemic-premise" in by_id["E1"]["detail"]
    assert "U1" not in by_id


def test_staged_rows_are_not_remaining(tmp_path: Path) -> None:
    plan = _PLAN.split("- id: G1")[0] + "```\n"
    reply = _stage(tmp_path, plan)
    assert [r["id"] for r in reply["rows"]] == ["U1"]
    assert "remaining_rows" not in reply


def _remaining(**over: object) -> tuple:
    args: dict = dict(
        raw_by_id={"R1": {"id": "R1"}},
        rows=[],
        staged=set(),
        landed=set(),
        parse_exclusions=[],
        gated=[],
        owed=[],
        slot_withheld=[],
        memo_close=[],
        retired=[],
        held_ids=[],
        dropped_rows=[],
        ran_premises={},
        unhonoured_order=[],
        plan_posix=PLAN_REL,
    )
    args.update(over)
    return _remaining_rows(**args)


def test_a_row_no_source_explains_refuses_the_stage_naming_it() -> None:
    with pytest.raises(AskStageError, match=r"UNEXPLAINED-REMAINING-ROW: .*R1"):
        _remaining()


def test_each_known_reason_is_carried() -> None:
    cases = {
        "owed": dict(owed=[SimpleNamespace(id="R1", reason="em-performed", detail="the EM runs it")]),
        "depends_on_plan": dict(parse_exclusions=[{"id": "R1", "reason": "depends_on_plan", "detail": "P2"}]),
        "excluded": dict(parse_exclusions=[{"id": "R1", "reason": "premise-closed", "detail": "x"}]),
        "unhonoured_order": dict(unhonoured_order=[{"id": "R1", "reason": "not in this pass's row set"}]),
        "held": dict(held_ids=["R1"]),
    }
    for reason, over in cases.items():
        (got,) = _remaining(**over)
        assert got["id"] == "R1" and got["reason"] == reason, reason
        assert set(got) == {"id", "reason", "detail"}


def test_missing_writes_row_is_explained() -> None:
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow

    row = EmitterRow(id="R1", title="t", surface="s", writes=UNDECLARED, reads=[], depends_on=[])
    (got,) = _remaining(rows=[row])
    assert got["reason"] == "missing_writes"


def test_an_ordered_row_left_out_keeps_both_reasons() -> None:
    (got,) = _remaining(
        parse_exclusions=[{"id": "R1", "reason": "depends_on_plan", "detail": "P2"}],
        unhonoured_order=[{"id": "R1", "reason": "held out: waits"}],
    )
    assert got["reason"] == "depends_on_plan"
    assert "ordered but not honoured: held out: waits" in got["detail"]


def test_landed_and_closed_rows_are_finished_not_remaining() -> None:
    assert _remaining(landed={"R1"}) == ()
    assert _remaining(raw_by_id={"R1": {"id": "R1", "disposition": "wont_do"}}) == ()
    assert _remaining(staged={"R1"}) == ()

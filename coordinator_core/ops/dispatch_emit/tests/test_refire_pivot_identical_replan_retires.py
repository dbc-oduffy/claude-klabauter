"""A pivot replan that leaves the spine unchanged still retires the open unlanded rows the leg did not keep."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_compose import _REPLAN_STAGED_SCHEMA
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import PLAN_REL, RUN, _plan, _row

OPEN = ["C1", "C2", "C3", "C4", "C5"]


def _identical_spine(tmp_path: Path, extra: str = "") -> None:
    """C0 landed, C1-C5 open: the post-replan spine equals the pre-halt spine."""
    _plan(tmp_path, _row("C0", "  disposition: coded\n") + "".join(_row(c) for c in OPEN) + extra)
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C0"], OPEN, reasons={"C0": "landed"})


def _stage(tmp_path: Path, **extra: object) -> dict:
    params = {"run_id": RUN, "plan_path": PLAN_REL, **extra}
    return read_stage_reply(tmp_path, _handler(params, repo_root=tmp_path))


def test_an_identical_spine_with_nothing_kept_stages_none_and_retires_every_open_row(tmp_path: Path) -> None:
    _identical_spine(tmp_path)
    reply = _stage(tmp_path, only_rows=[], pivot_resume=True, kept_rows=[])
    assert "error" not in reply
    assert reply["rows"] == []
    assert reply["retired_resume_rows"] == OPEN


def test_the_pre_halt_open_rows_are_requested_even_when_the_leg_names_no_only_rows(tmp_path: Path) -> None:
    _identical_spine(tmp_path)
    reply = _stage(tmp_path, pivot_resume=True, kept_rows=[])
    assert "error" not in reply
    assert reply["rows"] == []
    assert reply["retired_resume_rows"] == OPEN


def test_a_row_the_leg_keeps_is_staged_and_the_rest_retire(tmp_path: Path) -> None:
    _identical_spine(tmp_path)
    reply = _stage(tmp_path, only_rows=[], pivot_resume=True, kept_rows=["C2"])
    assert "error" not in reply
    assert [r["id"] for r in reply["rows"]] == ["C2"]
    assert reply["retired_resume_rows"] == ["C1", "C3", "C4", "C5"]


def test_a_row_the_leg_added_is_staged_beside_the_retirement_of_the_unchanged_rows(tmp_path: Path) -> None:
    _identical_spine(tmp_path, _row("C8"))
    reply = _stage(tmp_path, only_rows=["C8"], pivot_resume=True, kept_rows=[])
    assert "error" not in reply
    assert [r["id"] for r in reply["rows"]] == ["C8"]
    assert reply["retired_resume_rows"] == OPEN


def test_a_closed_row_is_never_retired(tmp_path: Path) -> None:
    _plan(tmp_path, _row("C1") + _row("C3", "  disposition: coded\n"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], ["C1", "C3"], reasons={})
    reply = _stage(tmp_path, only_rows=[], pivot_resume=True, kept_rows=[])
    assert "error" not in reply
    assert reply["retired_resume_rows"] == ["C1"]


def test_without_pivot_resume_the_open_rows_stay_staged(tmp_path: Path) -> None:
    _identical_spine(tmp_path)
    reply = _stage(tmp_path, only_rows=OPEN, kept_rows=[])
    assert "error" not in reply
    assert {r["id"] for r in reply["rows"]} == set(OPEN)
    assert reply["retired_resume_rows"] == []


def test_the_replan_leg_reports_the_rows_it_keeps() -> None:
    assert "kept_rows" in _REPLAN_STAGED_SCHEMA["properties"]
    assert "kept_rows" not in _REPLAN_STAGED_SCHEMA["required"]

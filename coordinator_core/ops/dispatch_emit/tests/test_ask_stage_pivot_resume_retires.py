"""`ask.stage` with `pivot_resume` retires every requested id the replanned spine no longer holds."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import PLAN_REL, RUN, _plan, _row

ONLY = ["C1", "C2", "C3"]


def _stage(tmp_path: Path, only_rows: list[str], **extra: object) -> dict:
    params = {"run_id": RUN, "plan_path": PLAN_REL, "only_rows": only_rows, **extra}
    return read_stage_reply(tmp_path, _handler(params, repo_root=tmp_path))


def test_pivot_resume_retires_ids_the_replan_removed_and_stages_the_rest(tmp_path: Path) -> None:
    _plan(tmp_path, _row("C2") + _row("C8"))
    reply = _stage(tmp_path, ONLY, pivot_resume=True)
    assert "error" not in reply
    assert reply["retired_resume_rows"] == ["C1", "C3"]
    staged = {r["id"] for r in reply["rows"]}
    assert "C2" in staged
    assert not staged & {"C1", "C3"}


def test_pivot_resume_retires_with_a_snapshot_that_does_not_name_the_ids(tmp_path: Path) -> None:
    _plan(tmp_path, _row("C0") + _row("C2"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C0"], ["C2"], reasons={"C0": "landed"})
    reply = _stage(tmp_path, ["C2", "C4", "C5"], pivot_resume=True)
    assert "error" not in reply
    assert reply["retired_resume_rows"] == ["C4", "C5"]


def test_without_pivot_resume_a_missing_id_still_refuses(tmp_path: Path) -> None:
    _plan(tmp_path, _row("C2") + _row("C8"))
    reply = _stage(tmp_path, ONLY)
    assert "C1" in reply["error"] and "C3" in reply["error"]


def test_pivot_resume_false_still_refuses(tmp_path: Path) -> None:
    _plan(tmp_path, _row("C2") + _row("C8"))
    reply = _stage(tmp_path, ONLY, pivot_resume=False)
    assert "C1" in reply["error"]


def test_pivot_resume_with_every_id_present_retires_nothing(tmp_path: Path) -> None:
    _plan(tmp_path, _row("C1") + _row("C2") + _row("C3"))
    reply = _stage(tmp_path, ONLY, pivot_resume=True)
    assert reply["retired_resume_rows"] == []
    assert {r["id"] for r in reply["rows"]} == set(ONLY)


def test_pivot_resume_refuses_a_landed_row_the_replan_deleted(tmp_path: Path) -> None:
    _plan(tmp_path, _row("C2"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C0"], ["C2"], reasons={"C0": "landed"})
    reply = _stage(tmp_path, ["C0", "C2"], pivot_resume=True)
    assert "C0" in reply["error"] and "landed" in reply["error"]

"""A resume stage reconciles the gate's resume rows with the re-planned spine: retired rows are reported."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import (
    PLAN_REL,
    RUN,
    _dep,
    _plan,
    _row,
    _stage,
)

GATE_RESUME = ["C4", "C5A", "C5B", "C6A", "C6B"]
LANDED = ["C0", "C1", "C2", "C3"]


def _landed_rows() -> str:
    return "".join(_row(r) for r in LANDED)


def test_incident_shape_stages_new_spine_and_reports_retired(tmp_path):
    _plan(
        tmp_path,
        _landed_rows() + _row("C1B") + _row("C4", _dep("C1B")) + _row("C5B", _dep("C1B", "C4")),
    )
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, LANDED, GATE_RESUME)
    reply = _stage(tmp_path, GATE_RESUME)
    assert "error" not in reply
    rows = {r["id"]: r for r in reply["rows"]}
    assert set(rows) == {"C1B", "C4", "C5B"}
    assert rows["C1B"]["wave"] < rows["C4"]["wave"] < rows["C5B"]["wave"]
    assert reply["retired_resume_rows"] == ["C5A", "C6A", "C6B"]


def test_retire_one_open_row_and_add_one(tmp_path):
    _plan(tmp_path, _row("A0") + _row("A2") + _row("A3"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["A0"], ["A1", "A2"])
    reply = _stage(tmp_path, ["A1", "A2"])
    assert "error" not in reply
    assert {r["id"] for r in reply["rows"]} == {"A2", "A3"}
    assert reply["retired_resume_rows"] == ["A1"]


def test_no_replan_stages_open_rows_and_reports_nothing(tmp_path):
    _plan(tmp_path, _landed_rows() + _row("C4") + _row("C5A"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, LANDED, ["C4", "C5A"])
    reply = _stage(tmp_path, ["C4", "C5A"])
    assert {r["id"] for r in reply["rows"]} == {"C4", "C5A"}
    assert reply["retired_resume_rows"] == []


def test_unknown_id_with_snapshot_refuses_without_naming_retired(tmp_path):
    _plan(tmp_path, _landed_rows() + _row("C4"))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, LANDED, ["C4", "C5A"])
    reply = _stage(tmp_path, ["C4", "C5A", "C99"])
    assert "C99" in reply["error"]
    assert "C5A" not in reply["error"]


def test_unknown_id_without_snapshot_refuses(tmp_path):
    _plan(tmp_path, _landed_rows() + _row("C4"))
    reply = _stage(tmp_path, ["C4", "C5A"])
    assert "C5A" in reply["error"]


def test_legacy_snapshot_without_resume_rows_refuses(tmp_path):
    _plan(tmp_path, _landed_rows() + _row("C4"))
    path = tmp_path / RUN_DIR_ROOT / RUN / resume_snapshot.SNAPSHOT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plan": PLAN_REL, "not_to_run": LANDED}), encoding="utf-8")
    reply = _stage(tmp_path, ["C4", "C5A"])
    assert "C5A" in reply["error"]

"""dispatch.sibling_status: read-only fan-out view sharing run_close.release_view."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import run_close, sibling_status
from coordinator_core.warp_fire import fire_command

PARENT = "ask-parent-20261015T000000-abcdef"


def _run(rid, after=(), status=c.STATUS_READY):
    return c.SiblingRun(rid, rid, tuple(after), f"state/sizings/{rid}.yaml", status)


def _seed(root: Path, runs, statuses=None):
    parent = c.ParentRecord(PARENT, tuple(runs))
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(parent.to_json()), encoding="utf-8")
    for rid, st in (statuses or {}).items():
        path = root / c.status_rel(rid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": st, "parent_id": PARENT}), encoding="utf-8")


def _snapshot(root: Path):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _fan(root):
    runs = [
        _run("A"),
        _run("D", ["A"], c.STATUS_AFTER),
        _run("B", ["A"], c.STATUS_AFTER),
        _run("C", ["X"], c.STATUS_AFTER),
        _run("X"),
    ]
    _seed(root, runs, {"A": c.STATUS_LANDED, "X": c.STATUS_HALTED})


def test_parent_id_reports_statuses_and_released_in_fanout_order(tmp_path):
    _fan(tmp_path)
    out = sibling_status.sibling_status({"parent_id": PARENT}, tmp_path)
    assert out["exit_code"] == 0 and out["parent_id"] == PARENT
    assert [s["run_id"] for s in out["siblings"]] == ["A", "D", "B", "C", "X"]
    assert [s["status"] for s in out["siblings"]] == ["landed", "after", "after", "after", "halted"]
    assert out["released"] == [fire_command("state/sizings/D.yaml"), fire_command("state/sizings/B.yaml")]
    assert out["blocked"] == [{"run_id": "C", "slice_sizing": "state/sizings/C.yaml"}]


def test_run_id_resolves_parent_and_matches_parent_reply(tmp_path):
    _fan(tmp_path)
    by_parent = sibling_status.sibling_status({"parent_id": PARENT}, tmp_path)
    assert sibling_status.sibling_status({"run_id": "B"}, tmp_path) == by_parent
    assert sibling_status.sibling_status({"run_id": "A"}, tmp_path) == by_parent


def test_unknown_and_malformed_ids_refuse(tmp_path):
    _fan(tmp_path)
    assert sibling_status.sibling_status({"parent_id": "nope"}, tmp_path)["exit_code"] == 1
    assert sibling_status.sibling_status({"run_id": "nope"}, tmp_path)["exit_code"] == 1
    assert sibling_status.sibling_status({"run_id": "../x"}, tmp_path)["exit_code"] == 1
    assert sibling_status.sibling_status({}, tmp_path)["exit_code"] == 1
    assert sibling_status.sibling_status({"parent_id": PARENT, "run_id": "A"}, tmp_path)["exit_code"] == 1


def test_no_file_written(tmp_path):
    _fan(tmp_path)
    before = _snapshot(tmp_path)
    sibling_status.sibling_status({"parent_id": PARENT}, tmp_path)
    sibling_status.sibling_status({"run_id": "D"}, tmp_path)
    assert _snapshot(tmp_path) == before


def test_shares_computation_with_sibling_release(tmp_path):
    runs = [_run("A"), _run("B", ["A"], c.STATUS_AFTER)]
    _seed(tmp_path, runs, {"A": c.STATUS_RUNNING})
    copy = tmp_path / c.run_parent_copy_rel("A")
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(json.dumps({"parent_id": PARENT}), encoding="utf-8")
    out = run_close.sibling_release(tmp_path, "A", "a" * 40)
    view = sibling_status.sibling_status({"parent_id": PARENT}, tmp_path)
    assert view["released"] == out["released"]
    assert view["blocked"] == out["blocked"]


def test_registered_handler(tmp_path):
    _fan(tmp_path)
    out = sibling_status._handler({"parent_id": PARENT}, tmp_path)
    assert out["exit_code"] == 0
    assert sibling_status._handler({"parent_id": PARENT}, None)["exit_code"] == 1

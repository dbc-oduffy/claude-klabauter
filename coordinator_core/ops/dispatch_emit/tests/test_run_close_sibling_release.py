"""run_close.sibling_release: a run's close rewrites only its own status file and names what it frees."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.warp_fire import fire_command

SHA = "a" * 40
PARENT = "ask-parent-20261015T000000-abcdef"


def _run(rid, after=(), status=c.STATUS_READY):
    return c.SiblingRun(rid, rid, tuple(after), f"state/sizings/{rid}.yaml", status)


def _seed(root: Path, runs, bound=()):
    parent = c.ParentRecord(PARENT, tuple(runs))
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(parent.to_json()), encoding="utf-8")
    for rid in bound:
        copy = root / c.run_parent_copy_rel(rid)
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(rec.read_text(encoding="utf-8"), encoding="utf-8")
        st = root / c.status_rel(rid)
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"status": c.STATUS_RUNNING}), encoding="utf-8")
    return rec


def _status(root, rid):
    return json.loads((root / c.status_rel(rid)).read_text(encoding="utf-8"))


def test_landed_release_names_successor_fire_call(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", SHA, budget=2)
    assert out["parent_id"] == PARENT
    assert out["released"] == [fire_command("state/sizings/B.yaml")]
    assert out["blocked"] == []
    assert _status(tmp_path, "A") == {"status": "landed", "sha": SHA}


def test_halted_predecessor_blocks_successor_but_frees_queued_slot(tmp_path):
    runs = [_run("A"), _run("B", ["A"], c.STATUS_AFTER), _run("C", (), c.STATUS_QUEUED)]
    _seed(tmp_path, runs, bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", None, budget=1)
    assert _status(tmp_path, "A")["status"] == "halted"
    assert out["released"] == [fire_command("state/sizings/C.yaml")]
    assert out["blocked"] == [{"run_id": "B", "slice_sizing": "state/sizings/B.yaml"}]


def test_queued_run_drains_when_slot_frees(tmp_path):
    runs = [_run("A"), _run("B", (), c.STATUS_QUEUED)]
    _seed(tmp_path, runs, bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", SHA, budget=1)
    assert out["released"] == [fire_command("state/sizings/B.yaml")]


def test_queued_run_stays_while_budget_full(tmp_path):
    runs = [_run("A"), _run("B"), _run("C", (), c.STATUS_QUEUED)]
    _seed(tmp_path, runs, bound=["A", "B"])
    out = run_close.sibling_release(tmp_path, "A", SHA, budget=1)
    assert out["released"] == [] and out["blocked"] == []


def test_non_sibling_run_returns_none(tmp_path):
    assert run_close.sibling_release(tmp_path, "solo", SHA, budget=1) is None
    assert not (tmp_path / "scratch").exists()


def test_sequential_closes_write_only_own_status_and_leave_parent_copies(tmp_path):
    runs = [_run("A"), _run("B"), _run("C", (), c.STATUS_QUEUED)]
    rec = _seed(tmp_path, runs, bound=["A", "B"])
    before = {p: p.read_bytes() for p in [rec, *(tmp_path / c.run_parent_copy_rel(r) for r in "AB")]}
    other_before = (tmp_path / c.status_rel("B")).read_bytes()
    results: dict = {}

    def close(rid):
        results[rid] = run_close.sibling_release(tmp_path, rid, SHA, budget=2)

    close("A")
    assert (tmp_path / c.status_rel("B")).read_bytes() == other_before
    close("B")
    assert _status(tmp_path, "A")["status"] == "landed"
    assert _status(tmp_path, "B")["status"] == "landed"
    for path, data in before.items():
        assert path.read_bytes() == data
    fire = fire_command("state/sizings/C.yaml")
    assert results["A"]["released"] == [fire] and results["B"]["released"] == [fire]


def test_details_carry_no_absolute_path(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", SHA, budget=2)
    for call in out["released"]:
        run_close.check_path_hygiene(call)
    assert str(tmp_path) not in json.dumps(out)

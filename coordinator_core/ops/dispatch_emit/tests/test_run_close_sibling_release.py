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
    out = run_close.sibling_release(tmp_path, "A", SHA)
    assert out["parent_id"] == PARENT
    assert out["released"] == [fire_command("state/sizings/B.yaml")]
    assert out["blocked"] == []
    assert _status(tmp_path, "A") == {"status": "landed", "sha": SHA}


def test_halted_predecessor_blocks_successor_but_frees_queued_slot(tmp_path):
    runs = [_run("A"), _run("B", ["A"], c.STATUS_AFTER), _run("C", (), c.STATUS_QUEUED)]
    _seed(tmp_path, runs, bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", None)
    assert _status(tmp_path, "A")["status"] == "halted"
    assert out["released"] == [fire_command("state/sizings/C.yaml")]
    assert out["blocked"] == [{"run_id": "B", "slice_sizing": "state/sizings/B.yaml"}]


def test_halted_with_every_row_committed_releases_successor(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", None, rows_committed=True)
    assert _status(tmp_path, "A") == {"status": "halted", "rows_committed": True}
    assert out["released"] == [fire_command("state/sizings/B.yaml")]
    assert out["blocked"] == []


def test_rows_committed_mark_survives_a_later_plain_halt(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    run_close.sibling_release(tmp_path, "A", None, rows_committed=True)
    out = run_close.sibling_release(tmp_path, "A", None)
    assert _status(tmp_path, "A")["rows_committed"] is True
    assert out["released"] == [fire_command("state/sizings/B.yaml")]


def test_halted_with_an_uncommitted_row_still_blocks_successor(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", None, rows_committed=False)
    assert "rows_committed" not in _status(tmp_path, "A")
    assert out["released"] == []
    assert [b["run_id"] for b in out["blocked"]] == ["B"]


def test_queued_run_drains_when_slot_frees(tmp_path):
    runs = [_run("A"), _run("B", (), c.STATUS_QUEUED)]
    _seed(tmp_path, runs, bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", SHA)
    assert out["released"] == [fire_command("state/sizings/B.yaml")]


def test_landed_close_frees_every_waiting_run_with_no_unlanded_predecessor(tmp_path):
    runs = [_run("A"), _run("B"), _run("C", (), c.STATUS_QUEUED), _run("D", ["A"], c.STATUS_AFTER),
            _run("E", ["B"], c.STATUS_AFTER)]
    _seed(tmp_path, runs, bound=["A", "B"])
    out = run_close.sibling_release(tmp_path, "A", SHA)
    assert out["released"] == [fire_command("state/sizings/C.yaml"), fire_command("state/sizings/D.yaml")]
    assert out["blocked"] == []


def test_halt_never_rewrites_a_landed_status(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    own = tmp_path / c.status_rel("A")
    own.write_text(json.dumps({"status": "landed", "sha": SHA}), encoding="utf-8")
    before = own.read_bytes()
    assert run_close.sibling_release(tmp_path, "A", None) is None
    assert own.read_bytes() == before


def test_halt_of_running_status_keeps_parent_id(tmp_path):
    _seed(tmp_path, [_run("A")], bound=["A"])
    own = tmp_path / c.status_rel("A")
    own.write_text(json.dumps({"status": "running", "parent_id": PARENT}), encoding="utf-8")
    run_close.sibling_release(tmp_path, "A", None)
    assert _status(tmp_path, "A") == {"status": "halted", "parent_id": PARENT}


def test_halt_with_no_status_file_writes_nothing(tmp_path):
    assert run_close.sibling_release(tmp_path, "solo", None) is None
    assert not (tmp_path / "scratch").exists()


def test_non_sibling_run_returns_none(tmp_path):
    assert run_close.sibling_release(tmp_path, "solo", SHA) is None
    assert not (tmp_path / "scratch").exists()


def test_sequential_closes_write_only_own_status_and_leave_parent_copies(tmp_path):
    runs = [_run("A"), _run("B"), _run("C", (), c.STATUS_QUEUED)]
    rec = _seed(tmp_path, runs, bound=["A", "B"])
    before = {p: p.read_bytes() for p in [rec, *(tmp_path / c.run_parent_copy_rel(r) for r in "AB")]}
    other_before = (tmp_path / c.status_rel("B")).read_bytes()
    results: dict = {}

    def close(rid):
        results[rid] = run_close.sibling_release(tmp_path, rid, SHA)

    close("A")
    assert (tmp_path / c.status_rel("B")).read_bytes() == other_before
    close("B")
    assert _status(tmp_path, "A")["status"] == "landed"
    assert _status(tmp_path, "B")["status"] == "landed"
    for path, data in before.items():
        assert path.read_bytes() == data
    fire = fire_command("state/sizings/C.yaml")
    assert results["A"]["released"] == [fire] and results["B"]["released"] == [fire]


def test_closed_by_halted_predecessor_releases_successor_and_persists(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", None, closed_by="docs/plans/p.md")
    assert out["released"] == [fire_command("state/sizings/B.yaml")]
    assert out["blocked"] == [] and "superseded" not in out
    assert _status(tmp_path, "A") == {"status": "halted", "closed_by": "docs/plans/p.md"}
    run_close.sibling_release(tmp_path, "A", None)
    assert _status(tmp_path, "A")["closed_by"] == "docs/plans/p.md"


def test_superseded_successor_is_reported_not_released_or_blocked(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    marker = tmp_path / c.superseded_rel("B")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"superseded_by": "N"}), encoding="utf-8")
    out = run_close.sibling_release(tmp_path, "A", None, closed_by="docs/plans/p.md")
    assert out["released"] == [] and out["blocked"] == []
    assert out["superseded"] == [{"run_id": "B", "superseded_by": "N"}]
    plain = run_close.sibling_release(tmp_path, "A", None)
    assert plain["blocked"] == [] and plain["superseded"] == out["superseded"]


def test_superseded_halted_open_successor_is_not_blocked(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    marker = tmp_path / c.superseded_rel("B")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"superseded_by": "N"}), encoding="utf-8")
    out = run_close.sibling_release(tmp_path, "A", None)
    assert out["blocked"] == [] and out["released"] == []


def test_close_halted_runs_for_plan_closes_named_run(tmp_path, monkeypatch):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    from coordinator_core.ops.dispatch_emit import halted_run

    monkeypatch.setattr(halted_run, "halted_ask_run_for", lambda root, s: "A")
    monkeypatch.setattr(halted_run, "halted_run_for", lambda root, s: None)
    out = run_close.close_halted_runs_for_plan(tmp_path, "docs/plans/p.md", "state/sizings/A.yaml")
    assert out == {
        "closed": ["A"],
        "released": [fire_command("state/sizings/B.yaml")],
        "superseded": [],
    }
    assert _status(tmp_path, "A")["closed_by"] == "docs/plans/p.md"


def test_close_halted_runs_for_plan_skips_run_without_status(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_run

    monkeypatch.setattr(halted_run, "halted_ask_run_for", lambda root, s: "Z")
    monkeypatch.setattr(halted_run, "halted_run_for", lambda root, s: None)
    out = run_close.close_halted_runs_for_plan(tmp_path, "docs/plans/p.md", "state/sizings/Z.yaml")
    assert out == {"closed": [], "released": [], "superseded": []}


def test_details_carry_no_absolute_path(tmp_path):
    _seed(tmp_path, [_run("A"), _run("B", ["A"], c.STATUS_AFTER)], bound=["A"])
    out = run_close.sibling_release(tmp_path, "A", SHA)
    for call in out["released"]:
        run_close.check_path_hygiene(call)
    assert str(tmp_path) not in json.dumps(out)

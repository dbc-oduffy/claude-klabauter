"""Group join release: release_view reads join inputs; a local input's close releases its joins."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.warp_fire import fire_command

PARENT = "ask-parent-20261102T000000-abcdef"
JOIN_SIZING = "state/sizings/join.yaml"
SHA = "a" * 40


def _status(root: Path, rid: str, status: str, **extra) -> None:
    path = root / c.status_rel(rid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, **extra}), encoding="utf-8")


def _seed(root: Path, foreign: Path, inputs=None) -> None:
    inputs = inputs if inputs is not None else (c.ExternalInput("LOC"), c.ExternalInput("FOR", str(foreign)))
    runs = (
        c.SiblingRun("S1", "s1", (), "state/sizings/s1.yaml", c.STATUS_READY),
        c.SiblingRun("S2", "s2", (), "state/sizings/s2.yaml", c.STATUS_READY),
        c.SiblingRun("J", c.JOIN_LABEL, ("S1", "S2"), JOIN_SIZING, c.STATUS_AFTER, after_external=tuple(inputs)),
    )
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(c.ParentRecord(PARENT, runs).to_json()), encoding="utf-8")
    _status(root, "S1", c.STATUS_RUNNING, parent_id=PARENT)
    _status(root, "S2", c.STATUS_RUNNING, parent_id=PARENT)
    _status(root, "J", c.STATUS_AFTER, parent_id=PARENT)


def _waiters(root: Path, run_id: str) -> None:
    path = root / c.join_waiters_rel(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"parents": [PARENT]}), encoding="utf-8")


def test_join_held_while_a_sibling_or_input_is_unlanded(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    _status(local, "LOC", c.STATUS_LANDED)
    assert run_close.sibling_release(local, "S1", SHA)["released"] == []
    # S1 and S2 landed, foreign input still running: held
    _status(foreign, "FOR", c.STATUS_RUNNING)
    assert run_close.sibling_release(local, "S2", SHA)["released"] == []


def test_last_sibling_close_releases_join_once_inputs_landed(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    _status(local, "LOC", c.STATUS_LANDED)
    _status(foreign, "FOR", c.STATUS_LANDED)
    assert run_close.sibling_release(local, "S1", SHA)["released"] == []
    out = run_close.sibling_release(local, "S2", SHA)
    assert out["released"] == [fire_command(JOIN_SIZING)]
    assert out["blocked"] == []


def test_local_input_close_releases_join_through_join_waiters(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    _status(foreign, "FOR", c.STATUS_LANDED)
    run_close.sibling_release(local, "S1", SHA)
    run_close.sibling_release(local, "S2", SHA)
    _waiters(local, "LOC")
    assert not (local / c.status_rel("LOC")).exists()
    out = run_close.sibling_release(local, "LOC", SHA)
    assert out["released"] == [fire_command(JOIN_SIZING)]
    assert out["join_parents"] == [PARENT]
    written = json.loads((local / c.status_rel("LOC")).read_text(encoding="utf-8"))
    assert written == {"status": c.STATUS_LANDED, "sha": SHA}


def test_local_input_close_holds_join_while_a_sibling_runs(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    _status(foreign, "FOR", c.STATUS_LANDED)
    _waiters(local, "LOC")
    out = run_close.sibling_release(local, "LOC", SHA)
    assert out["released"] == []


def test_non_sibling_run_without_waiters_stays_a_no_op(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    assert run_close.sibling_release(local, "PLAIN", SHA) is None
    assert not (local / c.status_rel("PLAIN")).exists()


def test_halted_unclosed_input_blocks_the_join(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    _status(local, "LOC", c.STATUS_HALTED)
    _status(foreign, "FOR", c.STATUS_LANDED)
    run_close.sibling_release(local, "S1", SHA)
    out = run_close.sibling_release(local, "S2", SHA)
    assert out["released"] == []
    assert out["blocked"] == [{"run_id": "J", "slice_sizing": JOIN_SIZING}]


def test_halted_input_with_rows_committed_counts_landed(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    _status(local, "LOC", c.STATUS_HALTED, rows_committed=True)
    _status(foreign, "FOR", c.STATUS_LANDED)
    run_close.sibling_release(local, "S1", SHA)
    out = run_close.sibling_release(local, "S2", SHA)
    assert out["released"] == [fire_command(JOIN_SIZING)]


def test_foreign_input_read_never_writes_the_foreign_tree(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    _status(local, "LOC", c.STATUS_LANDED)
    _status(foreign, "FOR", c.STATUS_RUNNING)
    before = {p: p.read_bytes() for p in foreign.rglob("*") if p.is_file()}
    run_close.sibling_release(local, "S1", SHA)
    run_close.sibling_release(local, "S2", SHA)
    assert {p: p.read_bytes() for p in foreign.rglob("*") if p.is_file()} == before


def test_release_view_without_reconcile_writes_nothing(tmp_path):
    local, foreign = tmp_path / "local", tmp_path / "foreign"
    _seed(local, foreign)
    _status(local, "LOC", c.STATUS_RUNNING)
    parent = c.ParentRecord.from_json(json.loads((local / c.parent_record_rel(PARENT)).read_text("utf-8")))
    before = {p: p.read_bytes() for p in local.rglob("*") if p.is_file()}
    statuses, view = run_close.release_view(local, parent)
    assert statuses["LOC"] == c.STATUS_RUNNING
    assert f"{foreign}::FOR" not in statuses
    assert view["released"] == []
    assert {p: p.read_bytes() for p in local.rglob("*") if p.is_file()} == before

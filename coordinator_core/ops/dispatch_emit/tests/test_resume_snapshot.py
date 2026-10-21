"""Contract tests for resume_snapshot."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import resume_snapshot as rs

PLAN = "docs/plans/p.md"


def test_round_trip(tmp_path):
    path = rs.write(tmp_path, "run-1", PLAN, ["C2", "C1"], ["C3"])
    assert path == tmp_path / "scratch" / "warp" / "run-1" / "resume-snapshot.json"
    assert rs.read(tmp_path, "run-1", PLAN) == rs.Snapshot(frozenset({"C1", "C2"}), frozenset({"C3"}))


def test_legacy_body_reads_empty_resume_rows(tmp_path):
    path = rs.write(tmp_path, "run-1", PLAN, ["C1"], [])
    path.write_text('{"plan": "%s", "not_to_run": ["C1"]}' % PLAN, encoding="utf-8")
    snap = rs.read(tmp_path, "run-1", PLAN)
    assert snap.not_to_run == frozenset({"C1"})
    assert snap.resume_rows == frozenset()


def test_malformed_resume_rows_is_none(tmp_path):
    path = rs.write(tmp_path, "run-1", PLAN, ["C1"], [])
    path.write_text('{"plan": "%s", "not_to_run": ["C1"], "resume_rows": "C2"}' % PLAN, encoding="utf-8")
    assert rs.read(tmp_path, "run-1", PLAN) is None
    path.write_text('{"plan": "%s", "not_to_run": ["C1"], "resume_rows": [2]}' % PLAN, encoding="utf-8")
    assert rs.read(tmp_path, "run-1", PLAN) is None


def test_absent_is_none(tmp_path):
    assert rs.read(tmp_path, "run-1", PLAN) is None


def test_other_plan_is_none(tmp_path):
    rs.write(tmp_path, "run-1", PLAN, ["C1"], [])
    assert rs.read(tmp_path, "run-1", "docs/plans/other.md") is None


def test_malformed_is_none(tmp_path):
    path = rs.write(tmp_path, "run-1", PLAN, ["C1"], [])
    path.write_text("{not json", encoding="utf-8")
    assert rs.read(tmp_path, "run-1", PLAN) is None
    path.write_text('{"plan": "%s", "not_to_run": "C1"}' % PLAN, encoding="utf-8")
    assert rs.read(tmp_path, "run-1", PLAN) is None


def test_unsafe_run_id_refused(tmp_path):
    for bad in ("..", "../x", "a/b", ""):
        assert rs.write(tmp_path, bad, PLAN, ["C1"], []) is None
        assert rs.read(tmp_path, bad, PLAN) is None
    assert not (tmp_path / "scratch").exists()
    assert not (tmp_path / "x").exists()


def test_clear_removes_and_tolerates_missing(tmp_path):
    rs.write(tmp_path, "run-1", PLAN, ["C1"], [])
    rs.clear(tmp_path, "run-1")
    assert rs.read(tmp_path, "run-1", PLAN) is None
    rs.clear(tmp_path, "run-1")

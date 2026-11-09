"""Pure-function tests for the real engine-records session tripwire (tmp_path only)."""

import json

from coordinator_core.conftest import diff_engine_records, is_fixture_run_id, snapshot_engine_records


def _write(home, key, runs):
    d = home / "machine-local" / "engine-records" / key
    d.mkdir(parents=True, exist_ok=True)
    payload = {"runs": {r: {"footprint": None, "minted_at": 1.0} for r in runs}, "schema": "live-run-index/1"}
    (d / "coordinator-warp-live-run-index.json").write_text(json.dumps(payload), encoding="utf-8")


def test_fixture_predicate():
    for rid in ("ask-20261013T000000-abcdef", "run1", "run2", "ask-20990101T000000-d41e00",
                "plan-20261104T010101-x", "ask-20261101T173854-fresh1", "ask-20261101T173854-ghost1", "warp-2026-11-01-submit-budget-bench-ab"):
        assert is_fixture_run_id(rid)
    assert not is_fixture_run_id("ask-20261108T221038-7bdd4e")


def test_added_dir_and_fixture_id_reported(tmp_path):
    _write(tmp_path, "aaa", ["ask-20261108T000000-111111"])
    before = snapshot_engine_records(str(tmp_path))
    _write(tmp_path, "aaa", ["ask-20261108T000000-111111", "run1"])
    _write(tmp_path, "bbb", ["run2"])
    dirs, fixture, other = diff_engine_records(before, snapshot_engine_records(str(tmp_path)))
    assert dirs == ["bbb"]
    assert fixture == [("aaa", "run1"), ("bbb", "run2")]
    assert other == []


def test_non_fixture_addition_is_warning_only(tmp_path):
    _write(tmp_path, "aaa", [])
    before = snapshot_engine_records(str(tmp_path))
    _write(tmp_path, "aaa", ["ask-20261108T000000-222222"])
    _, fixture, other = diff_engine_records(before, snapshot_engine_records(str(tmp_path)))
    assert fixture == [] and other == [("aaa", "ask-20261108T000000-222222")]


def test_removals_ignored_and_absent_dir_is_empty(tmp_path):
    assert snapshot_engine_records(str(tmp_path)) == {}
    _write(tmp_path, "aaa", ["run1"])
    before = snapshot_engine_records(str(tmp_path))
    _write(tmp_path, "aaa", [])
    assert diff_engine_records(before, snapshot_engine_records(str(tmp_path))) == ([], [], [])


def test_a_body_time_monkeypatch_undo_keeps_the_home_quarantined(monkeypatch):
    import os

    pinned = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "COORDINATOR_SETTINGS_HOME")}
    monkeypatch.setenv("HOME", "elsewhere")
    monkeypatch.undo()
    assert {k: os.environ.get(k) for k in pinned} == pinned
    assert pinned["COORDINATOR_SETTINGS_HOME"] is None
    assert pinned["HOME"] and pinned["HOME"] == pinned["USERPROFILE"]

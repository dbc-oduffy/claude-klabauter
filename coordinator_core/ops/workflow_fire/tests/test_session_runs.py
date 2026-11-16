"""open_runs_for_session: a session's fires whose run is neither landed nor halted."""
import json

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import write_run_stage
from coordinator_core.ops.workflow_fire import fire
from coordinator_core.ops.workflow_fire.session_runs import open_runs_for_session

SESSION = "sess-mine"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = tmp_path / "fires"
    reg.mkdir()
    monkeypatch.setattr(fire, "_registry_dir", lambda *a: reg)
    return reg


def put(registry, fire_id, **extra):
    record = {"fire_id": fire_id, "state": "running", "caller_session_id": SESSION, **extra}
    (registry / f"{fire_id}.json").write_text(json.dumps(record), encoding="utf-8")


def open_ids(tmp_path, session=SESSION):
    return {r["fire_id"]: r for r in open_runs_for_session(tmp_path, session)}


def test_staged_run_is_open(tmp_path, registry):
    put(registry, "f1", run_id="run-a")
    write_run_stage(tmp_path, "run-a", "staged")
    assert open_ids(tmp_path) == {
        "f1": {"fire_id": "f1", "run_id": "run-a", "stage": "staged", "fire_state": "running"}}


def test_landed_and_halted_runs_are_terminal(tmp_path, registry):
    put(registry, "f1", run_id="run-a")
    put(registry, "f2", run_id="run-b")
    write_run_stage(tmp_path, "run-a", "landed")
    write_run_stage(tmp_path, "run-b", "halted")
    assert open_ids(tmp_path) == {}


def test_absent_stage_is_open_with_none_stage(tmp_path, registry):
    put(registry, "f1", run_id="run-never-staged", state="exited")
    assert open_ids(tmp_path)["f1"]["stage"] is None


def test_other_sessions_record_is_ignored(tmp_path, registry):
    put(registry, "f1", run_id="run-a", caller_session_id="sess-other")
    put(registry, "f2", run_id="run-b", caller_session_id=None)
    write_run_stage(tmp_path, "run-a", "staged")
    write_run_stage(tmp_path, "run-b", "staged")
    assert open_ids(tmp_path) == {}
    assert open_ids(tmp_path, "sess-other").keys() == {"f1"}


def test_no_run_id_is_open_only_while_fire_not_exited(tmp_path, registry):
    put(registry, "f1", state="running")
    put(registry, "f2", state="pending")
    put(registry, "f3", state="exited")
    ids = open_ids(tmp_path)
    assert ids.keys() == {"f1", "f2"}
    assert ids["f1"]["run_id"] is None and ids["f1"]["stage"] is None


def test_unsafe_run_id_reads_as_open(tmp_path, registry):
    put(registry, "f1", run_id="../escape")
    assert open_ids(tmp_path).keys() == {"f1"}


def test_empty_session_id_matches_nothing(tmp_path, registry):
    put(registry, "f1", run_id="run-a", caller_session_id="")
    assert open_runs_for_session(tmp_path, "") == []


def test_missing_registry_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fire, "_registry_dir", lambda *a: tmp_path / "absent")
    assert open_runs_for_session(tmp_path, SESSION) == []

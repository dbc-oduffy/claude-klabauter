"""dispatch.stage_wait_status: a parked run's status, blocker states and engine resume, read without writing."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core import ipc
from coordinator_core.ops.dispatch_emit import run_token, stage_wait, stage_wait_status

SHA = "a" * 40


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def states(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stand in for `blocker_check.evaluate`: a ref -> (state, payload) table the test sets."""
    table: dict = {}
    fake = types.ModuleType("coordinator_core.ops.dispatch_emit.blocker_check")
    fake.evaluate = lambda root, blocker: table.get(blocker["ref"], ("live", None))
    monkeypatch.setitem(sys.modules, "coordinator_core.ops.dispatch_emit.blocker_check", fake)
    return table


def _status(repo, run_id="waiter", now=None):
    return stage_wait_status.stage_wait_status({"run_id": run_id}, repo, now=now)


def _listing(repo):
    home = run_token._record_home(repo)
    return sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in home.rglob("*") if p.is_file())


def test_a_run_parked_on_a_live_blocker_is_waiting(repo, states) -> None:
    stage_wait.park(repo, "waiter", ["peer"], ["a.py"], now=1000.0)
    reply = _status(repo, now=1060.0)
    assert reply["exit_code"] == 0 and reply["status"] == "waiting"
    assert reply["blockers"] == [{"kind": "run_end", "ref": "peer", "state": "live"}]
    assert reply["resumed_by"] is None and reply["flag"] is None and reply["waited_s"] == 60.0
    assert reply["resume_action"] == "refire" and reply["on_blocker_dead"] == "surface"


def test_a_cleared_blocker_resumes_the_run_by_the_engine_with_no_session(repo, states) -> None:
    stage_wait.park(repo, "waiter", ["peer"], ["a.py"], session_id="s1", now=1000.0)
    assert [w["run_id"] for w in stage_wait.wake_on_end(repo, "peer")] == ["waiter"]
    reply = _status(repo)
    assert reply["status"] == "resumed"
    assert reply["resumed_by"] == "engine" and reply["session"] is None
    assert reply["blockers"] == []


def test_a_close_landed_resume_reports_its_action_and_landed_sha(repo, states) -> None:
    stage_wait.park(repo, "waiter", [{"kind": "run_landed", "ref": "peer"}], [], resume_action="close_landed", now=1000.0)
    path = stage_wait._path(repo, "waiter")
    record = stage_wait.read(repo, "waiter")
    record.update(started=True, resumed_by="engine", action="close_landed", sha=SHA, resumed_at=1090.0)
    stage_wait._write(path, record)
    reply = _status(repo, now=9999.0)
    assert (reply["status"], reply["resumed_by"], reply["session"], reply["action"], reply["sha"]) == (
        "resumed", "engine", None, "close_landed", SHA,
    )
    assert reply["waited_s"] == 90.0


def test_a_resume_after_the_pm_answered_is_reported_as_pm(repo, states) -> None:
    stage_wait.park(repo, "waiter", ["peer"], [], now=1000.0)
    record = stage_wait.read(repo, "waiter")
    record.update(started=True, resumed_by="pm", action="refire", resumed_at=1100.0)
    stage_wait._write(stage_wait._path(repo, "waiter"), record)
    assert _status(repo)["resumed_by"] == "pm"


def test_a_dead_blocker_under_surface_is_attention_never_proceeded(repo, states) -> None:
    states["peer"] = ("dead", None)
    stage_wait.park(repo, "waiter", ["peer"], [], now=1000.0)
    reply = _status(repo, now=1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S + 1)
    assert reply["status"] == "attention"
    assert reply["blockers"] == [{"kind": "run_end", "ref": "peer", "state": "dead"}]
    assert reply["resumed_by"] is None and reply["action"] is None


def test_a_dead_blocker_under_proceed_is_not_an_attention_park(repo, states) -> None:
    states["peer"] = ("dead", None)
    stage_wait.park(repo, "waiter", ["peer"], [], on_blocker_dead="proceed", now=1000.0)
    assert _status(repo, now=1001.0)["status"] == "waiting"


def test_a_recorded_attention_park_names_its_flag_and_never_expires(repo, states) -> None:
    stage_wait.park(repo, "waiter", ["peer"], [], now=1000.0)
    record = stage_wait.read(repo, "waiter")
    record.update(status="attention", flag="state/pm-flags/waiter.stage-wait.json")
    stage_wait._write(stage_wait._path(repo, "waiter"), record)
    reply = _status(repo, now=1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S + 1)
    assert reply["status"] == "attention" and reply["flag"] == "state/pm-flags/waiter.stage-wait.json"


def test_an_unstarted_wait_past_its_deadline_is_expired(repo, states) -> None:
    stage_wait.park(repo, "waiter", ["peer"], [], now=1000.0)
    assert _status(repo, now=1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S - 1)["status"] == "waiting"
    assert _status(repo, now=1000.0 + stage_wait.STAGE_WAIT_DEADLINE_S + 1)["status"] == "expired"


def test_a_landed_blocker_without_its_evaluator_reads_pending(repo, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "coordinator_core.ops.dispatch_emit.blocker_check", None)
    stage_wait.park(repo, "waiter", [{"kind": "run_landed", "ref": "peer"}], [], now=1000.0)
    assert _status(repo)["blockers"] == [{"kind": "run_landed", "ref": "peer", "state": "pending"}]


def test_a_missing_or_unsafe_run_is_refused(repo, states) -> None:
    assert _status(repo, "nobody")["exit_code"] == 1
    assert stage_wait_status.stage_wait_status({"run_id": "../x"}, repo)["exit_code"] == 1
    assert stage_wait_status.stage_wait_status({}, repo)["exit_code"] == 1


def test_the_read_writes_nothing(repo, states) -> None:
    stage_wait.park(repo, "waiter", ["peer"], [], now=1000.0)
    before = _listing(repo)
    _status(repo)
    assert _listing(repo) == before


def test_the_op_is_registered_and_dispatches_over_the_repo_root(repo, states) -> None:
    stage_wait.park(repo, "waiter", ["peer"], [], now=1000.0)
    handler = ipc.get_op_handler("dispatch.stage_wait_status")
    assert handler is stage_wait_status._handler
    assert handler({"run_id": "waiter"}, repo)["status"] in ("waiting", "expired")
    assert handler({"run_id": "waiter"})["exit_code"] == 1

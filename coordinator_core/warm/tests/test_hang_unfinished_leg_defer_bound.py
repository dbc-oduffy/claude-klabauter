"""An unfinished disk-decision leg suppresses only inside IO_SATURATED_MAX_DEFER_SECS of the in-memory first-suppress mark."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from coordinator_core.warm import hang_restart

BOUND = hang_restart.IO_SATURATED_MAX_DEFER_SECS
THRESHOLD = hang_restart.HANG_DUMP_THRESHOLD


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hang_restart, "_stall_marks", {})
    monkeypatch.setattr(hang_restart, "_first_suppress", {})
    monkeypatch.setattr(hang_restart, "_progress_last", None)
    release = threading.Event()
    monkeypatch.setattr(hang_restart, "_disk_decision", lambda *a: release.wait(30))
    yield release
    release.set()


def _burst(stall: Path, exits: list) -> str | None:
    out = None
    for _ in range(THRESHOLD):
        out = hang_restart.note_stall(stall, server_pid=7, exit_fn=exits.append, deadline=0.01)
    return out


def test_unfinished_leg_with_no_mark_restarts(tmp_path: Path) -> None:
    exits: list = []
    assert _burst(tmp_path / "stall-dumps", exits) == "restart"
    assert exits == [7]


def test_unfinished_leg_suppresses_inside_the_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stall = tmp_path / "stall-dumps"
    clock = [1000.0]
    monkeypatch.setattr(hang_restart.time, "monotonic", lambda: clock[0])
    monkeypatch.setitem(hang_restart._first_suppress, str(stall), clock[0])
    clock[0] += BOUND - 1
    exits: list = []
    assert _burst(stall, exits) == "suppressed"
    assert exits == []
    assert str(stall) in hang_restart._first_suppress


def test_unfinished_leg_restarts_once_past_the_bound_and_clears_the_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stall = tmp_path / "stall-dumps"
    clock = [1000.0]
    monkeypatch.setattr(hang_restart.time, "monotonic", lambda: clock[0])
    monkeypatch.setitem(hang_restart._first_suppress, str(stall), clock[0])
    exits: list = []
    clock[0] += BOUND - 1
    assert _burst(stall, exits) == "suppressed"
    clock[0] += 2
    assert _burst(stall, exits) == "restart"
    assert exits == [7]
    assert str(stall) not in hang_restart._first_suppress


def test_finished_suppressed_verdict_stamps_the_mark_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stall = tmp_path / "stall-dumps"
    clock = [1000.0]
    monkeypatch.setattr(hang_restart.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hang_restart, "_disk_decision", lambda *a: "suppressed")
    exits: list = []
    assert _burst(stall, exits) == "suppressed"
    assert hang_restart._first_suppress[str(stall)] == 1000.0
    clock[0] += 5
    assert _burst(stall, exits) == "suppressed"
    assert hang_restart._first_suppress[str(stall)] == 1000.0
    assert exits == []


def test_progress_short_circuit_never_stamps_the_mark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hang_restart, "progressed_within", lambda window: True)
    stall = tmp_path / "stall-dumps"
    exits: list = []
    assert _burst(stall, exits) == "suppressed"
    assert hang_restart._first_suppress == {}


def test_a_quiet_dirs_expired_mark_is_evicted_by_another_dirs_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet = tmp_path / "quiet" / "stall-dumps"
    other = tmp_path / "other" / "stall-dumps"
    clock = [1000.0]
    monkeypatch.setattr(hang_restart.time, "monotonic", lambda: clock[0])
    monkeypatch.setitem(hang_restart._first_suppress, str(quiet), clock[0])
    clock[0] += BOUND - 1
    hang_restart.note_stall(other, server_pid=7, exit_fn=lambda _p: None, deadline=0.01)
    assert str(quiet) in hang_restart._first_suppress
    clock[0] += 2
    hang_restart.note_stall(other, server_pid=7, exit_fn=lambda _p: None, deadline=0.01)
    assert str(quiet) not in hang_restart._first_suppress

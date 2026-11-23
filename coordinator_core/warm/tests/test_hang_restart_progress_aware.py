"""The hang watchdog must not restart a saturated server whose pool workers keep completing ops.

Drives `note_stall` and `maybe_restart` at default thresholds with an `exit_fn` recorder; stall dirs
live under `tmp_path`.
"""

from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path

import pytest

from coordinator_core.warm import hang_restart, server

pytestmark = pytest.mark.complete


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hang_restart, "_stall_marks", {})
    monkeypatch.setattr(hang_restart, "_progress_last", None)


def _stall_dir(tmp_path: Path) -> Path:
    stall = tmp_path / "svc" / "stall-dumps"
    stall.mkdir(parents=True)
    return stall


def _write_dumps(stall: Path, count: int) -> None:
    now = time.time()
    for i in range(count):
        stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(now - i))
        (stall / f"{stamp}-{i}.txt").write_text("stall-class: hung\n", encoding="utf-8")


def _log(stall: Path) -> str:
    log = stall.parent / hang_restart.HANG_RESTART_LOG_FILENAME
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_progressed_within_tracks_the_last_completion() -> None:
    assert hang_restart.progressed_within(60.0) is False
    hang_restart.note_progress()
    assert hang_restart.progressed_within(60.0) is True
    assert hang_restart.progressed_within(0.0) is False


def test_note_stall_suppresses_while_completions_advance(tmp_path: Path) -> None:
    stall = _stall_dir(tmp_path)
    exits: list = []
    hang_restart.note_progress()
    verdicts = [hang_restart.note_stall(stall, exit_fn=exits.append) for _ in range(hang_restart.HANG_DUMP_THRESHOLD)]
    assert verdicts[-1] == "suppressed"
    assert exits == []
    assert _log(stall).count("action=suppressed reason=progress") == 1


def test_note_stall_restarts_when_completions_stop(tmp_path: Path) -> None:
    stall = _stall_dir(tmp_path)
    exits: list = []
    verdicts = [hang_restart.note_stall(stall, exit_fn=exits.append) for _ in range(hang_restart.HANG_DUMP_THRESHOLD)]
    assert verdicts[-1] == "restart"
    assert len(exits) == 1
    assert "action=restart" in _log(stall)


def test_note_stall_restarts_once_progress_ages_out_of_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stall = _stall_dir(tmp_path)
    exits: list = []
    hang_restart.note_progress()
    monkeypatch.setattr(hang_restart, "_progress_last", time.monotonic() - hang_restart.HANG_WINDOW_SECS - 1.0)
    verdicts = [hang_restart.note_stall(stall, exit_fn=exits.append) for _ in range(hang_restart.HANG_DUMP_THRESHOLD)]
    assert verdicts[-1] == "restart" and len(exits) == 1


def test_maybe_restart_suppresses_while_completions_advance(tmp_path: Path) -> None:
    stall = _stall_dir(tmp_path)
    exits: list = []
    _write_dumps(stall, hang_restart.HANG_DUMP_THRESHOLD)
    hang_restart.note_progress()
    assert hang_restart.maybe_restart(stall, exit_fn=exits.append) == "suppressed"
    assert exits == []
    assert "action=suppressed reason=progress" in _log(stall)


def test_maybe_restart_restarts_when_completions_stop(tmp_path: Path) -> None:
    stall = _stall_dir(tmp_path)
    exits: list = []
    _write_dumps(stall, hang_restart.HANG_DUMP_THRESHOLD)
    assert hang_restart.maybe_restart(stall, exit_fn=exits.append) == "restart"
    assert len(exits) == 1


def _fire_result_phase_accept_stalls(stall: Path, count: int) -> None:
    """`count` accept-side watchers parked in the result phase (queued behind busy workers), each
    fired and disarmed."""
    disarms = [
        server._arm_accept_stall(stall, 0.01, None, f"req{i}", ["result"]) for i in range(count)
    ]
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and len(list(stall.glob("*.txt"))) < count:
        time.sleep(0.02)
    for disarm in disarms:
        disarm()
    assert len(list(stall.glob("*.txt"))) >= count


@pytest.fixture
def exits(monkeypatch: pytest.MonkeyPatch) -> list:
    recorded: list = []
    monkeypatch.setattr(hang_restart, "exit_server", recorded.append)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)
    return recorded


def test_await_pool_result_counts_a_completed_future_as_progress() -> None:
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_result({"ok": True})
    assert hang_restart.progressed_within(60.0) is False
    assert server._await_pool_result(fut, 5.0, None) == {"ok": True}
    assert hang_restart.progressed_within(60.0) is True


def test_await_pool_result_counts_a_completed_future_with_a_repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_drive_verdict", lambda root: None)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_result({"ok": True})
    assert server._await_pool_result(fut, 5.0, tmp_path) == {"ok": True}
    assert hang_restart.progressed_within(60.0) is True


def test_a_guard_lane_completion_is_not_progress_on_either_wait_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_drive_verdict", lambda root: None)
    for root in (None, tmp_path):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_result({"ok": True})
        assert server._await_pool_result(fut, 5.0, root, count_progress=False) == {"ok": True}
        assert hang_restart.progressed_within(60.0) is False


def test_await_pool_result_timeout_is_not_progress() -> None:
    fut: concurrent.futures.Future = concurrent.futures.Future()
    with pytest.raises(concurrent.futures.TimeoutError):
        server._await_pool_result(fut, 0.05, None)
    assert hang_restart.progressed_within(60.0) is False


def test_a_broken_or_cancelled_future_is_not_progress() -> None:
    broken: concurrent.futures.Future = concurrent.futures.Future()
    broken.set_exception(concurrent.futures.BrokenExecutor("pool died"))
    with pytest.raises(concurrent.futures.BrokenExecutor):
        server._await_pool_result(broken, 5.0, None)
    cancelled: concurrent.futures.Future = concurrent.futures.Future()
    cancelled.cancel()
    server._note_pool_progress(cancelled)
    assert hang_restart.progressed_within(60.0) is False


def test_an_op_that_raised_inside_a_live_pool_is_still_progress() -> None:
    failed: concurrent.futures.Future = concurrent.futures.Future()
    failed.set_exception(ValueError("op error"))
    with pytest.raises(ValueError):
        server._await_pool_result(failed, 5.0, None)
    assert hang_restart.progressed_within(60.0) is True


def test_queued_result_phase_accept_stalls_do_not_restart_while_the_pool_completes(tmp_path: Path, exits: list) -> None:
    stall = _stall_dir(tmp_path)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_result({})
    server._await_pool_result(fut, 5.0, None)
    _fire_result_phase_accept_stalls(stall, 14)
    assert exits == []
    assert hang_restart.maybe_restart(stall) == "suppressed"
    assert "action=suppressed reason=progress" in _log(stall)


def test_queued_result_phase_accept_stalls_restart_when_the_pool_is_frozen(tmp_path: Path, exits: list) -> None:
    stall = _stall_dir(tmp_path)
    _fire_result_phase_accept_stalls(stall, 14)
    assert len(exits) == 1
    assert "action=restart" in _log(stall)


def test_a_worker_stall_at_threshold_leaves_the_restart_to_the_server(tmp_path: Path, exits: list, monkeypatch: pytest.MonkeyPatch) -> None:
    stall = _stall_dir(tmp_path)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall)
    _write_dumps(stall, hang_restart.HANG_DUMP_THRESHOLD)
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
        server._note_stall_in_memory(stall)
    server._restart_check(stall)
    assert exits == []
    assert _log(stall) == ""

    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)
    server._restart_check(stall)
    assert len(exits) == 1


def test_a_server_stall_at_threshold_while_the_pool_progresses_is_suppressed(tmp_path: Path, exits: list) -> None:
    stall = _stall_dir(tmp_path)
    hang_restart.note_progress()
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
        server._note_stall_in_memory(stall)
    assert exits == []


def test_client_decide_is_not_gated_on_the_server_counter(tmp_path: Path) -> None:
    svc = tmp_path / "svc"
    svc.mkdir()
    hang_restart.note_progress()
    assert hang_restart.decide(svc, server_pid=1, source="client-unanswering") == "restart"


def test_progress_stops_excusing_a_stall_episode_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-22 22:52-22:57Z: guard ops kept completing while accept stalled, so every burst read
    as progress and the wedged server lived 5 minutes. Past the grace, the burst restarts it."""
    monkeypatch.setattr(hang_restart, "_progress_excused_since", {})
    clock = [1000.0]
    monkeypatch.setattr(hang_restart.time, "monotonic", lambda: clock[0])
    stall = _stall_dir(tmp_path)
    exits: list = []

    def burst() -> str:
        hang_restart.note_progress()
        verdict = None
        for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
            verdict = hang_restart.note_stall(stall, exit_fn=exits.append)
        return verdict

    assert burst() == "suppressed"
    clock[0] += hang_restart.PROGRESS_GRACE_SECS / 2
    assert burst() == "suppressed"
    assert exits == []
    clock[0] += hang_restart.PROGRESS_GRACE_SECS
    assert burst() == "restart"
    assert len(exits) == 1


def test_a_new_stall_episode_gets_a_fresh_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hang_restart, "_progress_excused_since", {})
    clock = [1000.0]
    monkeypatch.setattr(hang_restart.time, "monotonic", lambda: clock[0])
    stall = _stall_dir(tmp_path)
    exits: list = []

    def burst() -> str:
        hang_restart.note_progress()
        verdict = None
        for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
            verdict = hang_restart.note_stall(stall, exit_fn=exits.append)
        return verdict

    assert burst() == "suppressed"
    clock[0] += hang_restart.HANG_WINDOW_SECS * 3
    assert burst() == "suppressed"
    assert exits == []

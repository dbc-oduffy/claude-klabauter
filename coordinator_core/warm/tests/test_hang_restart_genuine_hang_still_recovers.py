"""A genuinely hung (non-io-wait) server is still detected, ended and restarted; io-wait records never kill.

Drives `hang_restart.maybe_restart` / `note_stall` on stall records under `tmp_path` with a
recording `exit_fn`, and a real `server._arm_stall_dump` watcher on a handler that sleeps with no IO.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import hang_restart, server

pytestmark = pytest.mark.complete

HUNG_HEADER = "stall-class: hung"


@pytest.fixture
def stall(tmp_path: Path) -> Path:
    path = tmp_path / "svc" / "stall-dumps"
    path.mkdir(parents=True)
    return path


def _write(stall: Path, count: int, header: str, at: float, tag: str = "a") -> None:
    for i in range(count):
        stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(at - i))
        (stall / f"{stamp}-{tag}{i}.txt").write_text(f"{header}\n", encoding="utf-8")


def _log(stall: Path) -> str:
    path = stall.parent / hang_restart.HANG_RESTART_LOG_FILENAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_hung_records_inside_the_window_end_the_server_and_log_one_restart(stall: Path) -> None:
    now = time.time()
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD, HUNG_HEADER, now)
    exits: list = []
    assert hang_restart.maybe_restart(stall, server_pid=4242, now=now, exit_fn=exits.append) == "restart"
    assert exits == [4242]
    assert _log(stall).count("action=restart") == 1


def test_records_below_the_threshold_do_not_end_the_server(stall: Path) -> None:
    now = time.time()
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD - 1, HUNG_HEADER, now)
    exits: list = []
    assert hang_restart.maybe_restart(stall, now=now, exit_fn=exits.append) is None
    assert exits == []


def test_hung_records_older_than_the_window_do_not_end_the_server(stall: Path) -> None:
    now = time.time()
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD, HUNG_HEADER, now - hang_restart.HANG_WINDOW_SECS - 30)
    exits: list = []
    assert hang_restart.maybe_restart(stall, now=now, exit_fn=exits.append) is None
    assert exits == []


def test_the_same_count_of_io_wait_records_never_ends_the_server(stall: Path) -> None:
    now = time.time()
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD * 3, hang_restart.IO_WAIT_HEADER, now)
    exits: list = []
    assert hang_restart.maybe_restart(stall, now=now, exit_fn=exits.append) == "io-stall"
    assert exits == []
    assert "action=restart" not in _log(stall)


def test_io_wait_records_do_not_dilute_a_real_hang_into_silence(stall: Path) -> None:
    now = time.time()
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD * 2, hang_restart.IO_WAIT_HEADER, now, "io")
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD, HUNG_HEADER, now, "hung")
    exits: list = []
    assert hang_restart.maybe_restart(stall, server_pid=7, now=now, exit_fn=exits.append) == "restart"
    assert exits == [7]


def test_a_second_hang_inside_the_backoff_is_suppressed_and_one_after_it_restarts(stall: Path) -> None:
    now = time.time()
    exits: list = []
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD, HUNG_HEADER, now, "first")
    assert hang_restart.maybe_restart(stall, now=now, exit_fn=exits.append) == "restart"

    soon = now + hang_restart.RESTART_MIN_INTERVAL_SECS / 3
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD, HUNG_HEADER, soon, "soon")
    assert hang_restart.maybe_restart(stall, now=soon, exit_fn=exits.append) == "suppressed"
    assert len(exits) == 1
    assert "reason=backoff" in _log(stall)

    later = now + hang_restart.RESTART_MIN_INTERVAL_SECS + 5
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD, HUNG_HEADER, later, "later")
    assert hang_restart.maybe_restart(stall, now=later, exit_fn=exits.append) == "restart"
    assert len(exits) == 2


def test_the_backoff_wait_never_exceeds_its_cap() -> None:
    now = time.time()
    lines = [
        f"{time.strftime(hang_restart._LINE_STAMP_FMT, time.gmtime(now - 10 * (12 - i)))} pid=1 action=restart count=8"
        for i in range(12)
    ]
    assert hang_restart._backoff_secs(lines, now, hang_restart.RESTART_MIN_INTERVAL_SECS) == (
        hang_restart.RESTART_BACKOFF_CAP_SECS
    )


def test_in_memory_marks_at_the_threshold_end_the_server(stall: Path) -> None:
    exits: list = []
    verdicts = [
        hang_restart.note_stall(stall, server_pid=99, exit_fn=exits.append)
        for _ in range(hang_restart.HANG_DUMP_THRESHOLD)
    ]
    assert verdicts[:-1] == [None] * (hang_restart.HANG_DUMP_THRESHOLD - 1)
    assert verdicts[-1] == "restart"
    assert exits == [99]
    assert _log(stall).count("action=restart") == 1


def test_a_client_requested_restart_of_a_hung_engine_is_backed_off_then_allowed(stall: Path) -> None:
    svc = stall.parent
    now = time.time()
    assert hang_restart.decide(svc, server_pid=5, source="client-unanswering", now=now) == "restart"
    soon = now + hang_restart.RESTART_MIN_INTERVAL_SECS / 3
    assert hang_restart.decide(svc, server_pid=5, source="client-unanswering", now=soon) == "suppressed"
    later = now + hang_restart.RESTART_MIN_INTERVAL_SECS + 5
    assert hang_restart.decide(svc, server_pid=5, source="client-unanswering", now=later) == "restart"


@pytest.mark.parametrize("blocker", ["sleep", "lock"])
def test_a_real_watcher_on_a_sleeping_handler_writes_a_hung_record_and_counts_it(
    stall: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    held = threading.Lock()
    if blocker == "lock":
        held.acquire()
        threading.Thread(target=lambda: (held.acquire(), held.release()), daemon=True).start()
    counted: list = []
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall)
    monkeypatch.setattr(server, "_op_blocked_on_read", lambda _ident: False)
    monkeypatch.setattr(server, "_holder_drive_probe_ms", lambda _root: 0.0)
    monkeypatch.setattr(server, "_note_stall_in_memory", lambda _dir: counted.append("counted"))
    claims = tmp_path / "claims"
    claims.mkdir()
    release = threading.Event()
    disarm = server._arm_stall_dump(0.05, claims / "w", tmp_path)
    try:
        deadline = time.monotonic() + 10.0
        while (not list(stall.glob("*.txt")) or not counted) and time.monotonic() < deadline:
            release.wait(0.02)
    finally:
        disarm()
        if blocker == "lock":
            held.release()
    records = list(stall.glob("*.txt"))
    assert records
    assert all(hang_restart._is_io_wait_record(p) is False for p in records)
    assert counted

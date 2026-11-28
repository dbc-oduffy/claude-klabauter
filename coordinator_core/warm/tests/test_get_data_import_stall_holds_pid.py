"""A pool blocked in importlib `get_data` under the op-module prewarm holds one pid; a genuine hang still restarts.

Drives `note_stall` against `tmp_path` stall records and `_prewarm_noop` against a blocked op-module import.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core.ops import _registry_map
from coordinator_core.warm import hang_restart, server

pytestmark = pytest.mark.complete

_GET_DATA_STACK = (
    "Thread 0x0001 (most recent call first):\n"
    '  File "<frozen importlib._bootstrap_external>", line 1190 in get_data\n'
    '  File "<frozen importlib._bootstrap_external>", line 1131 in get_code\n'
    '  File "coordinator_core/warm/server.py", line 3191 in _prewarm_op_modules\n'
)
_USER_GET_DATA_STACK = (
    "Thread 0x0001 (most recent call first):\n"
    '  File "pkgutil.py", line 400 in get_data\n'
    '  File "coordinator_core/ops/some_op.py", line 10 in run\n'
)
_HUNG_STACK = (
    "Thread 0x0001 (most recent call first):\n"
    '  File "threading.py", line 359 in wait\n'
    '  File "coordinator_core/ops/some_op.py", line 10 in run\n'
)
PID = 7280


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hang_restart, "_stall_marks", {})
    monkeypatch.setattr(hang_restart, "_first_suppress", {})
    monkeypatch.setattr(hang_restart, "_progress_last", None)
    monkeypatch.setattr(hang_restart, "_progress_excused_since", {})


def _seed(stall_dir: Path, body: str, count: int = hang_restart.HANG_DUMP_THRESHOLD) -> None:
    stall_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for i in range(count):
        stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(now - 10 + i))
        (stall_dir / f"{stamp}-{PID}-{i}.txt").write_text(
            "engine-stamp: test\nsource-sha: unknown\nopen-path: none\nsinks: []\nleg: \n--- threads ---\n" + body,
            encoding="utf-8",
            newline="\n",
        )


def _burst(stall_dir: Path, exits: list) -> str | None:
    out = None
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
        out = hang_restart.note_stall(stall_dir, server_pid=PID, exit_fn=exits.append, deadline=5.0)
    return out


def test_get_data_top_classifies_as_io_wait(tmp_path: Path) -> None:
    _seed(tmp_path, _GET_DATA_STACK, count=1)
    (record,) = tmp_path.glob("*.txt")
    assert hang_restart._is_io_wait_record(record)


def test_a_get_data_outside_importlib_stays_hung(tmp_path: Path) -> None:
    _seed(tmp_path, _USER_GET_DATA_STACK, count=1)
    (record,) = tmp_path.glob("*.txt")
    assert not hang_restart._is_io_wait_record(record)


def test_slowed_import_bursts_hold_one_pid_with_zero_respawns(tmp_path: Path) -> None:
    stall_dir = tmp_path / "svc" / "stalls"
    _seed(stall_dir, _GET_DATA_STACK)
    exits: list = []
    outcomes = [_burst(stall_dir, exits) for _ in range(4)]
    assert outcomes == ["suppressed"] * 4
    assert exits == []
    log = (tmp_path / "svc" / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")
    assert f"reason={hang_restart.IO_HELD_REASON}" in log and "action=restart" not in log


def test_an_io_held_chain_past_the_defer_bound_restarts_and_inside_it_suppresses(tmp_path: Path) -> None:
    svc = tmp_path / "svc"
    svc.mkdir()
    now = time.time()
    first = now - hang_restart.IO_SATURATED_MAX_DEFER_SECS - 1
    hang_restart._append(svc / hang_restart.HANG_RESTART_LOG_FILENAME, first, PID, "suppressed reason=io-held", 3, 60.0, [])
    lines = hang_restart._tail_lines(svc / hang_restart.HANG_RESTART_LOG_FILENAME)

    def decide_at(at: float) -> str:
        return hang_restart._record_decision(svc, PID, at, 3, 60.0, 0.0, [], None, lines, io_held=True)

    assert decide_at(first + 1) == "suppressed"
    assert decide_at(now) == "restart"


def test_a_genuine_hang_window_still_restarts(tmp_path: Path) -> None:
    stall_dir = tmp_path / "svc" / "stalls"
    _seed(stall_dir, _HUNG_STACK)
    exits: list = []
    assert _burst(stall_dir, exits) == "restart"
    assert exits == [PID]


def test_a_window_with_more_hung_than_held_records_still_restarts(tmp_path: Path) -> None:
    stall_dir = tmp_path / "svc" / "stalls"
    _seed(stall_dir, _GET_DATA_STACK, count=2)
    stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(time.time() - 5))
    for i in range(5):
        (stall_dir / f"{stamp}-{PID}-hung{i}.txt").write_text(
            "engine-stamp: test\n--- threads ---\n" + _HUNG_STACK, encoding="utf-8", newline="\n"
        )
    exits: list = []
    assert _burst(stall_dir, exits) == "restart"
    assert exits == [PID]


def test_prewarm_noop_returns_while_the_op_module_import_is_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    entered = threading.Event()

    def slow_import() -> None:
        entered.set()
        release.wait(30)

    monkeypatch.setattr(server, "_prewarm_op_modules", slow_import)
    monkeypatch.setattr(server, "_prewarm_guard_bodies", lambda: None)
    monkeypatch.setattr(_registry_map, "OP_MODULE_MAP", {})
    monkeypatch.setattr(server, "_op_prewarm_thread", None)
    try:
        started = time.monotonic()
        server._prewarm_noop()
        elapsed = time.monotonic() - started
        assert entered.wait(5)
        assert elapsed < 2.0
        server._prewarm_noop()
        assert threading.enumerate().count(server._op_prewarm_thread) == 1
    finally:
        release.set()

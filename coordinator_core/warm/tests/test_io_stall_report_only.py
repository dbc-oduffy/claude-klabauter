"""Blocked file-read and git pack-read stalls are io-wait: logged as `io-stall`, never a restart.

Drives `server._leaf_blocked_on_read` on real frames and `hang_restart.maybe_restart` on stall
records under `tmp_path`.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import hang_restart, server

pytestmark = pytest.mark.complete


def read_bytes():
    return sys._getframe()


def compute():
    return sys._getframe()


def read():
    return sys._getframe()


class _Child:
    def __init__(self, args):
        self.args = args

    def communicate(self):
        return sys._getframe()


def _write(stall: Path, count: int, header: str) -> None:
    now = time.time()
    for i in range(count):
        stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(now - i))
        (stall / f"{stamp}-{i}.txt").write_text(f"{header}\n", encoding="utf-8")


@pytest.fixture
def stall(tmp_path: Path) -> Path:
    path = tmp_path / "svc" / "stall-dumps"
    path.mkdir(parents=True)
    return path


def test_a_read_bytes_leaf_and_a_git_child_wait_classify_blocked_on_read() -> None:
    assert server._leaf_blocked_on_read(read_bytes()) is True
    assert server._leaf_blocked_on_read(_Child(["git", "cat-file", "--batch"]).communicate()) is True


def test_compute_and_a_non_git_child_wait_do_not_classify_blocked_on_read() -> None:
    assert server._leaf_blocked_on_read(compute()) is False
    assert server._leaf_blocked_on_read(read()) is False
    assert server._leaf_blocked_on_read(_Child(["python", "-c", "pass"]).communicate()) is False


def test_io_wait_records_alone_log_io_stall_and_never_restart(stall: Path) -> None:
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD, hang_restart.IO_WAIT_HEADER)
    exits: list = []
    assert hang_restart.maybe_restart(stall, exit_fn=exits.append) == "io-stall"
    assert hang_restart.maybe_restart(stall, exit_fn=exits.append) == "io-stall"
    log = (stall.parent / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")
    assert exits == []
    assert log.count("action=io-stall") == 1
    assert "action=restart" not in log


@pytest.mark.parametrize("blocked, counted", [(True, []), (False, ["counted"])])
def test_the_worker_stall_watcher_counts_a_stall_only_when_the_op_is_not_blocked_on_a_read(
    stall: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: bool, counted: list
) -> None:
    seen: list = []
    classified = threading.Event()

    def _not_slow_drive(*_a, **_k) -> bool:
        classified.set()
        return False

    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall)
    monkeypatch.setattr(server, "_op_blocked_on_read", lambda _ident: blocked)
    monkeypatch.setattr(server, "_is_io_wait", _not_slow_drive)
    monkeypatch.setattr(server, "_holder_drive_probe_ms", lambda _root: 0.0)
    monkeypatch.setattr(server, "_note_stall_in_memory", lambda _dir: seen.append("counted"))
    disarm = server._arm_stall_dump(0.01, None, tmp_path)
    try:
        deadline = time.monotonic() + 5.0
        while not seen and not classified.is_set() and not blocked and time.monotonic() < deadline:
            time.sleep(0.01)
        if blocked:
            time.sleep(0.2)
    finally:
        disarm()
    assert seen == counted


@pytest.mark.parametrize("blocked, expect_io_wait", [(True, True), (False, False)])
def test_a_workers_stall_watcher_classes_a_blocked_read_io_wait(
    stall: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: bool, expect_io_wait: bool
) -> None:
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall)
    monkeypatch.setattr(server, "_op_blocked_on_read", lambda _ident: blocked)
    monkeypatch.setattr(server, "_holder_drive_probe_ms", lambda _root: 1.0)
    claims = tmp_path / "claims"
    claims.mkdir()
    disarm = server._arm_stall_dump(0.05, claims / "w", tmp_path)
    deadline = time.monotonic() + 10.0
    while not list(stall.glob("*.txt")) and time.monotonic() < deadline:
        time.sleep(0.02)
    disarm()
    records = list(stall.glob("*.txt"))
    assert records
    assert all(hang_restart._is_io_wait_record(p) is expect_io_wait for p in records)


def test_hung_records_at_the_same_count_still_restart(stall: Path) -> None:
    _write(stall, hang_restart.HANG_DUMP_THRESHOLD, "stall-class: hung")
    exits: list = []
    assert hang_restart.maybe_restart(stall, exit_fn=exits.append) == "restart"
    assert len(exits) == 1

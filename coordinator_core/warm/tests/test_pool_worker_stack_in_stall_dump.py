"""A full stall record carries every live pool worker's own stack, so a wedged worker is evidence,
not an absence.

Drives the real `_worker_process_init` in a real `ProcessPoolExecutor` worker and the real
`_write_stall_record`; only the blocked task is a module-level stand-in the spawned worker can import.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import time
import weakref
from pathlib import Path

import pytest

from coordinator_core.warm import server

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _isolated_pool_registry(monkeypatch) -> None:
    monkeypatch.setattr(server, "_STACK_POOLS", weakref.WeakSet())


def _blocked_in_stand_in_op(pid_file: str, stop_file: str) -> None:
    """Module-level so the spawned worker can unpickle it."""
    import os

    Path(pid_file).write_text(str(os.getpid()))
    while not Path(stop_file).exists():
        time.sleep(0.05)


def _wait_for(path: Path, secs: float = 30.0) -> str:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def _record_text(record: Path | None) -> str:
    assert record is not None
    return record.read_text(encoding="utf-8")


def test_full_stall_record_names_the_blocked_worker_and_its_frame(tmp_path: Path) -> None:
    stall_dir = tmp_path / "stall-dumps"
    pid_file, stop_file = tmp_path / "pid", tmp_path / "stop"
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context(),
        initializer=server._worker_process_init,
        initargs=(str(tmp_path / "stderr"), str(stall_dir)),
    )
    try:
        future = pool.submit(_blocked_in_stand_in_op, str(pid_file), str(stop_file))
        worker_pid = int(_wait_for(pid_file))
        server._register_stack_pool(pool)

        record = server._write_stall_record(stall_dir, None, key="wedge", full=True, sample_box=False)

        text = _record_text(record)
        assert "--- pool workers ---" in text
        block = text.split("--- pool workers ---", 1)[1]
        assert f"worker-pid: {worker_pid}" in block
        assert "_blocked_in_stand_in_op" in block
        assert "no answer within" not in block
    finally:
        stop_file.write_text("stop")
        pool.shutdown(wait=True, cancel_futures=True)
    future.result(timeout=30)


def _holding_the_gil_in_a_c_call(pid_file: str, stop_file: str) -> None:
    """Module-level so the spawned worker can unpickle it; `PyDLL` keeps the GIL through the call."""
    import ctypes
    import os

    Path(pid_file).write_text(str(os.getpid()))
    ctypes.PyDLL("kernel32").Sleep(12000)


def test_answered_stack_names_the_worker_once(tmp_path: Path) -> None:
    stall_dir = tmp_path / "stall-dumps"
    pid_file, stop_file = tmp_path / "pid", tmp_path / "stop"
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context(),
        initializer=server._worker_process_init,
        initargs=(str(tmp_path / "stderr"), str(stall_dir)),
    )
    try:
        pool.submit(_blocked_in_stand_in_op, str(pid_file), str(stop_file))
        worker_pid = int(_wait_for(pid_file))
        server._register_stack_pool(pool)
        block = server._collect_worker_stacks(stall_dir, wait=5.0)
        assert block.count(f"worker-pid: {worker_pid}") == 1
        assert "_blocked_in_stand_in_op" in block
    finally:
        stop_file.write_text("stop")
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.skipif(not hasattr(__import__("ctypes"), "WinDLL"), reason="Windows watchdog path")
def test_worker_holding_the_gil_yields_a_gil_free_watchdog_dump(tmp_path: Path) -> None:
    stall_dir = tmp_path / "stall-dumps"
    pid_file, stop_file = tmp_path / "pid", tmp_path / "stop"
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context(),
        initializer=server._worker_process_init,
        initargs=(str(tmp_path / "stderr"), str(stall_dir)),
    )
    try:
        pool.submit(_holding_the_gil_in_a_c_call, str(pid_file), str(stop_file))
        worker_pid = int(_wait_for(pid_file))
        server._register_stack_pool(pool)

        deadline = time.monotonic() + 9.0
        block = ""
        while time.monotonic() < deadline:
            block = server._collect_worker_stacks(stall_dir, wait=0.2)
            if "_holding_the_gil_in_a_c_call" in block:
                break
            time.sleep(0.5)

        assert f"worker-pid: {worker_pid}" in block
        assert "no answer within 0.20s" in block
        assert "_holding_the_gil_in_a_c_call" in block
    finally:
        procs = list((pool._processes or {}).values())
        pool.shutdown(wait=False, cancel_futures=True)
        for proc in procs:
            proc.terminate()


def test_stub_record_pulls_no_worker_stacks(tmp_path: Path) -> None:
    stall_dir = tmp_path / "stall-dumps"
    record = server._write_stall_record(stall_dir, None, key="stub", full=False, sample_box=False)
    assert "--- pool workers ---" not in _record_text(record)


class _AliveProc:
    def is_alive(self) -> bool:
        return True


class _FakePool:
    def __init__(self, pids) -> None:
        self._processes = {pid: _AliveProc() for pid in pids}


def test_worker_without_a_stack_file_is_named_not_armed(tmp_path: Path) -> None:
    pool = _FakePool([424242])
    server._register_stack_pool(pool)
    block = server._collect_worker_stacks(tmp_path, wait=0.05)
    assert "worker-pid: 424242" in block
    assert "never armed" in block


def test_worker_that_never_answers_is_named_within_the_wait(tmp_path: Path, monkeypatch) -> None:
    pool = _FakePool([515151])
    server._register_stack_pool(pool)
    path = server._worker_stack_path(tmp_path, 515151)
    path.parent.mkdir(parents=True)
    path.write_text("")
    monkeypatch.setattr(server, "_signal_worker_stack", lambda pid: True)

    started = time.monotonic()
    block = server._collect_worker_stacks(tmp_path, wait=0.1)

    assert "worker-pid: 515151" in block
    assert "no answer within 0.10s" in block
    assert time.monotonic() - started < 1.0


def test_stack_file_of_a_worker_no_live_pool_holds_is_removed(tmp_path: Path) -> None:
    stale = server._worker_stack_path(tmp_path, 7)
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    server._collect_worker_stacks(tmp_path, wait=0.01)
    assert not stale.exists()

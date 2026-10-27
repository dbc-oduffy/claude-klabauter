"""Windows measurement jobs kill their tree on close, and a hung child makes
the spawn raise within a bounded timeout instead of waiting forever."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

import pytest

from coordinator_core.benchmarks import process_time

pytestmark = [
    pytest.mark.skipif(not process_time.IS_WINDOWS, reason="Windows job-object behaviour"),
    pytest.mark.spawns_process,
]

_HUNG_CHILD = (
    "import pathlib, sys, time;"
    "pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()));"
    "time.sleep(3600)"
)
_TIMEOUT_S = 3.0
_RAISE_BOUND_S = 30.0
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _is_running(pid: int) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _wait_for_pid_file(path, deadline_s: float = 20.0) -> int:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.05)
    raise AssertionError(f"child never wrote {path}")


def _wait_gone(pid: int, deadline_s: float = 10.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if not _is_running(pid):
            return True
        time.sleep(0.05)
    return not _is_running(pid)


def _limit_flags(job) -> int:
    info = process_time._JobObjectExtendedLimitInformation()
    ok = process_time._k32.QueryInformationJobObject(
        wintypes.HANDLE(job),
        process_time._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
        None,
    )
    assert ok, ctypes.WinError(ctypes.get_last_error())
    return int(info.BasicLimitInformation.LimitFlags)


def test_created_job_carries_kill_on_close():
    job = process_time._windows_create_kill_on_close_job()
    try:
        flags = _limit_flags(job)
    finally:
        process_time._k32.CloseHandle(wintypes.HANDLE(job))
    assert flags & process_time._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_job_without_the_flag_is_distinguishable():
    job = process_time._k32.CreateJobObjectW(None, None)
    try:
        flags = _limit_flags(job)
    finally:
        process_time._k32.CloseHandle(wintypes.HANDLE(job))
    assert not flags & process_time._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_closing_the_job_handle_kills_a_live_member(tmp_path):
    pid_file = tmp_path / "pid.txt"
    job = process_time._windows_create_kill_on_close_job()
    proc = subprocess.Popen(
        [sys.executable, "-c", _HUNG_CHILD, str(pid_file)],
        creationflags=process_time._CREATE_NO_WINDOW,
    )
    try:
        assert process_time._k32.AssignProcessToJobObject(
            wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))
        )
        pid = _wait_for_pid_file(pid_file)
        assert _is_running(pid)
        process_time._k32.CloseHandle(wintypes.HANDLE(job))
        job = None
        assert _wait_gone(pid), "member survived the job handle closing"
    finally:
        if job is not None:
            process_time._k32.CloseHandle(wintypes.HANDLE(job))
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.parametrize("entry", ["batched", "single_invocation"])
def test_hung_child_raises_within_bound_and_is_killed(entry, tmp_path, monkeypatch):
    monkeypatch.setattr(process_time, "SPAWN_WAIT_TIMEOUT_S", _TIMEOUT_S)
    pid_file = tmp_path / "pid.txt"
    cmd = [sys.executable, "-c", _HUNG_CHILD, str(pid_file)]

    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            if entry == "batched":
                process_time.batched_process_time_ms(cmd, k=1)
            else:
                process_time.single_invocation_tree_process_time(cmd)
        elapsed = time.monotonic() - t0
    finally:
        pid = int(pid_file.read_text()) if pid_file.exists() else None
        survived = pid is not None and not _wait_gone(pid)
        if survived:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                creationflags=process_time._CREATE_NO_WINDOW,
            )

    assert elapsed < _RAISE_BOUND_S
    assert pid is not None, "child never started, so the hang was not exercised"
    assert not survived, "hung child still running after the measurement raised"

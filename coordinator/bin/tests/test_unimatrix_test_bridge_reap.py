"""A process a test spawns through subprocess.Popen is gone after the test's teardown."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

_PARENT_SRC = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _load_conftest():
    spec = importlib.util.spec_from_file_location("_conftest_under_test", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def test_spawned_child_and_grandchild_die_on_reap(monkeypatch):
    reaper = _load_conftest()._ChildReaper()
    reaper.install(monkeypatch)
    child = subprocess.Popen(
        [sys.executable, "-c", _PARENT_SRC],
        stdout=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    grandchild_pid = int(child.stdout.readline())
    assert _pid_alive(child.pid) and _pid_alive(grandchild_pid)

    reaper.reap()

    assert child.poll() is not None
    assert _wait_dead(child.pid)
    assert _wait_dead(grandchild_pid)
    child.stdout.close()


def test_reap_leaves_an_already_exited_child_alone(monkeypatch):
    reaper = _load_conftest()._ChildReaper()
    reaper.install(monkeypatch)
    child = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    child.wait(timeout=30)
    reaper.reap()
    assert child.returncode == 0


def test_autouse_fixture_wraps_popen_for_this_test():
    assert subprocess.Popen.__name__ == "_Reaped"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX leads a new process group")
def test_posix_child_leads_its_own_process_group(monkeypatch):
    import os

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(child.pid) == child.pid != os.getpgid(0)
    finally:
        child.kill()
        child.wait(timeout=10)

"""Tests for the spawn-free verified kill / command-line primitives."""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.bash_guards import _process_kill as pk
from coordinator_core.bash_guards import _taskstop_contract as contract

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows ctypes primitives")


class _Kernel:
    def __init__(self, terminate=True, wait=0):
        self.terminated = 0
        self.closed = 0
        self._terminate, self._wait = terminate, wait

    def TerminateProcess(self, h, code):
        self.terminated += 1
        return self._terminate

    def WaitForSingleObject(self, h, ms):
        return self._wait

    def CloseHandle(self, h):
        self.closed += 1


def _stub(monkeypatch, kernel, handle=1234, err=0, ctime=77):
    monkeypatch.setattr(pk, "_is_windows", lambda: True)
    monkeypatch.setattr(pk, "_kernel", lambda: kernel)
    monkeypatch.setattr(pk._hp, "_win_open", lambda pid, access: (handle, err))
    monkeypatch.setattr(pk._hp, "_win_ctime_of", lambda h: ctime)


def test_ctime_mismatch_does_not_terminate(monkeypatch):
    k = _Kernel()
    _stub(monkeypatch, k, ctime=77)
    assert pk.terminate_verified(1, 78) is False
    assert k.terminated == 0 and k.closed == 1


def test_access_denied_and_gone_pid_return_false(monkeypatch):
    for err in (5, 87):
        k = _Kernel()
        _stub(monkeypatch, k, handle=0, err=err)
        assert pk.terminate_verified(1, 77) is False
        assert pk.command_line(1, 77) is None
        assert k.terminated == 0


def test_verified_kill_confirms_death(monkeypatch):
    k = _Kernel()
    _stub(monkeypatch, k)
    assert pk.terminate_verified(1, 77) is True
    assert k.terminated == 1 and k.closed == 1


def test_wait_timeout_or_terminate_failure_is_false(monkeypatch):
    _stub(monkeypatch, _Kernel(wait=0x102))
    assert pk.terminate_verified(1, 77) is False
    _stub(monkeypatch, _Kernel(terminate=False))
    assert pk.terminate_verified(1, 77) is False


def test_exception_never_propagates(monkeypatch):
    _stub(monkeypatch, _Kernel())
    monkeypatch.setattr(pk._hp, "_win_ctime_of", lambda h: 1 / 0)
    assert pk.terminate_verified(1, 77) is False
    assert pk.command_line(1, 77) is None


def test_off_windows_is_inert(monkeypatch):
    monkeypatch.setattr(pk, "_is_windows", lambda: False)
    assert pk.terminate_verified(1, 1) is False
    assert pk.command_line(1, 1) is None


def test_satisfies_kill_primitives_protocol():
    assert isinstance(SimpleNamespace(terminate_verified=pk.terminate_verified,
                                      command_line=pk.command_line), contract.KillPrimitives)


def test_import_closure_has_no_subprocess():
    tree = ast.parse(Path(pk.__file__).read_text(encoding="utf-8"))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module.split(".")[0])
    assert not names & {"subprocess", "os", "multiprocessing"}
    hp = ast.parse(Path(pk._hp.__file__).read_text(encoding="utf-8"))
    assert "subprocess" not in {
        a.name.split(".")[0] for n in ast.walk(hp) if isinstance(n, ast.Import) for a in n.names
    }


@_win_only
def test_live_child_command_line_then_kill():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        ctime = pk._hp.creation_time(child.pid)
        assert ctime is not None
        cl = pk.command_line(child.pid, ctime)
        assert cl is not None and "time.sleep(60)" in cl
        assert pk.command_line(child.pid, ctime + 1) is None
        assert pk.terminate_verified(child.pid, ctime + 1) is False
        assert child.poll() is None
        assert pk.terminate_verified(child.pid, ctime) is True
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child.poll() is not None
        assert pk.terminate_verified(child.pid, ctime) is False
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()

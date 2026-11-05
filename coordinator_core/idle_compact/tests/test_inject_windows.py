"""inject_windows: kernel32 call order, separate Enter write, exit-code mapping."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.idle_compact import inject_windows as iw
from coordinator_core.idle_compact.contract import Injector


class FakeK32:
    def __init__(self, attach=True, handle=5, writes=(True, True), events=None):
        self.events = events if events is not None else []
        self._attach = attach
        self._handle = handle
        self._writes = list(writes)
        self.written = []

    def FreeConsole(self):
        self.events.append("FreeConsole")
        return 1

    def AttachConsole(self, pid):
        self.events.append(("AttachConsole", pid))
        return self._attach

    def CreateFileW(self, name, *rest):
        self.events.append(("CreateFileW", name))
        return self._handle

    def WriteConsoleInputW(self, handle, arr, n, written_ref):
        chars = "".join(arr[i].Event.KeyEvent.uChar for i in range(0, n, 2))
        vks = [arr[i].Event.KeyEvent.wVirtualKeyCode for i in range(n)]
        downs = [bool(arr[i].Event.KeyEvent.bKeyDown) for i in range(n)]
        self.events.append(("Write", chars))
        self.written.append((chars, vks, downs))
        return self._writes.pop(0)


def test_sequence_order_and_separate_enter():
    events = []
    k32 = FakeK32(events=events)
    code = iw.run_sequence(77, k32=k32, sleep=lambda s: events.append(("sleep", s)))
    assert code == iw.EXIT_OK
    assert events == [
        "FreeConsole",
        ("AttachConsole", 77),
        ("CreateFileW", "CONIN$"),
        ("Write", "/compact"),
        ("sleep", 0.5),
        ("Write", "\r"),
        "FreeConsole",
    ]
    text, vks, downs = k32.written[0]
    assert downs == [True, False] * len("/compact")
    assert iw.VK_RETURN not in vks
    enter, evks, _ = k32.written[1]
    assert enter == "\r" and evks == [iw.VK_RETURN, iw.VK_RETURN]


def test_attach_failure_writes_nothing():
    k32 = FakeK32(attach=False)
    assert iw.run_sequence(1, k32=k32, sleep=lambda s: None) == iw.EXIT_ATTACH_FAILED
    assert k32.written == []


def test_invalid_handle_writes_nothing():
    k32 = FakeK32(handle=-1)
    assert iw.run_sequence(1, k32=k32, sleep=lambda s: None) == iw.EXIT_OPEN_FAILED
    assert k32.written == []


def test_text_write_failure_skips_enter():
    k32 = FakeK32(writes=(False,))
    assert iw.run_sequence(1, k32=k32, sleep=lambda s: None) == iw.EXIT_WRITE_FAILED
    assert len(k32.written) == 1


def test_enter_write_failure_reported():
    k32 = FakeK32(writes=(True, False))
    assert iw.run_sequence(1, k32=k32, sleep=lambda s: None) == iw.EXIT_WRITE_FAILED


def test_main_rejects_bad_args():
    assert iw.main([]) == iw.EXIT_USAGE
    assert iw.main(["abc"]) == iw.EXIT_USAGE


class FakeChild:
    def __init__(self, code=0, hang=False):
        self.code = code
        self.hang = hang
        self.killed = False

    def wait(self, timeout=None):
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.code

    def kill(self):
        self.killed = True


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(iw.sys, "platform", "win32")


def test_injector_spawns_no_window_child_not_in_process(on_windows):
    calls = {}

    def popen(cmd, **kw):
        calls["cmd"] = cmd
        calls["kw"] = kw
        return FakeChild(0)

    inj = iw.WindowsInjector(popen=popen)
    assert isinstance(inj, Injector)
    res = inj.inject(1234)
    assert res.ok
    assert calls["cmd"] == [sys.executable, "-m", "coordinator_core.idle_compact.inject_windows", "1234"]
    assert calls["kw"]["creationflags"] & 0x08000000


@pytest.mark.parametrize("code", [iw.EXIT_ATTACH_FAILED, iw.EXIT_OPEN_FAILED, iw.EXIT_WRITE_FAILED, 99])
def test_injector_maps_failure_exit_codes(on_windows, code):
    res = iw.WindowsInjector(popen=lambda c, **k: FakeChild(code)).inject(1)
    assert not res.ok and res.reason


def test_injector_timeout_kills_child(on_windows):
    child = FakeChild(hang=True)
    res = iw.WindowsInjector(popen=lambda c, **k: child).inject(1)
    assert not res.ok and child.killed


def test_injector_non_windows_is_logged_skip(monkeypatch):
    monkeypatch.setattr(iw.sys, "platform", "linux")
    res = iw.WindowsInjector(popen=lambda c, **k: pytest.fail("spawned")).inject(1)
    assert not res.ok

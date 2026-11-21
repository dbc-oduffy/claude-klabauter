"""test_publish_background_priority — `publish.main` lowers its own process to
background CPU and I/O priority before any publish work, per platform branch,
and a failing priority call never fails the publish.

STUB-ONLY: os.nice, ctypes and the libc handle are faked; no real priority
is changed and no subprocess spawns.

Run: python -m pytest coordinator/bin/tests/test_publish_background_priority.py -q
"""

from __future__ import annotations

import importlib.util
import platform
import sys
import types
from pathlib import Path
import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "publish_background_priority_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


class _FakeLibc:
    def __init__(self) -> None:
        self.calls = []

    def syscall(self, *args) -> None:
        self.calls.append(("syscall", args))

    def setiopolicy_np(self, *args) -> None:
        self.calls.append(("setiopolicy_np", args))


def test_windows_never_enters_background_mode() -> None:
    """PROCESS_MODE_BACKGROUND_BEGIN pins a hard 32 MB working-set max; publish must not use it."""
    source = (_BIN_DIR / "publish.py").read_text(encoding="utf-8")
    assert "0x100000" not in source
    assert not hasattr(publish, "_WIN_PROCESS_MODE_BACKGROUND_BEGIN")
    assert publish._WIN_BELOW_NORMAL_PRIORITY_CLASS == 0x4000


def test_linux_niceness_and_ioprio_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    nice_calls = []
    libc = _FakeLibc()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(publish.os, "nice", lambda n: nice_calls.append(n), raising=False)
    monkeypatch.setattr(publish, "_libc", lambda: libc)
    publish.lower_process_priority_self()
    assert nice_calls == [publish._POSIX_BACKGROUND_NICE]
    assert libc.calls == [("syscall", (251, 1, 0, 3 << 13))]


def test_linux_unknown_machine_still_nices_and_skips_ioprio(monkeypatch: pytest.MonkeyPatch) -> None:
    nice_calls = []
    libc = _FakeLibc()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(publish.os, "nice", lambda n: nice_calls.append(n), raising=False)
    monkeypatch.setattr(publish, "_libc", lambda: libc)
    publish.lower_process_priority_self()
    assert nice_calls == [publish._POSIX_BACKGROUND_NICE]
    assert libc.calls == []


def test_darwin_niceness_and_io_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    nice_calls = []
    libc = _FakeLibc()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(publish.os, "nice", lambda n: nice_calls.append(n), raising=False)
    monkeypatch.setattr(publish, "_libc", lambda: libc)
    publish.lower_process_priority_self()
    assert nice_calls == [publish._POSIX_BACKGROUND_NICE]
    assert libc.calls == [("setiopolicy_np", (1, 0, 3))]


def test_failure_is_silent_on_every_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise OSError("denied")

    monkeypatch.setattr(publish.os, "nice", boom, raising=False)
    monkeypatch.setattr(publish, "_libc", boom)
    for plat in ("linux", "darwin"):
        monkeypatch.setattr(sys, "platform", plat)
        publish.lower_process_priority_self()

    kernel32 = types.SimpleNamespace(GetCurrentProcess=boom, SetPriorityClass=boom)
    monkeypatch.setitem(
        sys.modules, "ctypes", types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=kernel32))
    )
    monkeypatch.setattr(sys, "platform", "win32")
    publish.lower_process_priority_self()


def test_main_lowers_priority_before_any_publish_work(monkeypatch: pytest.MonkeyPatch) -> None:
    order = []
    monkeypatch.setattr(publish, "lower_process_priority_self", lambda: order.append("lower"))
    monkeypatch.setattr(publish, "_install_staging_signal_cleanup", lambda: (lambda: None))
    monkeypatch.setattr(publish, "_main", lambda argv: order.append("_main") or 0)
    assert publish.main([]) == 0
    assert order == ["lower", "_main"]


def test_main_still_runs_when_priority_lowering_raises_nothing_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(publish.os, "nice", lambda n: (_ for _ in ()).throw(OSError()), raising=False)
    monkeypatch.setattr(publish, "_install_staging_signal_cleanup", lambda: (lambda: None))
    monkeypatch.setattr(publish, "_main", lambda argv: 7)
    assert publish.main([]) == 7


_LIVE_CHILD_PROBE = """
import importlib.util, sys, ctypes
spec = importlib.util.spec_from_file_location('p', sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules['p'] = m
spec.loader.exec_module(m)
k = ctypes.WinDLL('kernel32'); n = ctypes.WinDLL('ntdll')
k.GetCurrentProcess.restype = ctypes.c_void_p
n.NtQueryInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
h = k.GetCurrentProcess()
def q(cls):
    v = ctypes.c_ulong(); n.NtQueryInformationProcess(h, cls, ctypes.byref(v), 4, None); return v.value
before = (q(33), q(39))
m.lower_process_priority_self()
after = (q(33), q(39))
mn = ctypes.c_size_t(); mx = ctypes.c_size_t(); fl = ctypes.c_ulong()
k.GetProcessWorkingSetSizeEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
k.GetProcessWorkingSetSizeEx(h, ctypes.byref(mn), ctypes.byref(mx), ctypes.byref(fl))
print(*before, *after, fl.value)
"""

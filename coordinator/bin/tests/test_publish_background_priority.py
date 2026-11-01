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

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
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
    def __init__(self):
        self.calls = []

    def syscall(self, *args):
        self.calls.append(("syscall", args))

    def setiopolicy_np(self, *args):
        self.calls.append(("setiopolicy_np", args))


def test_windows_sets_below_normal_then_background_mode(monkeypatch):
    calls = []
    class _Fn:
        def __init__(self, impl):
            self._impl = impl

        def __call__(self, *a):
            return self._impl(*a)

    kernel32 = types.SimpleNamespace(
        GetCurrentProcess=_Fn(lambda: "HANDLE"),
        SetPriorityClass=_Fn(lambda handle, flag: calls.append((handle, flag))),
    )
    fake_ctypes = types.SimpleNamespace(
        WinDLL=lambda name: kernel32, c_void_p="c_void_p", c_uint32="c_uint32", c_int="c_int"
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setattr(sys, "platform", "win32")
    publish.lower_process_priority_self()
    assert calls == [
        ("HANDLE", publish._WIN_BELOW_NORMAL_PRIORITY_CLASS),
        ("HANDLE", publish._WIN_PROCESS_MODE_BACKGROUND_BEGIN),
    ]
    assert publish._WIN_BELOW_NORMAL_PRIORITY_CLASS == 0x4000
    assert publish._WIN_PROCESS_MODE_BACKGROUND_BEGIN == 0x100000


def test_linux_niceness_and_ioprio_idle(monkeypatch):
    nice_calls = []
    libc = _FakeLibc()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(publish.os, "nice", lambda n: nice_calls.append(n), raising=False)
    monkeypatch.setattr(publish, "_libc", lambda: libc)
    publish.lower_process_priority_self()
    assert nice_calls == [publish._POSIX_BACKGROUND_NICE]
    assert libc.calls == [("syscall", (251, 1, 0, 3 << 13))]


def test_linux_unknown_machine_still_nices_and_skips_ioprio(monkeypatch):
    nice_calls = []
    libc = _FakeLibc()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(publish.os, "nice", lambda n: nice_calls.append(n), raising=False)
    monkeypatch.setattr(publish, "_libc", lambda: libc)
    publish.lower_process_priority_self()
    assert nice_calls == [publish._POSIX_BACKGROUND_NICE]
    assert libc.calls == []


def test_darwin_niceness_and_io_throttle(monkeypatch):
    nice_calls = []
    libc = _FakeLibc()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(publish.os, "nice", lambda n: nice_calls.append(n), raising=False)
    monkeypatch.setattr(publish, "_libc", lambda: libc)
    publish.lower_process_priority_self()
    assert nice_calls == [publish._POSIX_BACKGROUND_NICE]
    assert libc.calls == [("setiopolicy_np", (1, 0, 3))]


def test_failure_is_silent_on_every_branch(monkeypatch):
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


def test_main_lowers_priority_before_any_publish_work(monkeypatch):
    order = []
    monkeypatch.setattr(publish, "lower_process_priority_self", lambda: order.append("lower"))
    monkeypatch.setattr(publish, "_install_staging_signal_cleanup", lambda: (lambda: None))
    monkeypatch.setattr(publish, "_main", lambda argv: order.append("_main") or 0)
    assert publish.main([]) == 0
    assert order == ["lower", "_main"]


def test_main_still_runs_when_priority_lowering_raises_nothing_fatal(monkeypatch):
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
print(*before, *after)
"""


def test_windows_real_process_priority_is_lowered_in_live_child():
    import subprocess
    import pytest

    if sys.platform != "win32":
        pytest.skip("Windows priority only")
    out = subprocess.run(
        [sys.executable, "-I", "-c", _LIVE_CHILD_PROBE, str(_BIN_DIR / "publish.py")],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    io_before, page_before, io_after, page_after = (int(x) for x in out.stdout.split()[-4:])
    assert io_after < io_before, out
    assert page_after < page_before, out

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
from pathlib import Path
import pytest
from typing import TYPE_CHECKING

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def test_windows_real_process_priority_is_lowered_in_live_child() -> None:
    import subprocess
    import pytest

    if sys.platform != "win32":
        pytest.skip("Windows priority only")
    out = subprocess.run(
        [sys.executable, "-I", "-c", _LIVE_CHILD_PROBE, str(_BIN_DIR / "publish.py")],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    io_before, page_before, io_after, page_after, ws_flags = (int(x) for x in out.stdout.split()[-5:])
    assert io_after < io_before, out
    assert page_after < page_before, out
    assert not ws_flags & 0x4, f"hard working-set max (QUOTA_LIMITS_HARDWS_MAX_ENABLE) set: {out}"

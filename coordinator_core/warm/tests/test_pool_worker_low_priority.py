"""A real pool worker starts at very-low memory and I/O priority."""

from __future__ import annotations

import sys
import threading

import pytest

from coordinator_core.warm import breadcrumb, server


def query_own_priority() -> dict:
    """Runs inside the pool worker: read back its own memory and I/O priority."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32")
    ntdll = ctypes.WinDLL("ntdll")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p]
    handle = k32.GetCurrentProcess()
    mem = ctypes.c_ulong(99)
    ok = k32.GetProcessInformation(handle, 0, ctypes.byref(mem), ctypes.sizeof(mem))
    io = ctypes.c_ulong(99)
    st = ntdll.NtQueryInformationProcess(handle, 33, ctypes.byref(io), ctypes.sizeof(io), None)
    return {"mem_ok": bool(ok), "memory": mem.value, "io_status": st, "io": io.value}


def test_worker_init_lowers_priority_before_registry_preload(monkeypatch) -> None:
    calls: list[str] = []
    from coordinator_core.warm import polite_io

    monkeypatch.setattr(polite_io, "lower_own_priority", lambda: calls.append("lower") or {})
    monkeypatch.setattr(server, "_bind_null_std_streams", lambda: None)
    monkeypatch.setattr(server, "_bind_worker_stderr_log", lambda d: None)
    monkeypatch.setattr(server, "_preload_op_registry", lambda: calls.append("preload"))

    class _StubThread:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(server.threading, "Thread", _StubThread)
    server._worker_process_init()

    assert calls == ["lower", "preload"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priority readback only")
def test_real_pool_worker_reports_very_low_memory_and_io_priority(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "runtime"))
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool = None
    ctx._dispatch_pool_lock = threading.Lock()
    ctx.engine_root = tmp_path / "engine"
    pool = ctx._ensure_dispatch_pool()
    try:
        got = pool.submit(query_own_priority).result(timeout=120)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    assert got["mem_ok"], got
    assert got["memory"] == 1, got
    assert got["io_status"] == 0 and got["io"] == 0, got

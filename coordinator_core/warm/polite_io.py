"""Cache-polite I/O for warm workers: low memory/I/O priority, sequential-scan opens, read rate limit.

ctypes is imported on the first priority call, never at module import (warm-reach import ceiling).
Every function degrades to a status or a plain open; none raises on a platform refusal.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from typing import Callable, Iterator

# Win32 values, mirrored from the Windows SDK / ntifs headers.
# PROCESS_INFORMATION_CLASS value for SetProcessInformation(MemoryPriority).
_PROCESS_MEMORY_PRIORITY = 0
# NtSetInformationProcess class selecting the process I/O priority hint.
_PROCESS_IO_PRIORITY = 33
# MEMORY_PRIORITY_VERY_LOW: standby pages of this process are repurposed first.
_MEMORY_PRIORITY_VERY_LOW = 1
# IoPriorityVeryLow: this process's I/O queues behind every other priority.
_IO_PRIORITY_VERY_LOW = 0


def _lower_windows() -> dict:
    import ctypes
    from ctypes import wintypes

    class MEMORY_PRIORITY_INFORMATION(ctypes.Structure):
        _fields_ = [("MemoryPriority", wintypes.ULONG)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetProcessInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetProcessInformation.restype = wintypes.BOOL
    ntdll.NtSetInformationProcess.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]
    ntdll.NtSetInformationProcess.restype = ctypes.c_long

    handle = k32.GetCurrentProcess()  # pseudo-handle: needs no close
    info = MEMORY_PRIORITY_INFORMATION(_MEMORY_PRIORITY_VERY_LOW)
    ok_mem = bool(k32.SetProcessInformation(
        handle, _PROCESS_MEMORY_PRIORITY, ctypes.byref(info), ctypes.sizeof(info)))
    io = ctypes.c_ulong(_IO_PRIORITY_VERY_LOW)
    st_io = ntdll.NtSetInformationProcess(
        handle, _PROCESS_IO_PRIORITY, ctypes.byref(io), ctypes.sizeof(io)) & 0xFFFFFFFF
    return {"memory": ok_mem, "io": st_io == 0, "io_status": hex(st_io)}


def _lower_posix() -> dict:
    status: dict = {"memory": False, "io": False, "nice": False}
    try:
        os.setpriority(os.PRIO_PROCESS, 0, 19)
        status["nice"] = True  # CPU priority only; "memory" stays False (no POSIX page-priority lever)
    except (AttributeError, OSError) as exc:
        status["nice_error"] = repr(exc)
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            # ioprio_set(IOPRIO_WHO_PROCESS=1, who=0, class IDLE=3 << 13)
            nr = {"x86_64": 251, "aarch64": 30}.get(os.uname().machine)
            if nr is not None and libc.syscall(nr, 1, 0, 3 << 13) == 0:
                status["io"] = True
        except Exception as exc:  # best-effort; never raise into a worker
            status["ioprio_error"] = repr(exc)
    return status


def lower_own_priority() -> dict:
    """Drop this process to very-low memory and I/O priority; return a status dict, never raise."""
    try:
        status = _lower_windows() if sys.platform == "win32" else _lower_posix()
    except Exception as exc:
        return {"memory": False, "io": False, "error": repr(exc)}
    status["platform"] = sys.platform
    return status


@contextlib.contextmanager
def open_bulk(path: str | os.PathLike) -> Iterator:
    """Open `path` for a bulk sequential read as a binary file object; never mmap.

    Windows: O_SEQUENTIAL (FILE_FLAG_SEQUENTIAL_SCAN). POSIX: posix_fadvise SEQUENTIAL on
    open and DONTNEED on close so the pages do not linger in the system cache.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_SEQUENTIAL", 0)
    fd = os.open(os.fspath(path), flags)
    advise = hasattr(os, "posix_fadvise")
    try:
        if advise:
            with contextlib.suppress(OSError):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        f = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    try:
        yield f
    finally:
        try:
            if advise:
                with contextlib.suppress(OSError, ValueError):
                    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            f.close()


class ReadRateLimiter:
    """Token bucket over bytes read; a read under `threshold` bytes never waits.

    Caller: `search.engine._read_text` when `throttle=True`. One acquire sleeps at most
    `nbytes / rate`; the caller's byte budget (`engine.MAX_BYTES_SCANNED`) bounds the total.
    """

    def __init__(
        self,
        bytes_per_sec: float,
        threshold: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = float(bytes_per_sec)
        self._threshold = threshold
        self._clock = clock
        self._sleep = sleep
        self._tokens = self._rate  # one second of burst
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self, nbytes: int) -> float:
        """Charge `nbytes`, sleeping if the bucket is in debt; return seconds slept."""
        if nbytes < self._threshold or self._rate <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            self._tokens -= nbytes
            wait = -self._tokens / self._rate if self._tokens < 0 else 0.0
        if wait > 0:
            self._sleep(wait)
        return wait

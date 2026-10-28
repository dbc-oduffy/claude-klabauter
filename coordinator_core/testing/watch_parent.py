"""Stdlib-only parent-liveness watch: a thread that fires once when a watched pid dies or is reused.

A parent is alive only if its pid exists, is not a zombie, and its process create time equals the
value captured at start. Create time comes from `process_create_time` on both reads, so exact
equality holds; never mix in another clock source.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Optional, Tuple

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_FILETIME_EPOCH_DELTA_S = 11644473600.0


def _probe_windows(pid: int) -> Optional[Tuple[float, bool]]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value != _STILL_ACTIVE:
            return None
        created, exited, kern, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kern), ctypes.byref(user)
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 1e7 - _FILETIME_EPOCH_DELTA_S, False
    finally:
        k32.CloseHandle(handle)


def _probe_linux(pid: int) -> Optional[Tuple[float, bool]]:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    # comm may contain spaces/parens: split after the last ')'.
    rest = raw[raw.rfind(")") + 2 :].split()
    if len(rest) < 20:
        return None
    return float(rest[19]), rest[0] == "Z"


def _probe_darwin(pid: int) -> Optional[Tuple[float, bool]]:
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 37:
        return None
    # kinfo_proc.kp_proc: p_starttime timeval at 0 (sec int64, usec int32); p_stat byte at 36.
    sec = int.from_bytes(buf.raw[0:8], sys.byteorder, signed=True)
    usec = int.from_bytes(buf.raw[8:12], sys.byteorder, signed=True)
    if sec == 0 and usec == 0:
        return None
    return sec + usec / 1e6, buf.raw[36] == 5  # SZOMB


def _probe(pid: int) -> Optional[Tuple[float, bool]]:
    """Return (create_time, is_zombie) for a live pid, else None."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _probe_windows(pid)
    if sys.platform == "darwin":
        return _probe_darwin(pid)
    return _probe_linux(pid)


def process_create_time(pid: int) -> Optional[float]:
    """Process start time for `pid` on this platform's single clock source; None if no such pid."""
    found = _probe(pid)
    return None if found is None else found[0]


def _parent_alive(pid: int, create_time: Optional[float]) -> bool:
    found = _probe(pid)
    if found is None or found[1]:
        return False
    return create_time is None or found[0] == create_time


def watch_parent(
    pid: Optional[int] = None,
    create_time: Optional[float] = None,
    on_gone: Callable[[], None] = lambda: os._exit(137),
    interval: float = 2.0,
) -> threading.Thread:
    """Start a daemon thread that calls `on_gone()` once when the parent is gone, then stops.

    `pid` defaults to os.getppid() and `create_time` to its create time, both captured at call.
    Gone means: pid absent, zombie, or create time differs from the captured value (pid reuse).
    If the default-captured parent is already absent, on_gone fires on the first poll.
    """
    if pid is None:
        pid = os.getppid()
    if create_time is None:
        create_time = process_create_time(pid)
        if create_time is None:
            create_time = float("nan")  # never equals a live pid's time: gone on first poll
    stop = threading.Event()

    def _run() -> None:
        while not stop.wait(interval):
            if not _parent_alive(pid, create_time):
                on_gone()
                return

    thread = threading.Thread(target=_run, name=f"watch-parent-{pid}", daemon=True)
    thread.start()
    return thread

"""Heavy dispatch-pool size from measured box headroom (free RAM, CPU load, disk queue), never a constant.

`dispatch_pool_size` answers "how many heavy workers can this box hold right now". `sample_headroom`
reads the box through ctypes on Windows (no child process, no WMI, no PDH) and is cached for a short
TTL so an admission path calls it without blocking filesystem or syscall cost. Platforms without the
Windows readers report the inputs they lack as `None`; a missing input never shrinks the pool.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

#: MiB charged per heavy worker. Measured 2026-11-18: the live warm server process holds ~91 MiB working
#: set with ops loaded, and a worker running a heavy op peaks at a multiple of that; 256 MiB is the charge.
PER_WORKER_RAM_MB = 256
#: MiB never handed to workers: the rest of the box (peer sessions, editors, the OS) keeps this.
RAM_RESERVE_MB = 4096
#: CPU load fraction above which no further workers are added beyond the cores left idle.
CPU_IDLE_FLOOR = 0.10
#: Disk queue depth at which the pool starts shrinking in proportion. 2026-11-04 11:24Z: 20 workers drove
#: the E: queue to 242; above this depth the pool scales by `DISK_QUEUE_HIGH / queue`.
DISK_QUEUE_HIGH = 8
#: Seconds one `sample_headroom()` result is reused.
SAMPLE_TTL_SECS = 2.0


@dataclass(frozen=True)
class Headroom:
    """One box sample. `load` is CPU busy fraction 0..1 and `disk_queue` the in-flight I/O count of the
    drive the op reads; either is `None` when unreadable."""

    free_ram_mb: float
    ncpu: int
    load: Optional[float] = None
    disk_queue: Optional[float] = None


def dispatch_pool_size(sample: Optional[Headroom] = None) -> int:
    """Worker count the box can hold now: min(RAM slots, idle-CPU slots), shrunk by disk queue; at least 1."""
    s = sample if sample is not None else sample_headroom()
    if s.free_ram_mb == float("inf"):
        ram_slots = s.ncpu
    else:
        ram_slots = int(max(0.0, s.free_ram_mb - RAM_RESERVE_MB) // PER_WORKER_RAM_MB)
    if s.load is None:
        cpu_slots = s.ncpu
    else:
        cpu_slots = int(s.ncpu * max(0.0, 1.0 - max(0.0, min(1.0, s.load))) + 1e-9)
    size = min(ram_slots, cpu_slots, s.ncpu)
    if s.disk_queue is not None and s.disk_queue > DISK_QUEUE_HIGH:
        size = int(size * DISK_QUEUE_HIGH / s.disk_queue)
    return max(1, size)


_cache_lock = threading.Lock()
_cached: Optional[tuple] = None  # (monotonic stamp, Headroom)
_prev_cpu: Optional[tuple] = None  # (idle, total) from the previous GetSystemTimes


def _free_ram_mb() -> Optional[float]:
    try:
        from coordinator_core.warm.box_sample import _memory_mb

        return _memory_mb()[0]
    except Exception:  # noqa: BLE001 -- an unreadable input never stops admission
        return None


def _ncpu() -> int:
    from coordinator_core.warm.box_sample import _ncpu as n

    return n()


def _cpu_load() -> Optional[float]:
    """Busy fraction since the previous call (first call has no baseline and returns None)."""
    global _prev_cpu
    if sys.platform != "win32":
        try:
            return min(1.0, os.getloadavg()[0] / _ncpu())
        except (OSError, AttributeError):
            return None
    import ctypes
    from ctypes import wintypes

    idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.WinDLL("kernel32").GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
        return None

    def _v(f: "wintypes.FILETIME") -> int:
        return (f.dwHighDateTime << 32) | f.dwLowDateTime

    now = (_v(idle), _v(kern) + _v(user))  # kernel time includes idle
    prev, _prev_cpu = _prev_cpu, now
    if prev is None or now[1] <= prev[1]:
        return None
    return max(0.0, min(1.0, 1.0 - (now[0] - prev[0]) / (now[1] - prev[1])))


def _disk_queue(drive: str) -> Optional[float]:
    """`QueueDepth` of the volume `drive` (`"E"`) via IOCTL_DISK_PERFORMANCE; None when unreadable."""
    if sys.platform != "win32" or not drive:
        return None
    import ctypes
    from ctypes import wintypes

    class _DiskPerformance(ctypes.Structure):
        _fields_ = [
            ("BytesRead", ctypes.c_int64), ("BytesWritten", ctypes.c_int64),
            ("ReadTime", ctypes.c_int64), ("WriteTime", ctypes.c_int64), ("IdleTime", ctypes.c_int64),
            ("ReadCount", wintypes.DWORD), ("WriteCount", wintypes.DWORD),
            ("QueueDepth", wintypes.DWORD), ("SplitCount", wintypes.DWORD),
            ("QueryTime", ctypes.c_int64), ("StorageDeviceNumber", wintypes.DWORD),
            ("StorageManagerName", ctypes.c_wchar * 8),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW("\\\\.\\" + drive[0] + ":", 0, 3, None, 3, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        out, got = _DiskPerformance(), wintypes.DWORD(0)
        if not k32.DeviceIoControl(handle, 0x70020, None, 0, ctypes.byref(out), ctypes.sizeof(out),
                                   ctypes.byref(got), None):
            return None
        return float(out.QueueDepth)
    finally:
        k32.CloseHandle(handle)


def _op_drive() -> str:
    """Drive letter of the tree the op reads (the process cwd); empty when it has none."""
    drive = os.path.splitdrive(os.getcwd())[0]
    return drive[:1].upper()


def _read_headroom() -> Headroom:
    free = _free_ram_mb()
    return Headroom(
        free_ram_mb=free if free is not None else float("inf"),
        ncpu=_ncpu(),
        load=_cpu_load(),
        disk_queue=_disk_queue(_op_drive()),
    )


def sample_headroom(ttl: float = SAMPLE_TTL_SECS) -> Headroom:
    """Box headroom, reused for `ttl` seconds. Never raises: an unreadable input is absent, not an error."""
    global _cached
    now = time.monotonic()
    with _cache_lock:
        if _cached is not None and now - _cached[0] < ttl:
            return _cached[1]
        try:
            h = _read_headroom()
        except Exception:  # noqa: BLE001
            h = _cached[1] if _cached is not None else Headroom(free_ram_mb=float("inf"), ncpu=_ncpu())
        _cached = (now, h)
        return h

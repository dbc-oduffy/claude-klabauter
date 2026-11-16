"""Box-side sample for the full warm stall record: which process was reading, was Defender busy, was memory short.

Runs only on the stall-dump path (`server._write_stall_record`), never on a request path. Windows reads
`NtQuerySystemInformation(SystemProcessInformation)` twice across the window (per-process read bytes and
CPU time) plus `GetPerformanceInfo` (available memory, system cache) through ctypes: no child process,
no WMI, no PDH. Other platforms write one explicit `unavailable` line.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
from typing import Dict, List, Tuple

WINDOW_SECS = 2.0
#: Slack past the window before the sample counts as overrun and yields `error:timeout`.
BOUND_MARGIN_SECS = 1.5
TOP_N = 5
DEFENDER_IMAGE = "msmpeng.exe"

_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
# x64 SYSTEM_PROCESS_INFORMATION field offsets.
_OFF_CREATE, _OFF_USER, _OFF_KERNEL, _OFF_IMAGE, _OFF_PID, _OFF_READ_BYTES = 32, 40, 48, 56, 80, 232
_MIN_ENTRY = _OFF_READ_BYTES + 8

# (pid, create_time) -> (image name, read bytes, cpu 100ns ticks)
_Snapshot = Dict[Tuple[int, int], Tuple[str, int, int]]


def _platform() -> str:
    return sys.platform


def _process_snapshot() -> _Snapshot:
    """Every process's image name, cumulative read bytes and CPU ticks. Raises OSError on a failed query."""
    ntdll = ctypes.WinDLL("ntdll")
    query = ntdll.NtQuerySystemInformation
    query.restype = ctypes.c_long
    size = 1 << 19
    for _ in range(8):
        buf = ctypes.create_string_buffer(size)
        needed = ctypes.c_ulong(0)
        status = query(_SYSTEM_PROCESS_INFORMATION, buf, size, ctypes.byref(needed)) & 0xFFFFFFFF
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(needed.value, size) + (1 << 16)
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation status 0x{status:08x}")
        break
    else:
        raise OSError("NtQuerySystemInformation buffer never fit")
    raw, base = buf.raw, ctypes.addressof(buf)
    out: _Snapshot = {}
    pos = 0
    while pos + _MIN_ENTRY <= len(raw):
        nxt = struct.unpack_from("<I", raw, pos)[0]
        create, user, kernel = struct.unpack_from("<qqq", raw, pos + _OFF_CREATE)
        name_len, _max, name_ptr = struct.unpack_from("<HHxxxxQ", raw, pos + _OFF_IMAGE)
        pid = struct.unpack_from("<Q", raw, pos + _OFF_PID)[0]
        read_bytes = struct.unpack_from("<q", raw, pos + _OFF_READ_BYTES)[0]
        name = ""
        if name_len and name_ptr >= base and name_ptr - base + name_len <= len(raw):
            name = raw[name_ptr - base : name_ptr - base + name_len].decode("utf-16-le", "replace")
        out[(pid, create)] = (name or ("Idle" if pid == 0 else f"pid{pid}"), read_bytes, user + kernel)
        if not nxt:
            break
        pos += nxt
    return out


class _PerfInfo(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", ctypes.c_ulong),
        ("ProcessCount", ctypes.c_ulong),
        ("ThreadCount", ctypes.c_ulong),
    ]


def _memory_mb() -> Tuple[float, float, float]:
    """`(available, total, system_cache)` in MiB from `GetPerformanceInfo`."""
    info = _PerfInfo()
    info.cb = ctypes.sizeof(info)
    if not ctypes.WinDLL("psapi").GetPerformanceInfo(ctypes.byref(info), info.cb):
        raise OSError("GetPerformanceInfo failed")
    page = info.PageSize / (1024 * 1024)
    return info.PhysicalAvailable * page, info.PhysicalTotal * page, info.SystemCache * page


def _ncpu() -> int:
    import os

    return os.cpu_count() or 1


def _sample_windows(window: float) -> str:
    t0 = time.perf_counter()
    before = _process_snapshot()
    time.sleep(max(0.0, window - (time.perf_counter() - t0)))
    after = _process_snapshot()
    elapsed = time.perf_counter() - t0
    avail, total, cache = _memory_mb()

    rates: List[Tuple[float, int, str]] = []
    defender_ticks = 0
    defender_seen = False
    for key, (name, read_now, cpu_now) in after.items():
        _, read_then, cpu_then = before.get(key, (name, 0, 0))
        if name.lower() == DEFENDER_IMAGE:
            defender_seen = True
            defender_ticks += max(0, cpu_now - cpu_then)
        rates.append((max(0, read_now - read_then) / elapsed / (1024 * 1024), key[0], name))
    rates.sort(key=lambda r: r[0], reverse=True)
    top = " | ".join(f"{name} pid={pid} {mbps:.2f}MB/s" for mbps, pid, name in rates[:TOP_N])
    if defender_seen:
        pct = defender_ticks / 1e7 / elapsed * 100
        defender = f"{pct:.1f} (100 = one core; {_ncpu()} logical cpus)"
    else:
        defender = "absent"
    return (
        f"box-sample: ok window-s={elapsed:.3f}\n"
        f"box-io-read-top{TOP_N}: {top}\n"
        f"box-msmpeng-cpu-pct: {defender}\n"
        f"box-available-memory-mb: {avail:.0f} of {total:.0f}\n"
        f"box-file-cache-mb: {cache:.0f}\n"
    )


def sample_lines(window: float = WINDOW_SECS) -> str:
    """Header lines (`name: value\\n` each) for one box-side sample over `window` seconds."""
    if _platform() != "win32":
        return f"box-sample: unavailable (platform={_platform()})\n"
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return "box-sample: unavailable (32-bit interpreter)\n"
    return _sample_windows(window)


def box_side_lines() -> str:
    """`sample_lines()` contained: a raise yields `box-sample: error:<type>`, an overrun
    `box-sample: error:timeout`. Never raises; blocks at most the window plus the margin."""
    from coordinator_core.bounded_io import run_bounded

    window = WINDOW_SECS

    def _run() -> str:
        try:
            return sample_lines(window)
        except Exception as exc:  # noqa: BLE001 -- diagnostics never stop an op
            return f"box-sample: error:{type(exc).__name__}\n"

    try:
        return run_bounded(
            "box-sample", _run, deadline=window + BOUND_MARGIN_SECS, default="box-sample: error:timeout\n"
        )
    except Exception as exc:  # noqa: BLE001
        return f"box-sample: error:{type(exc).__name__}\n"

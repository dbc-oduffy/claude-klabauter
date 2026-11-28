"""Per-process census for the fleet job ledger: one row per process with ``pid``, ``ppid``, ``comm``,
``cmdline``, ``start`` (epoch s), ``age_s``, ``cpu_s``, ``pcpu`` (lifetime average), ``rss_mb``,
``vram_mb`` and cumulative disk IO (``io_read_bytes``, ``io_write_bytes``, ``io_bytes_per_s`` over the
process lifetime).

Windows reads through ctypes (EnumProcesses, GetProcessTimes, GetProcessIoCounters,
NtQueryInformationProcess for ppid and command line), Linux through ``/proc``: no spawn. macOS makes one
``ps`` spawn and has no IO counters. ``read`` never raises: an unreadable axis is ``None`` on the row and
named in ``unavailable`` when no row carries it.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from typing import Any, Callable, Optional

from coordinator_core.group_em import gpu_sample

_MIB = 1024 * 1024
_FILETIME_EPOCH_OFFSET = 116444736000000000
_SPAWN_TIMEOUT_S = 5
_AXES = ("cmdline", "start", "cpu_s", "rss_mb", "io_read_bytes", "io_write_bytes", "vram_mb")
_ROW_KEYS = ("pid", "ppid", "comm", "cmdline", "start", "cpu_s", "rss_mb", "io_read_bytes", "io_write_bytes")
_DARWIN_IO_REASON = "io-unavailable-on-darwin"
_NT_LENGTH_STATUSES = (0xC0000004, 0x80000005, 0xC0000023)


def _windows_rows() -> list:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    class _Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (n, ctypes.c_size_t)
            for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                      "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                      "PeakPagefileUsage")
        ]

    class _FileTime(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

        def value(self) -> int:
            return (self.hi << 32) | self.lo

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicInfo(ctypes.Structure):
        _fields_ = [("ExitStatus", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("AffinityMask", ctypes.c_void_p), ("BasePriority", ctypes.c_void_p),
                    ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p)]

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]

    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(_FileTime)] * 4
    kernel32.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IoCounters)]
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters), ctypes.c_ulong]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]

    def ppid_of(handle) -> Optional[int]:
        info = _BasicInfo()
        if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info), ctypes.sizeof(info), None) != 0:
            return None
        return int(info.InheritedFromUniqueProcessId or 0)

    def cmdline_of(handle) -> Optional[str]:
        size = 2048
        for _ in range(2):
            buf = ctypes.create_string_buffer(size)
            needed = ctypes.c_ulong()
            status = ntdll.NtQueryInformationProcess(handle, 60, buf, size, ctypes.byref(needed))
            if status == 0:
                text = ctypes.cast(buf, ctypes.POINTER(_UnicodeString)).contents
                return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
            if (status & 0xFFFFFFFF) not in _NT_LENGTH_STATUSES:
                return None
            size = max(needed.value, size * 2)
        return None

    pids = (ctypes.c_ulong * 16384)()
    got = ctypes.c_ulong()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(got)):
        raise OSError("EnumProcesses failed")
    rows = []
    for pid in pids[: got.value // ctypes.sizeof(ctypes.c_ulong)]:
        handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            row: dict = dict.fromkeys(_ROW_KEYS)
            row["pid"] = int(pid)
            path = ctypes.create_unicode_buffer(520)
            length = ctypes.c_ulong(520)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                row["comm"] = os.path.basename(path.value) or "?"
            row["ppid"] = ppid_of(handle)
            row["cmdline"] = cmdline_of(handle)
            created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
            if kernel32.GetProcessTimes(handle, created, exited, kernel, user):
                row["start"] = (created.value() - _FILETIME_EPOCH_OFFSET) / 1e7
                row["cpu_s"] = (kernel.value() + user.value()) / 1e7
            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                row["rss_mb"] = round(counters.WorkingSetSize / _MIB, 1)
            io = _IoCounters()
            if kernel32.GetProcessIoCounters(handle, ctypes.byref(io)):
                row["io_read_bytes"] = int(io.ReadTransferCount)
                row["io_write_bytes"] = int(io.WriteTransferCount)
            rows.append(row)
        finally:
            kernel32.CloseHandle(handle)
    return rows


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def _linux_rows(proc_root: str = "/proc") -> list:
    sysconf = getattr(os, "sysconf")
    tck = sysconf("SC_CLK_TCK")
    page = sysconf("SC_PAGE_SIZE")
    boot = None
    for line in (_read_text(os.path.join(proc_root, "stat")) or "").splitlines():
        if line.startswith("btime "):
            boot = float(line.split()[1])
    rows = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        base = os.path.join(proc_root, entry)
        stat = _read_text(os.path.join(base, "stat"))
        if not stat or ")" not in stat:
            continue
        row: dict = dict.fromkeys(_ROW_KEYS)
        row["pid"] = int(entry)
        head, _, tail = stat.rpartition(")")
        row["comm"] = head.partition("(")[2]
        fields = tail.split()
        try:
            row["ppid"] = int(fields[1])
            row["cpu_s"] = (int(fields[11]) + int(fields[12])) / tck
            row["rss_mb"] = round(int(fields[21]) * page / _MIB, 1)
            if boot is not None:
                row["start"] = boot + int(fields[19]) / tck
        except (IndexError, ValueError):
            pass
        cmd = _read_text(os.path.join(base, "cmdline"))
        if cmd is not None:
            row["cmdline"] = cmd.replace("\0", " ").strip()
        for line in (_read_text(os.path.join(base, "io")) or "").splitlines():
            key, _, val = line.partition(":")
            if key in ("read_bytes", "write_bytes") and val.strip().isdigit():
                row["io_" + key] = int(val)
        rows.append(row)
    return rows


def _darwin_rows() -> list:
    done = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,lstart=,pcpu=,rss=,command="], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=_SPAWN_TIMEOUT_S)
    if done.returncode != 0:
        raise OSError("ps exited %d" % done.returncode)
    return _parse_darwin_ps(done.stdout.decode("utf-8", "replace"))


def _parse_darwin_ps(text: str, now: Optional[float] = None) -> list:
    """Rows from ``ps -o pid=,ppid=,lstart=,pcpu=,rss=,command=`` output; ``lstart`` is five tokens."""
    now = time.time() if now is None else now
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 9)
        if len(parts) < 9 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        row: dict = dict.fromkeys(_ROW_KEYS)
        row["pid"], row["ppid"] = int(parts[0]), int(parts[1])
        cmd = parts[9] if len(parts) > 9 else ""
        row["cmdline"] = cmd.strip()
        row["comm"] = os.path.basename(cmd.split(None, 1)[0]) if cmd.strip() else None
        try:
            row["start"] = time.mktime(time.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y"))
            row["cpu_s"] = max(now - row["start"], 0.0) * float(parts[7]) / 100.0
            row["rss_mb"] = round(int(parts[8]) / 1024, 1)
        except (ValueError, OverflowError):
            pass
        rows.append(row)
    return rows


def _default_rows(system: str) -> list:
    if system == "Windows":
        return _windows_rows()
    if system == "Darwin":
        return _darwin_rows()
    return _linux_rows()


def _vram_by_pid(reading: Any) -> tuple:
    """``({pid: MiB}, reason)`` from a ``gpu_sample.sample``-shaped reading."""
    if not isinstance(reading, dict):
        return {}, "vram-reading-malformed"
    if "gpu_unavailable" in reading:
        return {}, str(reading["gpu_unavailable"])
    raw = reading.get("vram_by_pid")
    if not isinstance(raw, dict):
        return {}, "vram-by-pid-absent"
    out = {}
    for key, val in raw.items():
        try:
            out[int(key)] = val
        except (TypeError, ValueError):
            continue
    return out, ""


def _finish(row: dict, now: float, vram: dict) -> dict:
    out = {k: row.get(k) for k in _ROW_KEYS}
    start = out["start"]
    age = max(now - start, 0.0) if isinstance(start, (int, float)) else None
    out["age_s"] = round(age, 1) if age is not None else None
    cpu = out["cpu_s"]
    out["pcpu"] = round(100.0 * cpu / age, 1) if age and isinstance(cpu, (int, float)) else None
    moved: list = [int(v) for v in (out["io_read_bytes"], out["io_write_bytes"]) if v is not None]
    out["io_bytes_per_s"] = round(sum(moved) / age, 1) if moved and age else None
    out["vram_mb"] = vram.get(out["pid"])
    return out


def read(
    readers: Optional[dict] = None,
    system: Optional[str] = None,
    now: Optional[float] = None,
) -> dict:
    """One census: ``{"system", "processes": [row...], "unavailable": {axis: reason}}``.

    ``readers`` injects ``processes`` (callable returning rows keyed by ``pid, ppid, comm, cmdline,
    start, cpu_s, rss_mb, io_read_bytes, io_write_bytes``) and ``vram`` (callable returning a
    ``gpu_sample.sample``-shaped dict). Never raises: a failed reader leaves its axes ``None``.
    """
    readers = readers or {}
    system = system or gpu_sample.host_system()
    now = time.time() if now is None else now
    unavailable: dict = {}
    try:
        raw = list((readers.get("processes") or (lambda: _default_rows(system)))())
    except Exception as exc:
        raw = []
        unavailable["processes"] = f"{type(exc).__name__}: {exc}"
    try:
        vram, vram_reason = _vram_by_pid((readers.get("vram") or gpu_sample.sample)())
    except Exception as exc:
        vram, vram_reason = {}, f"vram-error:{type(exc).__name__}"
    rows = [_finish(r, now, vram) for r in raw if isinstance(r, dict) and r.get("pid") is not None]
    if rows:
        for axis in _AXES:
            if axis == "vram_mb":
                if vram_reason:
                    unavailable[axis] = vram_reason
            elif all(r[axis] is None for r in rows):
                unavailable[axis] = (
                    _DARWIN_IO_REASON if system == "Darwin" and axis.startswith("io_") else "axis-unreadable")
    elif "processes" not in unavailable:
        unavailable["processes"] = "no-processes"
    return {"system": system, "processes": rows, "unavailable": unavailable}

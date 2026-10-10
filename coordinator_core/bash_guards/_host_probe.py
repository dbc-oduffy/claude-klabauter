"""Spawn-free host probe for guard-heavy-command-admission: RAM reading, process liveness keyed on
creation time and process-table snapshot on Windows, macOS and Linux.

Spawns nothing. Native libraries are reached through ctypes.CDLL(None) / WinDLL, never
ctypes.util.find_library. Every reader returns None (or a False/untrusted result) on any failure and
never raises; creation time is an opaque per-platform integer that is only ever compared for
equality with a value read by this module on the same host.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

_THIS_DIR = Path(__file__).resolve().parent
_contract_mod = sys.modules.get("coordinator_core.bash_guards._heavy_admission_contract")
if _contract_mod is None:
    _spec = importlib.util.spec_from_file_location(
        "_heavy_admission_contract", _THIS_DIR / "_heavy_admission_contract.py"
    )
    _contract_mod = importlib.util.module_from_spec(_spec)
    sys.modules.setdefault("_heavy_admission_contract", _contract_mod)
    _spec.loader.exec_module(_contract_mod)
MemoryReading = _contract_mod.MemoryReading
ProcRow = _contract_mod.ProcRow

_SAMPLER_FILE = _THIS_DIR.parent / "telemetry" / "host_sampler.py"


def _host_sampler():
    """host_sampler loaded by file path (no package import), reusing an already-imported copy."""
    for name in ("coordinator_core.telemetry.host_sampler", "host_sampler"):
        mod = sys.modules.get(name)
        if mod is not None:
            return mod
    spec = importlib.util.spec_from_file_location("host_sampler", _SAMPLER_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["host_sampler"] = mod
    spec.loader.exec_module(mod)
    return mod


def _platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


# --------------------------------------------------------------------------- RAM

def _memory_tuple() -> tuple:
    """(used_mb, avail_mb, total_mb) from host_sampler's per-platform reader."""
    hs = _host_sampler()
    plat = _platform()
    if plat == "windows":
        return hs._windows_memory_mb()
    if plat == "darwin":
        return hs._darwin_memory_mb()
    return hs._posix_memory_mb()


def read_available_mb() -> MemoryReading:
    """Available RAM in MB. trusted is False when the reading is None, non-positive, or larger than
    total RAM; a caller must deny heavy launches on an untrusted reading."""
    source = _platform()
    try:
        _used, avail, total = _memory_tuple()
    except Exception:
        return MemoryReading(None, False, source)
    if avail is None:
        return MemoryReading(None, False, source)
    trusted = avail > 0 and total is not None and total > 0 and avail <= total
    return MemoryReading(int(avail), bool(trusted), source)


# --------------------------------------------------------------------------- Windows

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x100000
_WAIT_TIMEOUT = 0x102
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_TH32CS_SNAPPROCESS = 0x2

_win_cache: dict = {}


def _win():
    """kernel32 with argtypes set for 64-bit handles; cached."""
    if "k" not in _win_cache:
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        k.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        _win_cache["k"] = k
    return _win_cache["k"]


def _win_open(pid: int, access: int):
    """(handle, last_error); handle is falsy on failure."""
    k = _win()
    ctypes.set_last_error(0)
    h = k.OpenProcess(access, False, pid)
    return h, (0 if h else ctypes.get_last_error())


def _win_ctime_of(handle) -> Optional[int]:
    from ctypes import wintypes

    k = _win()
    c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
    if not k.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
        return None
    return (c.dwHighDateTime << 32) | c.dwLowDateTime


def _windows_creation_time(pid: int) -> Optional[int]:
    h, _err = _win_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return None
    try:
        return _win_ctime_of(h)
    finally:
        _win().CloseHandle(h)


def _windows_alive(pid: int, ctime: int) -> bool:
    k = _win()
    h, err = _win_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE)
    if not h:
        return err == _ERROR_ACCESS_DENIED
    try:
        if k.WaitForSingleObject(h, 0) != _WAIT_TIMEOUT:
            return False
        return _win_ctime_of(h) == ctime
    finally:
        k.CloseHandle(h)


def _windows_snapshot() -> Optional[List]:
    """Toolhelp32 table; creation time per pid by GetProcessTimes (0 when the pid is inaccessible)."""
    k = _win()
    ent_t = _host_sampler()._PROCESSENTRY32
    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    rows: List = []
    try:
        entry = ent_t()
        entry.dwSize = ctypes.sizeof(ent_t)
        k.Process32First.argtypes = k.Process32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ok = k.Process32First(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            ct = _windows_creation_time(pid) if pid else None
            name = entry.szExeFile.decode("mbcs", "replace") if isinstance(entry.szExeFile, bytes) else str(entry.szExeFile)
            rows.append(ProcRow(pid, int(entry.th32ParentProcessID), ct or 0, name))
            ok = k.Process32Next(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return rows or None


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_working_set_bytes(pid: int) -> Optional[int]:
    k = _win()
    h, _err = _win_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return None
    try:
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(_MemoryCounters)
        if not k.K32GetProcessMemoryInfo(h, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.WorkingSetSize)
    finally:
        k.CloseHandle(h)


# --------------------------------------------------------------------------- Linux

def _linux_working_set_bytes(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{int(pid)}/statm", "r", encoding="ascii") as fh:
            resident_pages = int(fh.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _linux_stat(pid: int) -> Optional[tuple]:
    """(name, ppid, starttime) from /proc/<pid>/stat; comm may contain spaces and parens."""
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        lp, rp = raw.index("("), raw.rindex(")")
        rest = raw[rp + 2:].split()
        return raw[lp + 1:rp], int(rest[1]), int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def _linux_alive(pid: int, ctime: int) -> bool:
    st = _linux_stat(pid)
    return st is not None and st[2] == ctime


def _linux_snapshot() -> Optional[List]:
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    rows: List = []
    for e in entries:
        if e.isdigit():
            st = _linux_stat(int(e))
            if st is not None:
                rows.append(ProcRow(int(e), st[1], st[2], st[0]))
    return rows or None


# --------------------------------------------------------------------------- macOS
# READ-NOT-EXECUTED until C11's session: struct proc_bsdinfo layout, PROC_ALL_PIDS and
# PROC_PIDTBSDINFO values come from xnu headers read, not run on a Mac.

_PROC_ALL_PIDS = 1
_PROC_PIDTBSDINFO = 3


class _BSDInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32), ("rfu1", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32), ("pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32), ("e_tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_tvsec", ctypes.c_uint64), ("start_tvusec", ctypes.c_uint64),
    ]


def _darwin_bsdinfo(pid: int) -> Optional[tuple]:
    """(name, ppid, start_us) via proc_pidinfo, or None (gone, or another uid)."""
    try:
        lp = ctypes.CDLL(None, use_errno=True)
        b = _BSDInfo()
        n = lp.proc_pidinfo(int(pid), _PROC_PIDTBSDINFO, 0, ctypes.byref(b), ctypes.sizeof(b))
        if n != ctypes.sizeof(b):
            return None
        name = (b.name or b.comm).decode("utf-8", "replace")
        return name, int(b.ppid), int(b.start_tvsec) * 1_000_000 + int(b.start_tvusec)
    except Exception:
        return None


def _darwin_alive(pid: int, ctime: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    info = _darwin_bsdinfo(pid)
    return info is not None and info[2] == ctime


def _darwin_pids() -> Optional[List[int]]:
    try:
        lp = ctypes.CDLL(None, use_errno=True)
        need = lp.proc_listpids(_PROC_ALL_PIDS, 0, None, 0)
        if need <= 0:
            return None
        buf = (ctypes.c_int * (need // 4 + 64))()
        got = lp.proc_listpids(_PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
        if got <= 0:
            return None
        return [p for p in buf[: got // 4] if p > 0]
    except Exception:
        return None


def _darwin_snapshot() -> Optional[List]:
    pids = _darwin_pids()
    if pids is None:
        return None
    rows: List = []
    for p in pids:
        info = _darwin_bsdinfo(p)
        if info is not None:
            rows.append(ProcRow(p, info[1], info[2], info[0]))
    return rows or None


# --------------------------------------------------------------------------- public seam

def _safe(fn: Callable, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def alive(pid: int, ctime: int) -> bool:
    """True only when pid is running and its creation time equals ctime; a reused pid reads dead."""
    try:
        plat = _platform()
        if plat == "windows":
            return _windows_alive(pid, ctime)
        if plat == "darwin":
            return _darwin_alive(pid, ctime)
        return _linux_alive(pid, ctime)
    except Exception:
        return False


def creation_time(pid: int) -> Optional[int]:
    plat = _platform()
    if plat == "windows":
        return _safe(_windows_creation_time, pid)
    info = _safe(_darwin_bsdinfo if plat == "darwin" else _linux_stat, pid)
    return None if info is None else info[2]


def working_set_mb(pid: int) -> Optional[int]:
    """Resident set of pid in MB, or None when unreadable. macOS reads None until the live Mac
    verification (state/backlogs/2026-10-10-heavy-admission-live-mac-verification.md)."""
    plat = _platform()
    if plat == "windows":
        raw = _safe(_windows_working_set_bytes, pid)
    elif plat == "darwin":
        raw = None
    else:
        raw = _safe(_linux_working_set_bytes, pid)
    return None if raw is None else raw >> 20


def snapshot() -> Optional[Sequence]:
    """Every process as a ProcRow, or None when the table is unreadable."""
    plat = _platform()
    fn = {"windows": _windows_snapshot, "darwin": _darwin_snapshot}.get(plat, _linux_snapshot)
    return _safe(fn)


class HostPrimitives:
    """ProcessPrimitives bound to this host; the instance a caller passes where a stub is accepted."""

    alive = staticmethod(alive)
    creation_time = staticmethod(creation_time)
    snapshot = staticmethod(snapshot)


def ctime_units_per_second() -> int:
    """How many ProcRow.ctime units make one second on this host (FILETIME, clock ticks, or µs)."""
    if sys.platform == "win32":
        return 10_000_000
    if sys.platform == "darwin":
        return 1_000_000
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return 100

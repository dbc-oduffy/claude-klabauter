"""Shared host layer for the box-memory CLIs.

Stdlib plus optional psutil. Imports cleanly on every OS; Win32 bindings are created lazily
inside the functions that use them. No `coordinator_core` import.

Family exit codes: 0 ok, 1 operational failure, 2 usage, 3 not elevated, 4 admission HOLD
(`box-memory-snapshot --admit-peak` only), 6 unsupported host.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
UNSUPPORTED_HOST_EXIT = 6

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100


def unsupported(prog: str, platform: str, reason: str) -> int:
    """Print the one-line unsupported-host message on stderr; return UNSUPPORTED_HOST_EXIT."""
    print(f"{prog}: unsupported host ({platform}): {reason}", file=sys.stderr)
    return UNSUPPORTED_HOST_EXIT


def require_psutil(prog: str) -> int | None:
    """Return None when psutil imports; otherwise print one stderr line and return 1."""
    try:
        import psutil  # noqa: F401
    except ImportError:
        print(f"{prog}: psutil is required (pip install psutil)", file=sys.stderr)
        return 1
    return None


def load_governor(path: Path | None = None) -> types.ModuleType:
    """Load group-em-memory-governor.py by path; the only loader of that file.

    `path` defaults to the governor beside this module's `lib/` directory.
    """
    if path is None:
        path = Path(__file__).resolve().parents[1] / "group-em-memory-governor.py"
    spec = importlib.util.spec_from_file_location("group_em_memory_governor", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load governor from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class MemoryStatus:
    """Physical and commit memory, in bytes."""

    avail_phys: int
    total_phys: int
    commit_used: int
    commit_limit: int


def read_memory_status() -> MemoryStatus:
    """Read GlobalMemoryStatusEx. Windows only; raises OSError on API failure."""
    import ctypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        raise ctypes.WinError()
    return MemoryStatus(
        avail_phys=int(stat.ullAvailPhys),
        total_phys=int(stat.ullTotalPhys),
        commit_used=int(stat.ullTotalPageFile - stat.ullAvailPageFile),
        commit_limit=int(stat.ullTotalPageFile),
    )


def nonpaged_pool_bytes() -> int:
    """Kernel nonpaged pool size via GetPerformanceInfo. Windows only."""
    import ctypes
    from ctypes import wintypes

    class _PERFORMANCE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
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
            ("HandleCount", wintypes.DWORD),
            ("ProcessCount", wintypes.DWORD),
            ("ThreadCount", wintypes.DWORD),
        ]

    info = _PERFORMANCE_INFORMATION()
    info.cb = ctypes.sizeof(_PERFORMANCE_INFORMATION)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    if not psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.KernelNonpaged) * int(info.PageSize)


def empty_working_set(pid: int) -> None:
    """Trim one process's working set. Raises OSError(winerror). Windows only."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_SET_QUOTA, False, pid
    )
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kernel32.K32EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        if not kernel32.K32EmptyWorkingSet(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


@dataclass(frozen=True)
class AdmitVerdict:
    """Admission decision with both clauses exposed."""

    admit: bool
    need_avail: float
    avail_ok: bool
    commit_ok: bool


def admit(
    peak: int,
    status: MemoryStatus,
    cushion_pct: float = 25.0,
    cushion_cap: int = 8 * GIB,
    commit_limit: int | None = None,
) -> AdmitVerdict:
    """Pure admission check for a declared peak against a memory snapshot."""
    need_avail = peak + min(peak * cushion_pct / 100, cushion_cap)
    avail_ok = need_avail <= status.avail_phys
    commit_ok = status.commit_used + peak <= (status.commit_limit if commit_limit is None else commit_limit)
    return AdmitVerdict(
        admit=avail_ok and commit_ok,
        need_avail=need_avail,
        avail_ok=avail_ok,
        commit_ok=commit_ok,
    )

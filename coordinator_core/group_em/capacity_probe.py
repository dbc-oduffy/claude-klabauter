"""In-engine local capacity sampler: one box's ``fleet_live.FIELDS`` row, maxima and current use.

``sample()`` reuses the in-engine readers (``host_sampler``, ``gpu_sample``, ``box_load``,
``capacity_admit``) and adds only what they lack: commit charge and limit (Windows pagefile,
Linux ``CommitLimit``/``Committed_AS`` with swap as the fallback, macOS RAM plus swap), the CPU model,
fixed disks with their floor, and top processes by resident memory. Every reader is injectable and
``sample()`` never raises: a failed reader leaves its fields ``None`` (``gpus``/``disks``/``jobs``
and the top lists degrade to ``[]``). It spawns one ``ps`` child on POSIX (shared by ``top_cpu`` and ``top_ram``) and none on Windows.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.group_em import box_hold, box_load, capacity_admit, capacity_record, fleet_live, gpu_sample
from coordinator_core.telemetry import host_sampler

_VERDICT_KEYS = ("saturated", "mem_critical", "paused")

DEFAULT_DISK_FLOOR_GB = 50.0
TOP_N = 5
_SPAWN_TIMEOUT_S = 2
_MIB = 1024 * 1024
_GIB = 1024 ** 3
_JOB_KEYS = ("run", "job_type", "gpu_index", "granted_at", "vram_mib", "ram_mb", "cores")
_PSEUDO_FS = (
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "overlay", "squashfs",
    "autofs", "mqueue", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "configfs", "fusectl",
)


def _mb(nbytes: int) -> int:
    return int(nbytes // _MIB)


def _try(fn: Callable, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _home() -> Path:
    from coordinator_core._settings_home import settings_home

    return settings_home()


def parse_meminfo(text: str) -> dict:
    """``/proc/meminfo`` text as ``{key: MiB}``."""
    info: dict = {}
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[1].isdigit():
            info[parts[0]] = int(parts[1]) // 1024
    return info


def linux_commit(meminfo: dict) -> tuple:
    """``(commit_limit_mb, commit_used_mb)``: ``CommitLimit``/``Committed_AS``; when a key is
    absent, RAM+swap total and RAM+swap in use."""
    total = meminfo.get("MemTotal")
    swap_total = meminfo.get("SwapTotal", 0)
    limit = meminfo.get("CommitLimit")
    if limit is None and total is not None:
        limit = total + swap_total
    used = meminfo.get("Committed_AS")
    if used is None and total is not None and meminfo.get("MemAvailable") is not None:
        used = (total - meminfo["MemAvailable"]) + (swap_total - meminfo.get("SwapFree", swap_total))
    return limit, used


def darwin_commit(ram_total_mb: Optional[int], ram_avail_mb: Optional[int], swap: Optional[tuple]) -> tuple:
    """``(commit_limit_mb, commit_used_mb)`` from RAM and ``swap`` = ``(total_mb, used_mb)``."""
    if ram_total_mb is None or not swap:
        return None, None
    used = (ram_total_mb - ram_avail_mb + swap[1]) if ram_avail_mb is not None else None
    return ram_total_mb + swap[0], used


def windows_commit(pagefile_total_mb: int, pagefile_avail_mb: int) -> tuple:
    """``(commit_limit_mb, commit_used_mb)`` from ``ullTotalPageFile``/``ullAvailPageFile`` (commit)."""
    return pagefile_total_mb, pagefile_total_mb - pagefile_avail_mb


def _windows_pagefile() -> Optional[tuple]:
    class _Status(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _Status()
    status.dwLength = ctypes.sizeof(_Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return _mb(status.ullTotalPageFile), _mb(status.ullAvailPageFile)


def _darwin_swap() -> Optional[tuple]:
    """``(total_mb, used_mb)`` from ``vm.swapusage`` through ``sysctlbyname``; no child process."""

    class _SwapUsage(ctypes.Structure):
        _fields_ = [
            ("total", ctypes.c_uint64),
            ("avail", ctypes.c_uint64),
            ("used", ctypes.c_uint64),
            ("pagesize", ctypes.c_uint32),
            ("encrypted", ctypes.c_bool),
        ]

    buf = _SwapUsage()
    size = ctypes.c_size_t(ctypes.sizeof(buf))
    if ctypes.CDLL(None).sysctlbyname(b"vm.swapusage", ctypes.byref(buf), ctypes.byref(size), None, 0) != 0:
        return None
    return _mb(buf.total), _mb(buf.used)


def read_memory(system: Optional[str] = None, meminfo_text: Optional[str] = None,
                darwin_swap: Optional[Callable] = None, windows_pagefile: Optional[Callable] = None,
                ram: Optional[tuple] = None) -> tuple:
    """``(ram_total_mb, ram_free_mb, commit_limit_mb, commit_used_mb)``; a failed axis is ``None``.

    RAM comes from ``host_sampler.avail_mb`` (the one in-engine reader); the arguments inject the
    host for tests.
    """
    system = system or gpu_sample.host_system()
    avail, total = ram if ram is not None else host_sampler.avail_mb()
    limit = used = None
    try:
        if system == "Windows":
            got = (windows_pagefile or _windows_pagefile)()
            if got:
                limit, used = windows_commit(*got)
        elif system == "Darwin":
            limit, used = darwin_commit(total, avail, (darwin_swap or _darwin_swap)())
        else:
            if meminfo_text is None:
                with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as fh:
                    meminfo_text = fh.read()
            limit, used = linux_commit(parse_meminfo(meminfo_text))
    except Exception:
        limit = used = None
    return total, avail, limit, used


def read_cpu_model() -> Optional[str]:
    system = gpu_sample.host_system()
    if system == "Windows":
        import winreg

        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        try:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip() or None
        finally:
            winreg.CloseKey(key)
    if system == "Darwin":
        buf = ctypes.create_string_buffer(256)
        size = ctypes.c_size_t(256)
        if ctypes.CDLL(None).sysctlbyname(b"machdep.cpu.brand_string", buf, ctypes.byref(size), None, 0) != 0:
            return None
        return buf.value.decode("utf-8", "replace").strip() or None
    with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.lower().startswith(("model name", "hardware", "cpu model")):
                return line.partition(":")[2].strip() or None
    return None


def read_cpu_pct() -> Optional[float]:
    """Busy percent from ``host_sampler.read_load`` (unclamped POSIX load-per-core read as percent)."""
    load = host_sampler.read_load().get("cpu_load")
    return None if load is None else round(min(load, 1.0) * 100.0, 1)


def disk_floor_gb() -> float:
    raw = os.environ.get("COORDINATOR_DISK_FLOOR_GB")
    try:
        value = float(raw) if raw else DEFAULT_DISK_FLOOR_GB
    except ValueError:
        return DEFAULT_DISK_FLOOR_GB
    return value if value >= 0 else DEFAULT_DISK_FLOOR_GB


def _disk_mounts() -> list:
    system = gpu_sample.host_system()
    if system == "Windows":
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        return [
            chr(65 + i) + ":\\"
            for i in range(26)
            if mask & (1 << i) and kernel32.GetDriveTypeW(chr(65 + i) + ":\\") == 3
        ]
    if system == "Linux":
        mounts: list = []
        seen: set = set()
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3 or parts[2] in _PSEUDO_FS or not parts[0].startswith("/") or parts[0] in seen:
                    continue
                seen.add(parts[0])
                mounts.append(parts[1].replace("\\040", " "))
        return mounts or ["/"]
    return ["/"]


def read_disks(mounts: Optional[list] = None, floor: Optional[float] = None) -> list:
    """``[{mount, total_gb, free_gb, floor_gb}]`` per fixed disk; an unreadable mount is skipped."""
    floor = disk_floor_gb() if floor is None else floor
    rows = []
    for mount in mounts if mounts is not None else _try(_disk_mounts, []):
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        rows.append({
            "mount": mount,
            "total_gb": round(usage.total / _GIB, 1),
            "free_gb": round(usage.free / _GIB, 1),
            "floor_gb": floor,
        })
    return rows


def read_gpus() -> list:
    """GPU rows from ``gpu_sample.sample`` (NVML in-process: no ``nvidia-smi`` spawn); ``[]`` when none."""
    gpus = gpu_sample.sample().get("gpus")
    return gpus if isinstance(gpus, list) else []


def read_sessions() -> Optional[int]:
    """Count of session records under the claude config dir (``box_load.read_sessions``); ``None`` when
    the directory holds none."""
    found = box_load.read_sessions()
    return len(found) if found else None


def read_jobs() -> list:
    """Unexpired admission grants of this box: ``[{run, job_type, gpu_index, granted_at, ...}]``."""
    return [{k: g.get(k) for k in _JOB_KEYS} for g in capacity_admit.read_held({}, _home())]


def _slot_holder(name: str, jobs: list) -> Optional[str]:
    """Holder of the ``<name>-slot.json`` record beside ``box_hold``'s ``hold.json`` (``session_id``/``holder``);
    for ``gpu`` without a record, the run of the first grant that pins a GPU index."""
    try:
        record = json.loads((box_hold._hold_path().parent / (name + "-slot.json")).read_text("utf-8"))
        holder = record.get("session_id") or record.get("holder") if isinstance(record, dict) else None
        if holder:
            return str(holder)
    except (OSError, ValueError):
        pass
    if name == "gpu":
        for job in jobs:
            if job.get("gpu_index") is not None and job.get("run"):
                return str(job["run"])
    return None


def _top_cpu() -> list:
    return [
        {"pid": p["pid"], "comm": p["comm"], "pcpu": p["pcpu"]} for p in box_load.top_processes(TOP_N)
    ]


def _windows_top_ram() -> list:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class _Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (n, ctypes.c_size_t)
            for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                      "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                      "PeakPagefileUsage")
        ]

    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters), ctypes.c_ulong]
    pids = (ctypes.c_ulong * 8192)()
    needed = ctypes.c_ulong()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    rows = []
    for pid in pids[: needed.value // ctypes.sizeof(ctypes.c_ulong)]:
        handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                continue
            path = ctypes.create_unicode_buffer(520)
            length = ctypes.c_ulong(520)
            name = path.value if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)) else ""
            rows.append({"pid": int(pid), "comm": os.path.basename(name) or "?",
                         "rss_mb": _mb(counters.WorkingSetSize)})
        finally:
            kernel32.CloseHandle(handle)
    return sorted(rows, key=lambda r: r["rss_mb"], reverse=True)[:TOP_N]


def _posix_ps() -> list:
    """Every process as ``{pid, comm, pcpu, rss_mb}`` from one ``ps`` spawn; ``[]`` on failure."""
    done = subprocess.run(["ps", "-A", "-o", "pid=,rss=,pcpu=,comm="], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=_SPAWN_TIMEOUT_S)
    if done.returncode != 0:
        return []
    rows = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            try:
                pcpu = float(parts[2])
            except ValueError:
                continue
            rows.append({"pid": int(parts[0]), "comm": parts[3].strip(), "pcpu": pcpu,
                         "rss_mb": int(parts[1]) // 1024})
    return rows


def _posix_top_cpu(rows: list) -> list:
    top = sorted(rows, key=lambda r: r["pcpu"], reverse=True)[:TOP_N]
    return [{"pid": r["pid"], "comm": r["comm"], "pcpu": r["pcpu"]} for r in top]


def _posix_top_ram(rows: list) -> list:
    top = sorted(rows, key=lambda r: r["rss_mb"], reverse=True)[:TOP_N]
    return [{"pid": r["pid"], "comm": r["comm"], "rss_mb": r["rss_mb"]} for r in top]


def _top_ram() -> list:
    return _windows_top_ram() if gpu_sample.host_system() == "Windows" else _posix_top_ram(_posix_ps())


def _shared_top_readers(table: dict) -> None:
    """Point default ``top_cpu``/``top_ram`` at one lazily-run POSIX ``ps``; injected readers stay."""
    if table.get("top_cpu") is not _top_cpu or table.get("top_ram") is not _top_ram:
        return
    if gpu_sample.host_system() == "Windows":
        return
    cache: list = []

    def rows() -> list:
        if not cache:
            cache.append(_posix_ps())
        return cache[0]

    table["top_cpu"] = lambda: _posix_top_cpu(rows())
    table["top_ram"] = lambda: _posix_top_ram(rows())


def _alias() -> str:
    return fleet_live.box_alias(None)


def _box() -> str:
    from coordinator_core.group_em.box_registry import local_box_id

    return local_box_id()


_DEFAULT_READERS: dict = {
    "box": _box,
    "alias": _alias,
    "os": gpu_sample.host_system,
    "cores": os.cpu_count,
    "cpu_model": read_cpu_model,
    "memory": read_memory,
    "cpu_pct": read_cpu_pct,
    "load5": box_load.read_load5,
    "gpus": read_gpus,
    "disks": read_disks,
    "sessions": read_sessions,
    "jobs": read_jobs,
    "gpu_slot_holder": None,
    "editor_slot_holder": None,
    "top_cpu": _top_cpu,
    "top_ram": _top_ram,
    "verdicts": capacity_record.local_verdicts,
}


def sample(now: Optional[float] = None, readers: Optional[dict] = None) -> dict:
    """This box's row, keyed exactly by ``fleet_live.FIELDS`` with ``state`` ``ok``.

    ``readers`` overrides entries of the default reader table (each a zero-argument callable;
    ``memory`` returns ``(ram_total_mb, ram_free_mb, commit_limit_mb, commit_used_mb)``). Never raises.
    """
    table = dict(_DEFAULT_READERS)
    if readers:
        table.update(readers)
    _shared_top_readers(table)
    stamp = time.time() if now is None else now

    def read(name: str, default: Any = None) -> Any:
        fn = table.get(name)
        return _try(fn, default) if fn is not None else default

    row = fleet_live.empty_row(read("box"), fleet_live.STATE_OK)
    for name in ("alias", "os", "cores", "cpu_model", "cpu_pct", "load5", "sessions"):
        row[name] = read(name)
    memory = read("memory", (None, None, None, None))
    if not isinstance(memory, (tuple, list)) or len(memory) != 4:
        memory = (None, None, None, None)
    row["ram_total_mb"], row["ram_free_mb"], row["commit_limit_mb"], row["commit_used_mb"] = memory
    gpus = read("gpus", [])
    row["gpus"] = gpus if isinstance(gpus, list) else []
    used = [g["vram_used_mib"] for g in row["gpus"] if isinstance(g, dict) and g.get("vram_used_mib") is not None]
    utils = [g["util_pct"] for g in row["gpus"] if isinstance(g, dict) and g.get("util_pct") is not None]
    row["vram_used_mib"] = sum(used) if used else None
    row["gpu_util_pct"] = max(utils) if utils else None
    disks = read("disks", [])
    row["disks"] = disks if isinstance(disks, list) else []
    jobs = read("jobs", [])
    row["jobs"] = jobs if isinstance(jobs, list) else []
    for name in ("gpu_slot_holder", "editor_slot_holder"):
        if table.get(name) is None:
            row[name] = _try(lambda n=name[:-12]: _slot_holder(n, row["jobs"]))
        else:
            row[name] = read(name)
    for name in ("top_cpu", "top_ram"):
        value = read(name, [])
        row[name] = value if isinstance(value, list) else []
    # The stamped saturation and memory-governor verdicts ride the row to the G-EM box, which cannot
    # read this box's root; a key not in fleet_live.FIELDS is not on the row and is skipped.
    verdicts = read("verdicts", {})
    for name in _VERDICT_KEYS:
        if name in row and isinstance(verdicts, dict):
            row[name] = verdicts.get(name)
    row["maxima_source"] = "live"
    row["sampled_at"] = stamp
    row["last_seen"] = stamp
    return row

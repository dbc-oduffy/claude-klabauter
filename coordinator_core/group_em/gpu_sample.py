"""In-process GPU sample: NVML through ctypes on Windows and Linux, unified memory on Apple silicon.

Never spawns a child process. ``sample()`` returns ``{"gpus": [...], "vram_by_pid": {...}}`` or
``{"gpu_unavailable": <reason>}``; it never raises. A gpu row is the ``capacity_record`` shape
(``index, name, vram_total_mib, vram_used_mib, util_pct``) plus ``unified`` on Apple silicon.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any, Callable, Optional

_MIB = 1024 * 1024
_NAME_BUF = 96
_NVML_SUCCESS = 0
_NVML_INSUFFICIENT_SIZE = 7
_NVML_VALUE_NOT_AVAILABLE = 0xFFFFFFFFFFFFFFFF
_PROC_SLACK = 8

NVML_ABSENT = "nvml-absent"
VENDOR_UNSUPPORTED = "vendor-unsupported"
NO_GPU = "no-gpu"


class _Memory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint),
        ("usedGpuMemory", ctypes.c_ulonglong),
        ("gpuInstanceId", ctypes.c_uint),
        ("computeInstanceId", ctypes.c_uint),
    ]


def host_system() -> str:
    """``Windows`` | ``Linux`` | ``Darwin`` | the raw ``sys.platform``; ``platform.system()`` can spawn ``ver`` on Windows."""
    return {"win32": "Windows", "darwin": "Darwin"}.get(sys.platform, "Linux" if sys.platform.startswith("linux") else sys.platform)


def host_arch() -> str:
    """Machine architecture without ``platform`` (which can spawn on Windows)."""
    if sys.platform == "win32":
        return os.environ.get("PROCESSOR_ARCHITECTURE", "")
    return os.uname().machine


def _candidates(system: str) -> list:
    """NVML library paths to try, in order; a bare soname defers to the loader's search path."""
    if system == "Windows":
        found = []
        root = os.environ.get("SystemRoot")
        if root:
            found.append(os.path.join(root, "System32", "nvml.dll"))
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            found.append(os.path.join(program_files, "NVIDIA Corporation", "NVSMI", "nvml.dll"))
        return found
    return ["libnvidia-ml.so.1"]


def _load_nvml(system: str, loader: Callable = ctypes.CDLL) -> Any:
    """The NVML library object, or None when no candidate loads."""
    for path in _candidates(system):
        try:
            return loader(path)
        except (OSError, TypeError):
            continue
    return None


def _call(fn: Callable, *args) -> int:
    return int(fn(*args))


def _compute_processes(lib: Any, handle: ctypes.c_void_p) -> dict:
    """{pid: bytes of VRAM} of the compute processes on one device; {} when the query fails.

    An entry whose usage the driver does not report (WDDM) is dropped, never zeroed.
    """
    fn = getattr(lib, "nvmlDeviceGetComputeRunningProcesses_v3", None) or getattr(
        lib, "nvmlDeviceGetComputeRunningProcesses", None
    )
    if fn is None:
        return {}
    count = ctypes.c_uint(0)
    status = _call(fn, handle, ctypes.byref(count), None)
    if status == _NVML_SUCCESS and count.value == 0:
        return {}
    if status not in (_NVML_SUCCESS, _NVML_INSUFFICIENT_SIZE):
        return {}
    size = count.value + _PROC_SLACK
    infos = (_ProcessInfo * size)()
    count = ctypes.c_uint(size)
    if _call(fn, handle, ctypes.byref(count), infos) != _NVML_SUCCESS:
        return {}
    used: dict = {}
    for info in infos[: min(count.value, size)]:
        if info.usedGpuMemory != _NVML_VALUE_NOT_AVAILABLE:
            used[int(info.pid)] = used.get(int(info.pid), 0) + int(info.usedGpuMemory)
    return used


def _sample_nvml(lib: Any) -> dict:
    if _call(lib.nvmlInit_v2) != _NVML_SUCCESS:
        return {"gpu_unavailable": "nvml-init-failed"}
    try:
        count = ctypes.c_uint(0)
        if _call(lib.nvmlDeviceGetCount_v2, ctypes.byref(count)) != _NVML_SUCCESS or count.value == 0:
            return {"gpu_unavailable": NO_GPU}
        gpus: list = []
        by_pid: dict = {}
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if _call(lib.nvmlDeviceGetHandleByIndex_v2, ctypes.c_uint(index), ctypes.byref(handle)) != _NVML_SUCCESS:
                continue
            memory = _Memory()
            has_memory = _call(lib.nvmlDeviceGetMemoryInfo, handle, ctypes.byref(memory)) == _NVML_SUCCESS
            util = _Utilization()
            has_util = _call(lib.nvmlDeviceGetUtilizationRates, handle, ctypes.byref(util)) == _NVML_SUCCESS
            name_buf = ctypes.create_string_buffer(_NAME_BUF)
            has_name = _call(lib.nvmlDeviceGetName, handle, name_buf, ctypes.c_uint(_NAME_BUF)) == _NVML_SUCCESS
            gpus.append(
                {
                    "index": index,
                    "name": name_buf.value.decode("utf-8", "replace") if has_name else None,
                    "vram_total_mib": int(memory.total // _MIB) if has_memory else None,
                    "vram_used_mib": int(memory.used // _MIB) if has_memory else None,
                    "util_pct": int(util.gpu) if has_util else None,
                }
            )
            for pid, used in _compute_processes(lib, handle).items():
                by_pid[pid] = by_pid.get(pid, 0) + used // _MIB
        if not gpus:
            return {"gpu_unavailable": NO_GPU}
        return {"gpus": gpus, "vram_by_pid": by_pid}
    finally:
        try:
            lib.nvmlShutdown()
        except Exception:
            pass


def _sample_unified(total_mib: Optional[int]) -> dict:
    return {
        "gpus": [
            {
                "index": 0,
                "name": "Apple silicon",
                "vram_total_mib": total_mib,
                "vram_used_mib": None,
                "util_pct": None,
                "unified": True,
            }
        ],
        "vram_by_pid": {},
    }


def sample(
    system: Optional[str] = None,
    machine: Optional[str] = None,
    loader: Callable = ctypes.CDLL,
    ram_total_mb: Optional[Callable[[], Optional[int]]] = None,
) -> dict:
    """One GPU reading. Arguments inject the host for tests; the defaults read this host.

    Returns ``{"gpus": [...], "vram_by_pid": {pid: MiB}}`` or ``{"gpu_unavailable": reason}`` with
    reason ``nvml-absent`` (no loadable NVML), ``nvml-init-failed``, ``no-gpu``,
    ``vendor-unsupported`` (Intel macOS) or ``nvml-error:<ExceptionName>``. Never raises, never spawns.
    """
    system = system or host_system()
    machine = machine if machine is not None else host_arch()
    try:
        if system == "Darwin":
            if machine.lower() not in ("arm64", "aarch64"):
                return {"gpu_unavailable": VENDOR_UNSUPPORTED}
            if ram_total_mb is None:
                from coordinator_core.telemetry import host_sampler

                ram_total_mb = lambda: host_sampler.avail_mb()[1]  # noqa: E731
            return _sample_unified(ram_total_mb())
        if system not in ("Windows", "Linux"):
            return {"gpu_unavailable": VENDOR_UNSUPPORTED}
        lib = _load_nvml(system, loader)
        if lib is None:
            return {"gpu_unavailable": NVML_ABSENT}
        return _sample_nvml(lib)
    except Exception as exc:
        return {"gpu_unavailable": f"nvml-error:{type(exc).__name__}"}


def static_rows(reading: dict) -> dict:
    """The capability-record view of ``reading``: ``{"devices": [{name, vram_total_mib, unified}]}``
    or ``{"unavailable": reason}``. Drops every live figure (utilisation, VRAM used)."""
    gpus = reading.get("gpus")
    if not isinstance(gpus, list):
        return {"unavailable": reading.get("gpu_unavailable") or NVML_ABSENT}
    return {
        "devices": [
            {
                "name": g.get("name"),
                "vram_total_mib": g.get("vram_total_mib"),
                "unified": bool(g.get("unified")),
            }
            for g in gpus
        ]
    }


if __name__ == "__main__":
    import json

    print(json.dumps(sample()))
    sys.exit(0)

"""Box capability probe: what this machine can run, recorded in box.json beside its identity.

Zero subprocess calls -- stdlib, ctypes and ``os.sysconf`` only. ``gpu`` is null because GPU
detection (``nvidia-smi``) is a spawn; it waits on the hardware. Leaf module like ``box_registry``.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.group_em import box_registry


def _ram_mb() -> Optional[int]:
    """Total physical RAM in MiB; None when the platform will not say."""
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
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

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return None
            return int(status.ullTotalPhys // (1024 * 1024))
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size // (1024 * 1024))
    except (OSError, ValueError, AttributeError):
        return None


def _has_ue() -> bool:
    """True when an Unreal root env var names an existing dir, or an editor binary is on PATH."""
    for var in ("UE_ROOT", "UNREAL_ENGINE_ROOT"):
        root = os.environ.get(var)
        if root and os.path.isdir(root):
            return True
    return any(shutil.which(name) is not None for name in ("UnrealEditor", "UnrealEditor-Cmd"))


def probe() -> dict:
    """This machine's capability record.

    Fields: os, arch, cpu_count, ram_mb, python, gpu (always None), has_p4 and has_ue (flat
    booleans so ``placement.satisfies`` can require ``{"has_ue": True}``).
    """
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count(),
        "ram_mb": _ram_mb(),
        "python": platform.python_version(),
        "gpu": None,
        "has_p4": shutil.which("p4") is not None,
        "has_ue": _has_ue(),
    }


def stored_capability(home: Optional[Path] = None) -> Optional[dict]:
    """The non-empty ``capability`` dict already in ``<home>/state/group-em/box.json``; None when
    absent, empty or unreadable (an empty record makes the register op probe afresh).

    Read-only: never mints an identity and never probes.
    """
    try:
        with open(box_registry._identity_path(home), "r", encoding="utf-8") as fh:
            record = json.loads(fh.read())
    except (OSError, ValueError):
        return None
    cap = record.get("capability") if isinstance(record, dict) else None
    return cap if isinstance(cap, dict) and cap else None


def write_capability(home: Optional[Path] = None) -> dict:
    """Merge ``probe()`` into ``<home>/state/group-em/box.json`` as ``capability``; returns the record.

    Mints the identity first when absent, and keeps every other key of the file.
    """
    box_registry.ensure_identity(home)
    path = box_registry._identity_path(home)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.loads(fh.read())
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    record["capability"] = probe()
    box_registry._write(path, record)
    return record


def _main() -> int:
    print(json.dumps(write_capability(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

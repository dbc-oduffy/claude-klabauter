"""cpu_contain: contain CPU-saturating processes on this box.

A dead-parent CPU orphan is terminated; a live-parented saturator is dropped to BELOW_NORMAL
priority. The orphan predicate and the protected name list are injected by the caller, so this
module imports nothing from `coordinator_core/group_em`. The caller's own pid and every ancestor
are never touched. No process is spawned: `SetPriorityClass` goes through ctypes on Windows,
`os.setpriority` elsewhere.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from coordinator_core.box_memory.orphan_reap import _terminate, is_self_or_ancestor, protected_pids

_WIN_PROCESS_SET_INFORMATION = 0x0200
_WIN_BELOW_NORMAL_PRIORITY_CLASS = 0x4000
_POSIX_NICE_INCREMENT = 10


def _field(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def lower_priority(pid: int) -> None:
    """Drop `pid` to BELOW_NORMAL (Windows) or nice +10 (POSIX). Raises OSError on refusal."""
    if is_self_or_ancestor(pid):
        return
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(_WIN_PROCESS_SET_INFORMATION, False, pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), f"OpenProcess failed for pid {pid}")
        try:
            if not kernel32.SetPriorityClass(handle, _WIN_BELOW_NORMAL_PRIORITY_CLASS):
                raise OSError(ctypes.get_last_error(), f"SetPriorityClass failed for pid {pid}")
        finally:
            kernel32.CloseHandle(handle)
        return
    current = os.getpriority(os.PRIO_PROCESS, pid)
    os.setpriority(os.PRIO_PROCESS, pid, max(current, _POSIX_NICE_INCREMENT))


def _is_protected_name(name: str, protected: Iterable[str]) -> bool:
    low = name.lower()
    stem = low[:-4] if low.endswith(".exe") else low
    return low in protected or stem in protected


def contain(
    rows: Sequence,
    primitives: Any,
    is_orphan: Callable[[Any], bool],
    protected: Any,
    saturated: bool,
    *,
    terminate: Callable[[int], None] = _terminate,
    lower: Callable[[int], None] = lower_priority,
    self_pid: Optional[int] = None,
    dry_run: bool = False,
) -> List[dict]:
    """Contain each candidate saturator in `rows` and return `{pid, name, action, ok}` per target.

    `rows` are the caller's CPU-heavy candidates (`ProcRow`s or dicts with `pid`, `name`, `ctime`).
    With `saturated` False nothing is touched. A row is skipped when its pid is the caller or an
    ancestor, its name is in `protected` (case-insensitive, with or without `.exe`), or its
    creation time no longer matches `primitives.creation_time` (gone, or the PID was reused).
    `is_orphan(row)` True terminates (`action` "terminate"); otherwise the row drops to
    below-normal priority (`action` "lower_priority"). With `dry_run` nothing is acted on and
    `action` is prefixed "would_". A refused action reports `ok` False and never raises."""
    if not saturated:
        return []
    names = {str(n).strip().lower() for n in (protected or ()) if str(n).strip()}
    protected_set = protected_pids(self_pid)
    actions: List[dict] = []
    for row in rows or ():
        try:
            pid = _field(row, "pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid in protected_set:
                continue
            name = str(_field(row, "name") or _field(row, "comm") or "")
            if _is_protected_name(name, names):
                continue
            ctime = _field(row, "ctime", 0)
            if ctime and primitives is not None and primitives.creation_time(pid) != ctime:
                continue
            try:
                orphan = bool(is_orphan(row))
            except Exception:  # noqa: BLE001 -- an unreadable verdict is not an orphan
                orphan = False
            kind = "terminate" if orphan else "lower_priority"
            if dry_run:
                actions.append({"pid": pid, "name": name, "action": f"would_{kind}", "ok": True})
                continue
            try:
                (terminate if orphan else lower)(pid)
                ok = True
            except Exception:  # noqa: BLE001 -- access denied or gone: report, keep going
                ok = False
            actions.append({"pid": pid, "name": name, "action": kind, "ok": ok})
        except Exception:  # noqa: BLE001 -- a malformed row is skipped, never fatal
            continue
    return actions


def cpu_shares(
    primitives: Any,
    rows: Sequence,
    interval_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[int, float]:
    """Percent of one core each row's process used over `interval_s`, from two `cpu_time_s` reads.

    A pid with an unreadable read on either side is omitted. Spawns nothing."""
    pids = [_field(r, "pid") for r in rows or ()]
    pids = [p for p in pids if isinstance(p, int) and not isinstance(p, bool)]
    first = {p: primitives.cpu_time_s(p) for p in pids}
    t0 = clock()
    sleep(interval_s)
    elapsed = clock() - t0
    if elapsed <= 0:
        return {}
    shares: Dict[int, float] = {}
    for pid, before in first.items():
        after = primitives.cpu_time_s(pid)
        if before is None or after is None or after < before:
            continue
        shares[pid] = (after - before) / elapsed * 100.0
    return shares

# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""coordinator_core/session/gate_budget.py -- per-invocation process-time
budget reporting for the two validate front doors
(coordinator/bin/validate-fast-and-packageability.py's `_run_resolved_command`
and coordinator/bin/workday-complete-step1-validate.py's `_run_fast_test_cmd`).

Spec: docs/plans/2026-09-07-fix-the-validate-gate-recursive-tier-invocation.md
(B1).

What this emits, and nothing else: one stderr line carrying the door's own
process time (`self_process_ms`) and the spawned suite's aggregate process
time (`suite_process_ms`), plus a second stderr line naming DR-344 when
`self_process_ms` exceeds the 500ms brightline
(docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md). Process
time only -- never wall clock, and there is no fallback to one; an unreadable
counter reports "unavailable" rather than a wall-clock substitute. Neither
door's stdout (`Validation: ...` / `RC_UBT=...`) nor its exit code is ever
touched by anything in this module -- every failure here is swallowed into
"unavailable".

Division of labour with `with-suite-mutex --max-runtime`
(coordinator.local.md): that is a wall-clock KILL ceiling (default
`suite_mutex.STALE_TTL_SECS`, 45 min) guarding against a stuck/hung run. This
module never kills anything -- it only REPORTS, in process time, after the
run has already finished. The two are not the same mechanism and are not
substitutes for each other.

Negative spec: no suite-process-time ceiling and no breach line for it. The
suite is not a claude-klabauter op and DR-344's 500ms bar does not apply to it; the
only honest ceiling value is a PM-side quiet-box measurement, and no agent
in this repo runs the tier to produce one. That config key and its breach
line land together, in the change that sets the key from that measurement --
not here. There is likewise no worker-count-cap configuration axis.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

DR344_BAR_MS = 500


def self_process_ms() -> Optional[int]:
    try:
        t = os.times()
        return round((t.user + t.system) * 1000)
    except Exception:
        return None


def snapshot_children_times() -> Optional[tuple[float, float]]:
    if os.name == "nt":
        return None
    try:
        t = os.times()
        return (t.children_user, t.children_system)
    except Exception:
        return None


def _windows_job_process_ms(job_handle) -> Optional[int]:
    if os.name != "nt" or job_handle is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        JobObjectBasicAccountingInformation = 1

        class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        ok = kernel32.QueryInformationJobObject(
            job_handle,
            JobObjectBasicAccountingInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not ok:
            return None
        total_100ns = info.TotalUserTime + info.TotalKernelTime
        return round(total_100ns / 10_000)
    except Exception:
        return None


def suite_process_ms(
    before: Optional[tuple[float, float]] = None,
    after: Optional[tuple[float, float]] = None,
    job_handle=None,
) -> Optional[int]:
    if os.name == "nt":
        return _windows_job_process_ms(job_handle)
    if before is None or after is None:
        return None
    try:
        delta_seconds = (after[0] - before[0]) + (after[1] - before[1])
        return round(delta_seconds * 1000)
    except Exception:
        return None


def format_budget_line(self_ms: Optional[int], suite_ms: Optional[int]) -> str:
    self_str = str(self_ms) if self_ms is not None else "unavailable"
    suite_str = str(suite_ms) if suite_ms is not None else "unavailable"
    return (
        f"[validate-gate] self_process_ms={self_str} suite_process_ms={suite_str} "
        "(process time, not wall clock)"
    )


def format_breach_line(self_ms: Optional[int]) -> Optional[str]:
    if self_ms is None or self_ms <= DR344_BAR_MS:
        return None
    return (
        f"[validate-gate] DR-344 BREACH: self_process_ms={self_ms} exceeds the "
        f"{DR344_BAR_MS}ms process-time brightline "
        "(docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md)"
    )


def emit_budget_lines(self_ms: Optional[int], suite_ms: Optional[int]) -> None:
    print(format_budget_line(self_ms, suite_ms), file=sys.stderr)
    breach = format_breach_line(self_ms)
    if breach is not None:
        print(breach, file=sys.stderr)

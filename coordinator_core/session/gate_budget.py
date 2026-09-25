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
    """This process's own process time (user + system, milliseconds), via
    `os.times()` -- portable across POSIX and Windows. `os.times()` reports
    cumulative time since process start, which is exactly the door's own
    work for these one-shot CLI invocations, so no snapshot/delta is needed
    here (contrast `suite_process_ms`, which DOES need one because the
    aggregate keeps accruing across the spawned child's whole lifetime).

    Returns None (never raises) if `os.times()` itself is unavailable on
    this platform/build -- the caller renders that as "unavailable".
    """
    try:
        t = os.times()
        return round((t.user + t.system) * 1000)
    except Exception:
        return None


def snapshot_children_times() -> Optional[tuple[float, float]]:
    """POSIX-only snapshot of `os.times()`'s `children_user`/`children_system`
    fields, taken BEFORE the suite subprocess is spawned. Feed the return
    value into `suite_process_ms` as `before`, after taking a second
    snapshot (`after`) once the spawned process has been waited on.

    `children_user`/`children_system` cover every waited-for descendant,
    xdist workers included, which is why this is the suite's own aggregate
    and not merely the direct child's time.

    Returns None on Windows (no `os.times()` children fields there -- use
    the Job Object accounting path instead, via the `job_handle` argument
    to `suite_process_ms`) or on any read failure.
    """
    if os.name == "nt":
        return None
    try:
        t = os.times()
        return (t.children_user, t.children_system)
    except Exception:
        return None


def _windows_job_process_ms(job_handle) -> Optional[int]:
    """Read `TotalUserTime + TotalKernelTime` (100ns units) off a Windows
    Job Object via `QueryInformationJobObject(JobObjectBasicAccountingInformation)`,
    converted to milliseconds. Must be called BEFORE the caller's own
    `_close_windows_job_object` releases the handle -- once closed, the
    accounting is gone.

    Returns None on any non-Windows platform, a `None` handle, or any
    failure reading the accounting block (never raises).
    """
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
    """The spawned suite's aggregate process time, milliseconds.

    POSIX: `after - before` on the `(children_user, children_system)` pairs
    from `snapshot_children_times`, taken before the spawn and after the
    wait. Windows: reads the Job Object the door already assigns the child
    to (`_assign_windows_job_object`) via `_windows_job_process_ms`, which
    the caller must do BEFORE its own `_close_windows_job_object`.

    Returns None (never raises) whenever the needed inputs are absent or
    unreadable -- the caller renders that as "unavailable".
    """
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
    """The one stderr line both doors print per invocation. `<n|unavailable>`
    on either figure independently -- one being readable never depends on
    the other."""
    self_str = str(self_ms) if self_ms is not None else "unavailable"
    suite_str = str(suite_ms) if suite_ms is not None else "unavailable"
    return (
        f"[validate-gate] self_process_ms={self_str} suite_process_ms={suite_str} "
        "(process time, not wall clock)"
    )


def format_breach_line(self_ms: Optional[int]) -> Optional[str]:
    """Returns the DR-344 breach line when `self_ms` exceeds the 500ms
    brightline, else None. An unreadable `self_ms` (None) never breaches --
    there is nothing to compare, so no line is emitted for it.
    """
    if self_ms is None or self_ms <= DR344_BAR_MS:
        return None
    return (
        f"[validate-gate] DR-344 BREACH: self_process_ms={self_ms} exceeds the "
        f"{DR344_BAR_MS}ms process-time brightline "
        "(docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md)"
    )


def emit_budget_lines(self_ms: Optional[int], suite_ms: Optional[int]) -> None:
    """Print the budget line, then the breach line if any, to stderr. The
    single call site both doors wire into their own suite-spawn helper."""
    print(format_budget_line(self_ms, suite_ms), file=sys.stderr)
    breach = format_breach_line(self_ms)
    if breach is not None:
        print(breach, file=sys.stderr)

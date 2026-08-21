"""
coordinator_core.benchmarks.process_time -- Windows job-object process-time primitive.

Purpose: DR-344's brightline gates on PROCESS TIME (user+kernel CPU time
across a spawned process tree), never wall clock -- CLAUDE.md's own § "The
brightline" is explicit that wall clock on this box measures peer load
(50-70 concurrent sessions is the design condition), not cost, and a gate
another concurrent session can move is the same bug wearing a new name.
This module is the single shared primitive for measuring it: a Windows job
object accounts `TotalUserTime + TotalKernelTime` (100ns units) over EVERY
process ever assigned to it, including exited children -- the whole tree, not
just the immediate child `subprocess` sees.

Three traps this module exists to avoid -- each already produced a false
PASS on this box (see state/handoffs/2026-08-21_103635_reaching-the-warm-
engine.md § Measured findings):

  1. `os.times().children_user`/`children_system` are ALWAYS 0.0 on Windows
     (CPython does not populate them there) -- a probe built on them reads
     "0ms" for a real 400ms+ import and reports PASS unconditionally. This
     module never reads `os.times()`.
  2. Windows job-object accounting quantises to the ~15.6ms scheduler tick
     (values land on multiples of it: 0.0 / 15.6 / 31.2 / ...). A single-
     sample read anywhere near a 60ms bar measures tick noise, not cost.
     `batched_process_time_ms` amortises K invocations inside ONE job object
     and divides by K, recovering sub-tick resolution honestly -- this is
     the primitive a caller near the bar should use, not a single `measure`.
  3. `JobObjectBasicAndIoAccountingInformation` (info class 2) returns
     ERROR_BAD_LENGTH on this box; only `JobObjectBasicAccountingInformation`
     (class 1, no I/O counters) is queried here.

NEGATIVE SPEC: this module does not touch `coordinator_core.benchmarks.timer`
(the existing wall-clock spawn-to-exit primitive backing the qsub-01 latency
harness) -- that module answers a different, already-established question
(per-op wall-clock budget conformance against `time_invocation`'s own
contract) and stays wall-clock by design; this module is additive, a second
instrument for a different unit, not a replacement for the first.

Windows-only: `batched_process_time_ms` raises `NotImplementedError` off
Windows -- job objects are a Win32 primitive with no POSIX equivalent, and
DR-344's brightline is itself scoped to this box.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Optional, Sequence

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _JobObjectBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _CREATE_SUSPENDED = 0x00000004
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002


def _resume_all_threads(pid: int) -> None:
    """Resumes every thread of `pid` -- the child is spawned
    `CREATE_SUSPENDED` so it can be assigned to the job object BEFORE it
    executes anything chargeable; without this it never runs at all."""
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(_ThreadEntry32)
    found = _k32.Thread32First(snap, ctypes.byref(entry))
    while found:
        if entry.th32OwnerProcessID == pid:
            handle = _k32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
            if handle:
                _k32.ResumeThread(handle)
                _k32.CloseHandle(handle)
        found = _k32.Thread32Next(snap, ctypes.byref(entry))
    _k32.CloseHandle(snap)


def batched_process_time_ms(
    cmd: Sequence[str],
    k: int = 20,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Runs `cmd` `k` times inside ONE Windows job object and returns the
    per-invocation process time, amortised over `k` to beat the ~15.6ms
    scheduler-tick quantisation a single sample cannot (module docstring,
    trap 2).

    Returns:
        {
            "process_time_ms": float,  # (TotalUserTime + TotalKernelTime) / k
            "wall_ms": float,          # context only -- never gate on this
            "procs_per_call": float,   # TotalProcesses / k
            "rc": int,                 # last invocation's return code
            "k": int,
        }

    `rc` reports only the LAST invocation's exit code -- a caller that needs
    every invocation's exit status verified (e.g. AC9-style "an erroring
    invocation must not silently count as a valid sample") must check that
    itself; this primitive's job is the timing, not process health.

    Raises `NotImplementedError` off Windows, and `OSError`/`ctypes.WinError`
    on any job-object API failure (never silently degrades to a wrong unit).
    """
    if not IS_WINDOWS:
        raise NotImplementedError("batched_process_time_ms is a Windows job-object primitive")
    if k < 1:
        raise ValueError(f"batched_process_time_ms: k must be >= 1, got {k!r}")

    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        rc = 0
        t0 = time.perf_counter()
        for _ in range(k):
            proc = subprocess.Popen(
                list(cmd),
                env=env,
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_SUSPENDED,
            )
            if not _k32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            _resume_all_threads(proc.pid)
            rc = proc.wait()
        wall_ms = (time.perf_counter() - t0) * 1000.0 / k

        info = _JobObjectBasicAccountingInformation()
        if not _k32.QueryInformationJobObject(
            wintypes.HANDLE(job),
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        process_time_ms = (info.TotalUserTime + info.TotalKernelTime) / 10000.0 / k
        procs_per_call = info.TotalProcesses / k
    finally:
        _k32.CloseHandle(wintypes.HANDLE(job))

    return {
        "process_time_ms": round(process_time_ms, 3),
        "wall_ms": round(wall_ms, 3),
        "procs_per_call": round(procs_per_call, 3),
        "rc": rc,
        "k": k,
    }

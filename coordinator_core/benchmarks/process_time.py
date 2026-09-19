"""
coordinator_core.benchmarks.process_time -- process-time primitive, one per
platform DR-344's brightline actually runs on.

Purpose: DR-344's brightline gates on PROCESS TIME (user+kernel CPU time
across a spawned process tree), never wall clock -- CLAUDE.md's own § "The
brightline" is explicit that wall clock on this box measures peer load
(50-70 concurrent sessions is the design condition), not cost, and a gate
another concurrent session can move is the same bug wearing a new name.
This module is the single shared primitive for measuring it, with a
three-way platform dispatch: Windows (job object), Darwin (kqueue +
per-pid wait4), everything else (raises, naming which half is missing).

Three traps this module exists to avoid -- each already produced a false
PASS on this box (see state/handoffs/2026-08-21_103635_reaching-the-warm-
engine.md § Measured findings):

  1. `os.times().children_user`/`children_system` are ALWAYS 0.0 on Windows
     (CPython does not populate them there) -- a probe built on them reads
     "0ms" for a real 400ms+ import and reports PASS unconditionally. This
     is Windows-only: on POSIX the fields ARE populated but
     `_SC_CLK_TCK`-granular (n=1, illustrative: `getrusage` read 49.914ms
     in the same run `os.times()` reported 0.03/0.01 -- a silent
     GRANULARITY DOWNGRADE there, not a silent zero, still unusable near a
     60ms bar, still not the same failure as the Windows case). This
     module never reads `os.times()` on either platform.
  2. Windows job-object accounting quantises to the ~15.6ms scheduler tick
     (values land on multiples of it: 0.0 / 15.6 / 31.2 / ...). A single-
     sample read anywhere near a 60ms bar measures tick noise, not cost.
     `batched_process_time_ms` amortises K invocations and divides by K,
     recovering sub-tick resolution honestly -- this is the primitive a
     caller near the bar should use, not a single `measure`. On Darwin the
     K-batching is NOISE AMORTISATION only, not a resolution rescue --
     macOS process accounting is microsecond-precise and has no comparable
     tick to clear; batching there exists to smooth run-to-run jitter.
  3. `JobObjectBasicAndIoAccountingInformation` (info class 2) returns
     ERROR_BAD_LENGTH on this box; only `JobObjectBasicAccountingInformation`
     (class 1, no I/O counters) is queried here.

A fourth trap, Darwin-specific, caught during this chunk's own verification:
`getrusage(RUSAGE_CHILDREN)` is PROCESS-WIDE, not batch-scoped -- 200ms of
unrelated child CPU reaped by ANOTHER THREAD in the same measurement window
was charged into this instrument's own figure (0.766ms -> 1.421ms/call over
K=300). Cross-checking reap counts to detect that does NOT work: the
contaminating reap happens inside somebody else's `Popen.wait()`, invisible
to this module's own bookkeeping, and the two counts being compared (direct
reaps vs whole-tree) are different quantities regardless. The fix is
structural, not defensive: reap each invocation's root with `os.wait4()`
and read THAT CHILD'S OWN rusage (self plus whatever IT reaped) -- a figure
keyed to a pid, so another thread's child cannot enter it. This is the
same structural guarantee the Windows job object gives for free.

A fifth trap: never set `SIG_IGN` on `SIGCHLD` in the measuring process.
Both XNU and Linux deliberately DESTROY the CPU accounting for auto-reaped
children under that disposition (XNU compiles the `ruadd` accumulation out
in the `P_NOCLDWAIT` branch, citing POSIX) -- if a caller has done this,
`batched_process_time_ms` fails loud on Darwin rather than silently
under-reporting.

NEGATIVE SPEC: this module does not touch `coordinator_core.benchmarks.timer`
(the existing wall-clock spawn-to-exit primitive backing the qsub-01 latency
harness) -- that module answers a different, already-established question
(per-op wall-clock budget conformance against `time_invocation`'s own
contract) and stays wall-clock by design; this module is additive, a second
instrument for a different unit, not a replacement for the first.

LINUX (`batched_process_time_ms` only -- `single_invocation_tree_process_time`
still raises `NotImplementedError` there, unaddressed by this chunk). The
getrusage process-time half was already POSIX and verified against Linux's
own `kernel/exit.c :: wait_task_zombie()` rollup; the spawn-count half uses
`sys.addaudithook` (CPython 3.8+, stdlib, no new dependency) on the
`os.posix_spawn`/`os.fork`/`subprocess.Popen` events, installed inside a
freshly forked, single-purpose child (`_linux_run_measured_child`) so the
count and the `getrusage(RUSAGE_SELF)`+`getrusage(RUSAGE_CHILDREN)` read are
both scoped to that one child, the same contamination-free structure
Darwin's `os.wait4()`-keyed read has (trap 4 below).

FIDELITY GAP, STATED PLAINLY: the audit hook counts spawns *this Python
process* issues, at any call depth (the hook is process-global, not
call-stack-scoped) -- for a `sys.executable`-rooted `cmd` (the shape every
caller in this tree actually uses: measuring a Python function's own
subprocess usage) this has FULL fidelity, equal to Darwin's kqueue path,
because every spawn the measured code performs is a direct act of the
hooked interpreter. It does NOT see forking done *inside* a non-Python
process this child execs or `Popen`s -- that binary's own descendants, if
it has any, are invisible (no interpreter there to hook). Darwin's
kqueue/EVFILT_PROC mechanism sees the whole descendant tree regardless of
language and is NOT made redundant by this addition for that reason: a
caller measuring an arbitrary external binary's full process tree still
needs the Darwin path (or an equivalent not built here) rather than this
one. Windows/Darwin/Linux is a genuine three-way platform split, not a
two-way one with Linux glossed over. This split is THIS MODULE's own
implementation boundary, not DR-344's -- DR-344's brightline itself
contains no Windows or POSIX scoping (checked against the ruling text
directly); the module previously glossed this split as if the ruling were
scoped to this box, which made the platform gap below look intentional
when it is not.

PER-PLATFORM: WHAT IS NOT MEASURED (AC9). This module answers "what did
the process tree cost," never "was every reap counted" -- on macOS, that
second question has a hole this module cannot close:

  - Darwin: orphaned or unreaped descendant CPU is lost PERMANENTLY, not
    late. Measured directly across n=4 orphan variants (nowait at depth 2
    and depth 3, double-fork at depth 2 and depth 3): level 1 read ~61.4ms
    against full-tree rollups ranging 105-118ms across the four variants,
    and a 0.5s settle window did not recover the missing CPU time
    afterward. macOS has no
    `PR_SET_CHILD_SUBREAPER` (a Linux-only prctl) -- there is no mechanism
    on this platform to re-parent an orphan under this instrument's own
    reaper, so this hole CANNOT be closed on the fleet floor. A reader of
    this module must not assume a future patch closes it here.
  - Windows: the job-object mechanism accounts every process ever assigned
    to the job for the job's lifetime, including ones that outlive their
    immediate parent, so orphaning within the job is not the same open
    question -- but job-object accounting is still tick-quantised (trap 2
    above) and still silently excludes any process a misbehaving child
    manages to launch OUTSIDE the job (e.g. via `CREATE_BREAKAWAY_FROM_JOB`
    on a job not configured to deny it); this module does not verify job
    breakaway is denied.
  - Linux (`batched_process_time_ms`): a spawn issued by a non-Python
    process this child execs/`Popen`s is invisible (no interpreter there
    for `sys.addaudithook` to run in) -- for the `sys.executable`-rooted
    invocations this tree actually measures, nothing is missed, but a
    caller pointing this primitive at an arbitrary external binary and
    expecting tree-wide descendant visibility gets an undercount of
    anything that binary forks itself. `single_invocation_tree_process_time`
    remains unimplemented on Linux (`NotImplementedError`), which is
    itself the honest answer for that function on this platform rather
    than a silent zero.

A SIXTH TRAP, and the only one that is not a leak: measuring a WARM-ENGINE
op through the CLI door measures the DOOR, not the op. The door
(`coordinator-invoke`) is an IPC client -- it dials a warm server that was
already running, and a pre-existing process was never assigned to this
module's job object, so every subprocess the op spawns and every millisecond
it burns happen outside the measured tree. The read is real, small, and
answers a question nobody asked.

Measured, not hypothesised: `coordinator-invoke push.outstanding` read
21.875ms at 2.0 procs/call through the door, while the same op measured
IN-PROCESS costs UNDER 1ms at 0.00 procs/call -- the door reading is mostly
the client's own interpreter start and contains none of the op
(`state/audits/2026-08-31-push-outstanding-lands-under-the-bar-in-process-
time.md`).

Unlike the five traps above this is not a defect in the mechanism: the work
is CORRECTLY outside the job, because it belongs to a process this
measurement did not create. The defect is only ever in the caller's choice of
what to spawn. The consequence is the same one this module exists to prevent
-- an unconditional PASS, since the door's cost is bounded and roughly
constant no matter how expensive the op behind it becomes.

The tell is `procs_per_call`: an op known to spawn git reading 2.0 procs/call
(interpreter plus one child) cannot have run inside the measurement. Measure
a warm-engine op by importing and calling it in the spawned process, against
an import-only baseline of the same shape, and take the delta.
"""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import select
import signal
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from typing import Optional, Sequence

IS_WINDOWS = sys.platform == "win32"
IS_DARWIN = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

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
    # Without this every measured child allocates a conhost window. This module
    # measures git, and a k-batched call spawns k of them, so the omission cost
    # a window per sample on every budget measurement in the repo.
    _CREATE_NO_WINDOW = 0x08000000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002


if IS_DARWIN:
    _libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)

    # -- posix_spawn / posix_spawnattr -----------------------------------
    # os.posix_spawn exposes no raw attr-flag argument (its signature is
    # file_actions/setpgroup/resetids/setsid/setsigmask/setsigdef/scheduler
    # only), so POSIX_SPAWN_START_SUSPENDED genuinely requires this ctypes
    # route -- the real justification for ctypes here, alongside the
    # module's existing Windows ctypes usage (dispatch brief).
    _POSIX_SPAWN_START_SUSPENDED = 0x0080

    _libc.posix_spawnattr_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    _libc.posix_spawnattr_init.restype = ctypes.c_int
    _libc.posix_spawnattr_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    _libc.posix_spawnattr_destroy.restype = ctypes.c_int
    _libc.posix_spawnattr_setflags.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_short]
    _libc.posix_spawnattr_setflags.restype = ctypes.c_int
    _libc.posix_spawnp.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_char_p),
    ]
    _libc.posix_spawnp.restype = ctypes.c_int

    # -- kqueue / kevent (EVFILT_PROC) ------------------------------------
    _EVFILT_PROC = -5
    _NOTE_EXIT = 0x80000000
    _NOTE_FORK = 0x40000000
    # NOTE_TRACK (0x1) is ENOTSUP on this kernel -- verified, EV_ERROR
    # data=45. Not used, and not attempted-then-fallen-back-from: the
    # NOTE_FORK enumeration path below is the only path (dispatch brief).
    _EV_ADD = 0x0001
    _EV_ENABLE = 0x0004
    _EV_RECEIPT = 0x0040
    _EV_ERROR = 0x4000

    class _Kevent(ctypes.Structure):
        _fields_ = [
            ("ident", ctypes.c_ulong),
            ("filter", ctypes.c_short),
            ("flags", ctypes.c_ushort),
            ("fflags", ctypes.c_uint),
            ("data", ctypes.c_long),
            ("udata", ctypes.c_void_p),
        ]

    class _Timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    assert ctypes.sizeof(_Kevent) == 32, (
        "struct kevent layout drifted off the verified 32-byte arm64 shape "
        "this module's ctypes binding was hand-packed against"
    )

    _libc.kqueue.argtypes = []
    _libc.kqueue.restype = ctypes.c_int
    _libc.kevent.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(_Kevent),
        ctypes.c_int,
        ctypes.POINTER(_Kevent),
        ctypes.c_int,
        ctypes.POINTER(_Timespec),
    ]
    _libc.kevent.restype = ctypes.c_int

    # -- libproc -----------------------------------------------------------
    _libc.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    _libc.proc_listchildpids.restype = ctypes.c_int


def _env_with_benchmark_origin(base_env: Optional[dict]) -> dict:
    """Builds an env dict for the SPAWNED CHILD with the benchmark origin
    declared -- never mutates `os.environ` (the caller's own process).

    C1d: `declare_benchmark_origin()` writes ORIGIN_ENV into the
    interpreter-global `os.environ`, correct at a driver's own entry but
    wrong here -- `batched_process_time_ms` and
    `single_invocation_tree_process_time` are library spawn helpers any
    caller may invoke mid-process, and a global write would stamp the
    caller's whole process (and every later subprocess it spawns) as
    benchmark traffic. This builds a child-scoped copy instead: of
    `base_env` if the caller supplied one, else of this process's own
    `os.environ` (the previous implicit-inherit behaviour when `env` was
    passed through as `None`), with the origin layered on top.

    Precedence preserved: `setdefault` means an ORIGIN_ENV already present
    in the base env wins, matching `declare_benchmark_origin`'s own
    non-overwriting contract.
    """
    from coordinator_core.telemetry import op_latency

    env = dict(os.environ) if base_env is None else dict(base_env)
    env.setdefault(op_latency.ORIGIN_ENV, op_latency.BENCHMARK)
    return env


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


def _windows_query_job_accounting(job) -> "_JobObjectBasicAccountingInformation":
    """Reads the job's basic accounting block (info class 1 -- class 2
    returns ERROR_BAD_LENGTH on this box, module docstring trap 3)."""
    info = _JobObjectBasicAccountingInformation()
    if not _k32.QueryInformationJobObject(
        wintypes.HANDLE(job),
        _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return info


def _windows_spawn_into_job(
    job,
    cmd: Sequence[str],
    env: Optional[dict],
    cwd: Optional[str],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
) -> int:
    """Spawns `cmd` CREATE_SUSPENDED, assigns it to `job` before it can
    execute anything chargeable, resumes it, and waits. Returns its rc.

    The single Windows spawn mechanism in this module: both the k-batched
    external-command path and the single-invocation tree path below go
    through here, so there is exactly ONE job-accounting instrument to
    reason about rather than a sibling that drifts from it.
    """
    proc = subprocess.Popen(
        list(cmd),
        env=env,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW,
    )
    if not _k32.AssignProcessToJobObject(
        wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    _resume_all_threads(proc.pid)
    return proc.wait()


class LiveTreeAccountant:
    """Job-object accounting for an ALREADY-RUNNING process tree, read as a
    DELTA across an interval rather than as a total.

    The gap this closes: `batched_process_time_ms` and
    `single_invocation_tree_process_time` both measure a command this module
    spawns, so every process they account for is a descendant of that spawn.
    A WARM-SERVER op is not shaped that way. The client the caller runs is a
    JSON-RPC framer over a pipe; the op's real work -- and every `git` child
    it spawns, and every `conhost.exe` Windows allocates alongside one
    (DR-373) -- is charged to the long-lived SERVER process, which is not in
    the client's tree at all. Measuring the client and calling it the op's
    process time reports the framer and presents it as the op.

    Usage: attach once to the server pid, then bracket each invocation with
    `snapshot()` and subtract. Anything the server charged BEFORE attachment
    (boot, `_preload_op_registry`'s ~703ms of imports) is excluded from both
    readings by construction, so the delta is the interval's cost and never
    an amortised share of startup.

    Children inherit job membership at creation, so a `git` spawned after
    attachment is counted; one spawned before it is not. A detached listener
    started at server boot is therefore outside the accounting -- correct,
    since it is a boot cost and not a per-op one.

    NOT a second accounting mechanism: this reuses
    `_windows_query_job_accounting` and the same
    `_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION` info class the batched and
    single-invocation paths already read, so there is one job instrument in
    this module, not a sibling that drifts from it.

    QUANTISATION — READ THIS BEFORE BRACKETING A SINGLE CALL. Job accounting
    lands on ~15.6ms scheduler ticks, so a `snapshot()` pair around ONE
    invocation can only ever return a multiple of the tick: it reports a tick
    COUNT, not a cost. Worse, a median over tick-quantised per-call samples
    picks the low mode rather than the mean, so the error is biased downward
    and does not average out by taking more samples the same way.

    Two figures published from this module read exactly 15.62ms and 31.25ms --
    1x and 2x the tick -- and both were retracted (`001b0a669`); the re-run
    bracketed showed the second was really ~51ms, nearly double, and the error
    ran AGAINST the route being argued for. That is the shape to expect: a
    number landing suspiciously close to a tick multiple is evidence of the
    instrument, not of the work.

    BRACKET N CALLS INSIDE ONE WINDOW AND DIVIDE. One `snapshot()` before N
    invocations, one after, `/ N` -- that divides the quantisation error by N
    rather than paying it per sample. Repeat the whole window a few times and
    report the spread; do not median per-call deltas. Below roughly a 200ms
    bar, per-call bracketing is not a measurement.

    Windows only -- `NotImplementedError` elsewhere, never a silent degrade
    to a wrong unit.
    """

    def __init__(self, pid: int) -> None:
        if not IS_WINDOWS:
            raise NotImplementedError(
                "LiveTreeAccountant is Windows-only (job-object accounting); "
                f"no implementation for {sys.platform}"
            )
        self.pid = int(pid)
        self._job = _k32.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        handle = _k32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, self.pid
        )
        if not handle:
            _k32.CloseHandle(wintypes.HANDLE(self._job))
            self._job = None
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _k32.AssignProcessToJobObject(
                wintypes.HANDLE(self._job), wintypes.HANDLE(handle)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _k32.CloseHandle(wintypes.HANDLE(handle))

    def snapshot(self) -> dict:
        """Cumulative job accounting since attachment.

        Returns `{"process_time_ms": float, "procs": int}` -- user+kernel CPU
        across every process in the job (terminated ones included; the job
        keeps charging them), and the job's `TotalProcesses`, which counts
        the attached root itself.
        """
        info = _windows_query_job_accounting(self._job)
        return {
            "process_time_ms": (info.TotalUserTime + info.TotalKernelTime) / 10000.0,
            "procs": int(info.TotalProcesses),
        }

    def close(self) -> None:
        """Releases the job handle. Does NOT terminate anything: no
        `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is set, so the server and its
        children outlive the accountant -- closing the instrument must never
        be able to kill the thing being measured."""
        if self._job:
            _k32.CloseHandle(wintypes.HANDLE(self._job))
            self._job = None

    def __enter__(self) -> "LiveTreeAccountant":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _windows_batched_process_time_ms(
    cmd: Sequence[str],
    k: int,
    env: Optional[dict],
    cwd: Optional[str],
) -> dict:
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        rc = 0
        t0 = time.perf_counter()
        for _ in range(k):
            rc = _windows_spawn_into_job(job, cmd, env, cwd)
        wall_ms = (time.perf_counter() - t0) * 1000.0 / k

        info = _windows_query_job_accounting(job)
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


def _proc_listchildpids(ppid: int) -> list:
    """Two-call discipline (AC5): `proc_listchildpids` has no NULL-buffer
    size-probe convention (unlike the `proc_listpids` family it otherwise
    resembles) -- verified empirically: a NULL/0 call returns an
    unrelated positive garbage value, not a real size. So this probes with
    a generously sized buffer instead, and re-queries with a larger one if
    the returned COUNT (this call returns a pid count, not a byte count --
    also verified empirically) exactly fills the buffer, since a fixed-size
    buffer TRUNCATES SILENTLY on wide fan-out otherwise -- a naive 64- or
    128-entry guess sits right at the edge of a 200-grandchild fixture,
    returning a confident undercount with no error, exactly the failure
    class this instrument exists to avoid (dispatch brief)."""
    capacity = 128
    while True:
        buf = (ctypes.c_int * capacity)()
        got = _libc.proc_listchildpids(ppid, buf, ctypes.sizeof(buf))
        if got < 0:
            raise OSError(ctypes.get_errno(), "proc_listchildpids failed")
        if got < capacity:
            return [buf[i] for i in range(got)]
        capacity *= 4


def _posix_spawnp_suspended(argv: list, env: Optional[dict]) -> int:
    """Spawns `argv[0]` via `posix_spawnp` under `POSIX_SPAWN_START_SUSPENDED`
    so no descendant can be born before kevent registration lands -- the
    POSIX counterpart of the Windows path's `CREATE_SUSPENDED` ordering
    discipline (dispatch brief)."""
    attr = ctypes.c_void_p()
    rc = _libc.posix_spawnattr_init(ctypes.byref(attr))
    if rc != 0:
        raise OSError(rc, "posix_spawnattr_init failed")
    try:
        rc = _libc.posix_spawnattr_setflags(ctypes.byref(attr), _POSIX_SPAWN_START_SUSPENDED)
        if rc != 0:
            raise OSError(rc, "posix_spawnattr_setflags(START_SUSPENDED) failed")

        pid = ctypes.c_int(0)
        argv_enc = [a.encode() for a in argv] + [None]
        argv_arr = (ctypes.c_char_p * len(argv_enc))(*argv_enc)
        env_map = os.environ if env is None else env
        env_enc = [f"{k}={v}".encode() for k, v in env_map.items()] + [None]
        env_arr = (ctypes.c_char_p * len(env_enc))(*env_enc)

        rc = _libc.posix_spawnp(
            ctypes.byref(pid), argv[0].encode(), None, ctypes.byref(attr), argv_arr, env_arr
        )
        if rc != 0:
            raise OSError(rc, f"posix_spawnp failed for {argv!r}")
        return pid.value
    finally:
        _libc.posix_spawnattr_destroy(ctypes.byref(attr))


def _kevent_register(kq: int, pid: int) -> bool:
    """Registers `EVFILT_PROC`/`NOTE_EXIT|NOTE_FORK` for `pid`. Returns
    False (never raises) only on ESRCH -- the process already exited before
    registration landed, a retryable condition the caller bounds.

    AC6: registering with `nevents=0` on the eventlist returns 0 with errno
    clear even when the kernel REJECTED the registration -- an inert
    kqueue and `procs_per_call == 1`, indistinguishable from "this command
    spawns nothing" (hit for real during verification). Every registration
    here therefore passes a live eventlist and inspects `EV_ERROR`, never a
    bare 0/0 call.

    Review finding F1 (EM-confirmed): a plain 1-slot eventlist poll here is
    NOT scoped to this registration's own changelist entry -- kevent(2)'s
    eventlist half drains ANY pending event already queued on `kq`, so this
    call could silently steal a different, already-registered pid's real
    NOTE_FORK/NOTE_EXIT and discard it (undercount or hang with no signal).
    EV_RECEIPT (0x0040, confirmed against
    MacOSX.sdk/usr/include/sys/event.h) is the BSD idiom that gives AC6's
    "every registration produces a synchronous result" guarantee WITHOUT
    draining unrelated events: it forces this changelist entry's own result
    into the eventlist, and per event(2)/kqueue semantics a SUCCESSFUL
    EV_RECEIPT registration reports EV_ERROR set with data == 0 (0 is not
    an error code here -- it is the success sentinel), not the absence of
    EV_ERROR."""
    change = _Kevent()
    change.ident = pid
    change.filter = _EVFILT_PROC
    change.flags = _EV_ADD | _EV_ENABLE | _EV_RECEIPT
    change.fflags = _NOTE_EXIT | _NOTE_FORK
    change.data = 0
    change.udata = None
    out = _Kevent()
    # A zero timespec (poll, don't block) is still passed for symmetry with
    # the rest of this module's kevent calls, though EV_RECEIPT makes the
    # result synchronous regardless of timeout.
    zero_timeout = _Timespec(0, 0)
    n = _libc.kevent(kq, ctypes.byref(change), 1, ctypes.byref(out), 1, ctypes.byref(zero_timeout))
    if n < 0:
        errno_val = ctypes.get_errno()
        if errno_val == errno.ESRCH:
            return False
        raise OSError(errno_val, f"kevent registration syscall failed for pid {pid}")
    if n == 0 or not (out.flags & _EV_ERROR):
        # EV_RECEIPT guarantees a synchronous EV_ERROR-flagged result for
        # this exact changelist entry -- anything else means the kernel
        # did not honor EV_RECEIPT the way this module relies on, and
        # AC6's guarantee (no silent inert kqueue) no longer holds.
        raise OSError(
            0,
            f"kevent EV_RECEIPT registration for pid {pid} returned no result "
            f"(n={n}) -- cannot confirm registration succeeded",
        )
    if out.data == 0:
        return True
    if out.data == errno.ESRCH:
        return False
    raise OSError(int(out.data), f"kevent registration rejected for pid {pid} (EV_RECEIPT data={out.data})")


def _kevent_register_with_retry(kq: int, pid: int, max_retries: int = 5) -> bool:
    """AC7 disposition: bounded retry on ESRCH. Attach failure at this rate
    (~33% observed on the highest-fan-out `burst` fixture during
    verification) is a real undercount channel if left as a bare
    fail-loud per pid, so callers get a bounded number of chances here
    before an unresolved residual is rolled up and RAISED by the batched
    primitive (`_darwin_batched_process_time_ms`), never returned as a
    silent lower bound."""
    ok = _kevent_register(kq, pid)
    retries = 0
    while not ok and retries < max_retries:
        retries += 1
        ok = _kevent_register(kq, pid)
    return ok


def _darwin_one_invocation(cmd: Sequence[str], env: Optional[dict], cwd: Optional[str]):
    """Runs `cmd` once, scoped to exactly the process tree it spawns.

    Returns (process_time_ms, procs_seen, attach_failed, rc).
    """
    pre_children = _proc_listchildpids(os.getpid())
    if pre_children:
        raise RuntimeError(
            f"process_time window-open assertion failed: os.getpid() already "
            f"has children {pre_children} before this invocation spawned anything"
        )

    kq = _libc.kqueue()
    if kq < 0:
        raise OSError(ctypes.get_errno(), "kqueue() failed")

    try:
        old_cwd = None
        if cwd is not None:
            old_cwd = os.getcwd()
            os.chdir(cwd)
        try:
            root_pid = _posix_spawnp_suspended(list(cmd), env)
        finally:
            if old_cwd is not None:
                os.chdir(old_cwd)

        # F2 (EM-confirmed): every path from here to the wait4() reap below
        # must not leak a live or suspended root/subtree on error -- the
        # root is spawned POSIX_SPAWN_START_SUSPENDED and only SIGCONT'd a
        # few lines down, so an exception before that leaves it suspended
        # forever, and an exception after SIGCONT leaves a live tree
        # running, unreaped, contaminating the next invocation's
        # window-open assertion. `reaped` tracks whether wait4()/waitpid()
        # already ran normally so this finally never double-reaps.
        reaped = False
        try:
            # AC7: attach the kevent, THEN record the pid -- inverted from
            # the naive order, so an attach failure on the root is a hard
            # retryable error rather than a silent subtree loss (dispatch
            # brief).
            if not _kevent_register_with_retry(kq, root_pid):
                os.kill(root_pid, signal.SIGKILL)
                os.waitpid(root_pid, 0)
                reaped = True
                raise RuntimeError(
                    f"process_time: could not attach kevent to root pid {root_pid} "
                    "after bounded retry -- refusing to measure a window we cannot observe"
                )

            seen = {root_pid}
            exited = set()
            attach_failed = 0

            os.kill(root_pid, signal.SIGCONT)

            events = (_Kevent * 8)()
            while root_pid not in exited:
                n = _libc.kevent(kq, None, 0, events, len(events), None)
                if n < 0:
                    raise OSError(ctypes.get_errno(), "kevent wait failed")
                for i in range(n):
                    ev = events[i]
                    pid = ev.ident
                    if ev.flags & _EV_ERROR:
                        # F3: defensive only. EV_ERROR on this eventlist is
                        # documented as arising from changelist processing
                        # during registration (a submitted nchanges entry),
                        # never from this pure nchanges=0 data-retrieval
                        # call -- kept as a guard, not a steady-state path.
                        continue
                    if ev.fflags & _NOTE_FORK:
                        # Dedupe by pid: this is what makes the count
                        # structurally immune to the double-count defect in
                        # state/lessons/2026-08-19-a-spawn-counting-instrument-
                        # lies-twice-before-it-tells-the-truth.md (which bit an
                        # instrument wrapping both subprocess.run and Popen).
                        for child in _proc_listchildpids(pid):
                            if child in seen:
                                continue
                            if _kevent_register_with_retry(kq, child):
                                seen.add(child)
                            else:
                                attach_failed += 1
                    if ev.fflags & _NOTE_EXIT:
                        exited.add(pid)

            _reaped_pid, status, rusage = os.wait4(root_pid, 0)
            reaped = True
            # PROCESS-TIME SCOPING (AC4): this rusage is keyed to root_pid --
            # self plus whatever root_pid itself reaped -- so it cannot be
            # contaminated by another thread's unrelated child exiting in the
            # same window, unlike getrusage(RUSAGE_CHILDREN) (module docstring).
            process_time_ms = (rusage.ru_utime + rusage.ru_stime) * 1000.0
            if hasattr(os, "waitstatus_to_exitcode"):
                rc = os.waitstatus_to_exitcode(status)
            else:  # pragma: no cover - py<3.9 fallback
                rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1

            post_children = _proc_listchildpids(os.getpid())
            if post_children:
                raise RuntimeError(
                    f"process_time window-close assertion failed: os.getpid() "
                    f"still has children {post_children} after root {root_pid} exited"
                )

            return process_time_ms, len(seen), attach_failed, rc
        finally:
            if not reaped:
                try:
                    os.kill(root_pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    os.waitpid(root_pid, 0)
                except OSError:
                    pass
    finally:
        os.close(kq)


def _darwin_batched_process_time_ms(
    cmd: Sequence[str],
    k: int,
    env: Optional[dict],
    cwd: Optional[str],
) -> dict:
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise RuntimeError(
            "process_time: SIGCHLD is SIG_IGN in this process -- both XNU "
            "and Linux destroy CPU accounting for auto-reaped children "
            "under that disposition (module docstring); refusing to "
            "silently under-report rather than measuring through it"
        )

    total_process_time_ms = 0.0
    total_procs = 0
    total_attach_failed = 0
    rc = 0
    t0 = time.perf_counter()
    for _ in range(k):
        process_time_ms, procs, attach_failed, rc = _darwin_one_invocation(cmd, env, cwd)
        total_process_time_ms += process_time_ms
        total_procs += procs
        total_attach_failed += attach_failed
    wall_ms = (time.perf_counter() - t0) * 1000.0 / k

    if total_attach_failed:
        # AC7: the primitive raises on an unresolved non-zero residual --
        # never a per-call-site obligation. total_procs is a LOWER BOUND
        # here, not the real count: some subtree exited unobserved.
        raise RuntimeError(
            f"process_time: attach_failed={total_attach_failed} after bounded "
            f"retry -- procs_per_call would be {round(total_procs / k, 3)} "
            "(LOWER BOUND, not exact). Refusing to return an undercount."
        )

    return {
        "process_time_ms": round(total_process_time_ms / k, 3),
        "wall_ms": round(wall_ms, 3),
        "procs_per_call": round(total_procs / k, 3),
        "rc": rc,
        "k": k,
    }


_LINUX_AUDIT_SPAWN_EVENTS = frozenset({"os.posix_spawn", "os.fork", "subprocess.Popen"})
"""The three stdlib audit events a spawn can surface through on CPython
3.8+ (`sys.addaudithook`): `subprocess.Popen` (the common path, fired once
per `Popen.__init__` regardless of which low-level primitive it uses
underneath), plus the two lower-level primitives a caller could reach
directly without going through `subprocess` at all. Verified empirically
in this container (all three fire on a bare `os.fork`/`os.posix_spawn`/
`subprocess.run` call respectively).

Review: reviewer (F2) -- this no-double-count guarantee is CONDITIONAL, not
a general build fact as previously claimed. CPython's `Popen._execute_child`
only takes the internal `self._posix_spawn(...)` fast path (itself calling
the audited `os.posix_spawn()`) when `close_fds=False`; with that argument,
BOTH `subprocess.Popen` and `os.posix_spawn` fire for the same one real
process (reproduced directly: `subprocess.run(['/bin/true'], close_fds=False)`
raised both events). Every `close_fds=` call site in this repo today is
explicitly `True`, so no in-tree caller trips this, but a caller of this
general-purpose primitive that uses `close_fds=False` is double-counted for
that spawn. Over-counting is the safe direction for a spawn-count budget
(never a silent undercount), so this is left unfixed rather than
special-cased, but the claim is corrected to state its actual scope."""

_LINUX_READ_LOOP_TIMEOUT_S = 30.0
"""Bound on `_linux_one_invocation`'s pipe-EOF read loop (review finding F1).
EOF alone is not decidable in bounded time if the measured child forked a
raw (non-exec) descendant that outlives it and holds `write_fd` open --
there is no fd-level fix (CLOEXEC only trips at execve(), not fork()), so
the read loop is bounded instead of trusting EOF unconditionally. Generous
relative to the 500ms brightline (module docstring) because this measures
an arbitrary caller-supplied `cmd`, not just brightline-scoped ops."""


def _linux_run_measured_child(
    cmd: Sequence[str], env: Optional[dict], cwd: Optional[str], write_fd: int
) -> None:
    """Runs in the freshly forked, not-yet-measured child (the LINUX
    counterpart of `_darwin_one_invocation`'s `root_pid`). Never returns --
    always exits via `os._exit`, so no atexit/cleanup from the parent's own
    process state can run twice.

    FIDELITY, STATED PLAINLY: the audit hook installed here counts every
    `os.posix_spawn`/`os.fork`/`subprocess.Popen` call *this Python
    process* issues, directly or from arbitrarily deep inside its own call
    stack (the hook is process-global, not call-stack-scoped) -- for the
    dominant real use in this repo, a `sys.executable`-rooted invocation
    whose own Python code is what's under measurement (e.g. "does
    `brief()` spawn any git calls"), this has FULL fidelity: every spawn
    that code issues, at any call depth, is seen. What it does NOT see:
    a spawn issued by a NON-PYTHON process this child itself execs or
    subprocess.Popen's (e.g. if `cmd` names an external binary, or if
    measured Python code shells out to one) -- that binary's OWN internal
    forking, if any, happens in a separate process with no audit hook and
    is invisible here. This is narrower than Darwin's kqueue/EVFILT_PROC
    path, which sees the whole descendant tree regardless of language.
    Where `cmd` names a `sys.executable` invocation (the shape every
    zero-spawn-census caller in this tree actually uses), that gap cannot
    be entered: the count already includes everything the measured Python
    code itself does.
    """
    spawn_count = 0

    def _hook(event: str, args) -> None:
        nonlocal spawn_count
        if event in _LINUX_AUDIT_SPAWN_EVENTS:
            spawn_count += 1

    rc = 0
    try:
        if cwd is not None:
            os.chdir(cwd)
        if env is not None:
            os.environ.clear()
            os.environ.update(env)

        sys.addaudithook(_hook)

        is_python_root = bool(cmd) and cmd[0] == sys.executable
        if is_python_root and len(cmd) >= 2 and cmd[1] == "-c":
            code = cmd[2] if len(cmd) >= 3 else ""
            sys.argv = ["-c", *cmd[3:]]
            try:
                exec(compile(code, "<batched_process_time_ms -c>", "exec"), {"__name__": "__main__"})
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        elif is_python_root and len(cmd) >= 2:
            import runpy

            sys.argv = list(cmd[1:])
            try:
                runpy.run_path(cmd[1], run_name="__main__")
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        elif is_python_root:
            # Review: reviewer (F6) -- `cmd == [sys.executable]` (len 1) matched
            # neither branch above (both require len(cmd) >= 2) and fell through
            # to the non-Python-root `subprocess.run` branch, launching a bare
            # interactive REPL that can hang waiting on stdin. Fail loud instead.
            raise ValueError(
                f"process_time: malformed cmd {cmd!r} -- a sys.executable-rooted "
                "cmd must be either ['python', '-c', code, ...] or "
                "['python', script_path, ...], never sys.executable alone"
            )
        else:
            # Non-Python root: this process's own act of launching `cmd` is
            # itself one spawn (counted via the `subprocess.Popen` audit
            # event above). NOTE (Review: reviewer F4): this is NOT the same
            # figure Darwin reports for the identical invocation -- Darwin's
            # `_darwin_one_invocation` execs the forked root directly into
            # `cmd`, so `root_pid` IS the command process and `len(seen) ==
            # 1`; here this process stays alive as a wrapper around
            # `subprocess.run`, so the total is 2 (wrapper + command), one
            # higher than Darwin's for the same invocation. Not "matching."
            completed = subprocess.run(list(cmd))
            rc = completed.returncode
    except BaseException:
        traceback.print_exc()
        rc = 1
    finally:
        # Review: reviewer (F5) -- os.write can itself raise (e.g.
        # BrokenPipeError if the parent's read end is already gone), and that
        # exception was previously unguarded here, skipping os._exit(0) and
        # letting the forked child fall through into a full unhandled-exception
        # unwind of the copied parent process. Guard the write/close so this
        # child always terminates via os._exit no matter what the payload
        # write does.
        try:
            import resource

            ru_self = resource.getrusage(resource.RUSAGE_SELF)
            ru_children = resource.getrusage(resource.RUSAGE_CHILDREN)
            process_time_ms = (
                ru_self.ru_utime + ru_self.ru_stime + ru_children.ru_utime + ru_children.ru_stime
            ) * 1000.0
            payload = json.dumps(
                {"process_time_ms": process_time_ms, "procs": spawn_count + 1, "rc": int(rc)}
            ).encode("utf-8")
            os.write(write_fd, payload)
        except BaseException:
            traceback.print_exc()
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            os._exit(0)


def _linux_one_invocation(cmd: Sequence[str], env: Optional[dict], cwd: Optional[str]) -> dict:
    """Forks a dedicated, single-purpose child to run `cmd` (or, for a
    `sys.executable`-rooted `cmd`, runs its code IN that child rather than
    spawning a further process for it -- see `_linux_run_measured_child`).

    Structurally contamination-free the way Darwin's `os.wait4()`-keyed
    read is (module docstring, trap 4): this child exists for exactly one
    invocation and nothing else runs in it concurrently, so
    `getrusage(RUSAGE_SELF)` + `getrusage(RUSAGE_CHILDREN)` read INSIDE
    that child cannot be contaminated by an unrelated thread's child
    reaped elsewhere in this (the caller's) process -- there is no
    "elsewhere" in a process that only ever does this one thing.
    """
    read_fd, write_fd = os.pipe()
    # Review: reviewer (F3) -- os.fork() itself can raise (EAGAIN under
    # RLIMIT_NPROC/memory pressure, exactly the condition most likely to make
    # fork fail, since this runs in a k-iteration loop). Previously both fds
    # leaked on that path since neither the child branch nor the parent's
    # os.close(write_fd) below ever ran. Close both before propagating.
    try:
        pid = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:
        os.close(read_fd)
        _linux_run_measured_child(cmd, env, cwd, write_fd)
        os._exit(1)  # pragma: no cover - _linux_run_measured_child always exits first

    os.close(write_fd)
    chunks = []
    # Review: reviewer (F1, P1 -- demonstrated hang) -- relying on EOF alone
    # hangs indefinitely if the measured child forks a raw (non-exec)
    # descendant that outlives it: CLOEXEC only trips at execve(), never at
    # fork(), so the descendant inherits write_fd and keeps the pipe open
    # long after the root child (and its payload write) is done. Reproduced:
    # a 3s grandchild sleep hung this loop for the full 3s with no bound.
    # There is no fd-level fix for a fork-without-exec descendant, so this
    # bounds the read loop itself rather than trusting EOF unconditionally.
    deadline = time.monotonic() + _LINUX_READ_LOOP_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.close(read_fd)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
            raise RuntimeError(
                f"process_time: Linux measured child's read loop exceeded "
                f"{_LINUX_READ_LOOP_TIMEOUT_S}s without EOF -- likely an "
                "orphaned descendant (a raw os.fork() without exec) still "
                "holding the measurement pipe's write end open past the "
                "root child's own exit"
            )
        ready, _, _ = select.select([read_fd], [], [], remaining)
        if not ready:
            continue
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    _, _status = os.waitpid(pid, 0)

    if not chunks:
        raise RuntimeError(
            "process_time: Linux measured child exited without reporting a "
            "result -- it crashed before its own finally block could run "
            "(e.g. os.fork()/pipe failure), so no process-time/spawn-count "
            "figure exists for this invocation"
        )

    return json.loads(b"".join(chunks).decode("utf-8"))


def _linux_batched_process_time_ms(
    cmd: Sequence[str],
    k: int,
    env: Optional[dict],
    cwd: Optional[str],
) -> dict:
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise RuntimeError(
            "process_time: SIGCHLD is SIG_IGN in this process -- both XNU "
            "and Linux destroy CPU accounting for auto-reaped children "
            "under that disposition (module docstring); refusing to "
            "silently under-report rather than measuring through it"
        )

    total_process_time_ms = 0.0
    total_procs = 0
    rc = 0
    t0 = time.perf_counter()
    for _ in range(k):
        result = _linux_one_invocation(cmd, env, cwd)
        total_process_time_ms += result["process_time_ms"]
        total_procs += result["procs"]
        rc = result["rc"]
    wall_ms = (time.perf_counter() - t0) * 1000.0 / k

    return {
        "process_time_ms": round(total_process_time_ms / k, 3),
        "wall_ms": round(wall_ms, 3),
        "procs_per_call": round(total_procs / k, 3),
        "rc": rc,
        "k": k,
    }


def batched_process_time_ms(
    cmd: Sequence[str],
    k: int = 20,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Runs `cmd` `k` times and returns the per-invocation process time,
    amortised over `k`. On Windows this beats the ~15.6ms scheduler-tick
    quantisation a single sample cannot; on Darwin, where accounting is
    already microsecond-precise, `k` amortises run-to-run jitter instead
    (module docstring, trap 2).

    Returns:
        {
            "process_time_ms": float,  # user+kernel CPU time / k
            "wall_ms": float,          # context only -- never gate on this
            "procs_per_call": float,   # distinct pids spawned / k
            "rc": int,                 # last invocation's return code
            "k": int,
        }

    `rc` reports only the LAST invocation's exit code -- a caller that needs
    every invocation's exit status verified (e.g. AC9-style "an erroring
    invocation must not silently count as a valid sample") must check that
    itself; this primitive's job is the timing, not process health.

    Raises `NotImplementedError` on platforms with neither primitive, and
    `OSError`/`ctypes.WinError`/`RuntimeError` on any measurement-mechanism
    failure (never silently degrades to a wrong unit). On Darwin, an
    unresolved non-zero `attach_failed` residual after the bounded kevent
    retry (AC7) RAISES `RuntimeError` rather than returning a lower-bound
    count -- pre-EV_RECEIPT-fix this residual was returned silently at ~20%
    of runs (4/20, k=20) on this box's 500-iteration immediate-reap
    adversary; post-fix but pre-EV_RECEIPT-registration-fix, re-measured at
    n=54 (24 + 30 across two runs), 1/54 raised (~1.9%) and the other 53/54
    returned exactly 501 -- zero runs returned a wrong count either way.
    After the separate EV_RECEIPT registration fix landed (this module's own
    `_kevent_register`, closing a different silent-steal channel), a fresh
    n=40 on the same 500-iteration adversary on this box (2026-08-22) gave
    39/40 exact 501 and 1/40 raised -- still a raise at roughly the same
    2-3% rate, not zero. The channel is not closed, only made loud: a
    caller can still see a raise on this adversary at roughly this rate.
    """
    if k < 1:
        raise ValueError(f"batched_process_time_ms: k must be >= 1, got {k!r}")

    child_env = _env_with_benchmark_origin(env)

    if IS_WINDOWS:
        return _windows_batched_process_time_ms(cmd, k, child_env, cwd)
    if IS_DARWIN:
        return _darwin_batched_process_time_ms(cmd, k, child_env, cwd)
    if IS_LINUX:
        return _linux_batched_process_time_ms(cmd, k, child_env, cwd)

    raise NotImplementedError(
        f"batched_process_time_ms: no primitive implemented for platform "
        f"{sys.platform!r} (only win32/darwin/linux are)."
    )


def batched_process_time_quantiles(
    cmd: Sequence[str],
    k: int = 20,
    n: int = 15,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Runs `batched_process_time_ms(cmd, k, env, cwd)` `n` times and
    returns p50/p90 over those `n` batch samples.

    AC8: op_budget_suspension.py's REINSTATEMENT section requires quantiles
    over a real n and explicitly refuses single samples ("git --version
    ranged 15.3ms min to 279.3ms p99 at n=30 -- a ~20x spread on nothing").
    Callers were re-deriving quantiles on top of a mean; this primitive
    exists so they no longer have to. `n=15` x `k=20` is the spike's own
    methodology and the obvious default -- not re-derived here.

    The existing single-batch dict shape from `batched_process_time_ms` is
    UNCHANGED on every platform by this addition; this function only calls
    that primitive `n` times and summarises the resulting
    `process_time_ms` samples, so a caller of the single-batch primitive
    needs no platform branch either way.

    Returns:
        {
            "p50_ms": float,
            "p90_ms": float,
            "n": int,
            "k": int,
            "samples": list[float],  # each sample's process_time_ms, len n
        }

    `n=1` returns `p50_ms == p90_ms` == the single sample.

    Raises whatever `batched_process_time_ms` raises (NotImplementedError,
    OSError, ctypes.WinError, RuntimeError) -- no additional degradation.
    """
    if n < 1:
        raise ValueError(f"batched_process_time_quantiles: n must be >= 1, got {n!r}")

    samples = []
    for _ in range(n):
        result = batched_process_time_ms(cmd, k=k, env=env, cwd=cwd)
        samples.append(result["process_time_ms"])

    ordered = sorted(samples)

    def _percentile(pct: float) -> float:
        """Nearest-rank percentile with an EXPLICIT round-half-up tie-break.

        Review finding F1/F2: Python's built-in `round()` is ties-to-even
        (banker's rounding), not round-half-up -- at n=2, `round(0.5)` ties
        to 0 (even), silently returning the MINIMUM sample as the median
        instead of the conventional nearest-rank midpoint, and the tie-break
        direction flips with the parity of `len(ordered) - 1` (down at n=2,
        up at n=4) as an unintentional artifact of the built-in's default.
        `math.floor(x + 0.5)` below is round-half-up on ties (0.5 rounds
        UP, not to the nearest even) and matches plain rounding everywhere
        else -- deliberately NOT `round()`, so a future refactor cannot
        silently reintroduce ties-to-even here.
        """
        if len(ordered) == 1:
            return ordered[0]
        idx = math.floor(pct * (len(ordered) - 1) + 0.5)
        idx = max(0, min(len(ordered) - 1, idx))
        return ordered[idx]

    return {
        "p50_ms": round(_percentile(0.50), 3),
        "p90_ms": round(_percentile(0.90), 3),
        "n": n,
        "k": k,
        "samples": samples,
    }


def single_invocation_tree_process_time(
    cmd: Sequence[str],
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
    stdout_path: Optional[str] = None,
    stderr_path: Optional[str] = None,
) -> dict:
    """Measures ONE invocation of `cmd` and everything it spawns -- the
    root's own CPU plus every descendant's -- as a single figure.

    This is the sibling `batched_process_time_ms` cannot be, for the class
    of thing that cannot be run `k` times. A close ceremony, a merge, an
    install: each mutates state, so the second invocation measures a
    different job than the first, and the k-batched primitive's whole
    premise (identical repeats, amortised) does not hold. That primitive
    also discards stdio; for a once-only invocation the output IS the
    evidence of what was measured, so it is captured to disk here.

    NOT a second accounting mechanism. On Windows this shares
    `_windows_spawn_into_job` and `_windows_query_job_accounting` with the
    batched path -- the same job object, the same info class, the same
    CREATE_SUSPENDED-then-assign ordering that keeps pre-assignment CPU
    from escaping the job. On Darwin it calls `_darwin_one_invocation`
    directly, which is precisely the per-invocation unit the batched path
    loops over.

    Why this exists at all (the trap it closes): `os.times()` reports
    `children_user`/`children_system` as 0.0 on Windows always (module
    docstring, trap 1), so a probe that sums a parent's own
    `time.process_time()` and calls it the cost of a spawn-heavy operation
    reports a FLOOR and presents it as a total. An operation that shells
    out 47 times reads as if those 47 processes were free.

    Returns:
        {
            "process_time_ms": float,  # user+kernel CPU, root AND descendants
            "wall_ms": float,          # context only -- never gate on this
            "procs": int,              # distinct processes in the tree,
                                       #   INCLUDING the root (so a command
                                       #   that spawns nothing reports 1)
            "rc": int,                 # the root's exit code
            "k": 1,                    # shape parity with the batched dict
            "stdout_path": str | None,
            "stderr_path": str | None,
        }

    `k` is present and always 1 so a caller can consume either primitive's
    dict without branching on which one produced it.

    QUANTISATION (Windows): job-object accounting lands on ~15.6ms
    scheduler ticks (module docstring, trap 2), and k=1 cannot amortise
    that away -- it is the price of measuring something unrepeatable. The
    figure carries roughly +/-16ms of tick noise, which is immaterial
    against a 500ms bar and NOT immaterial against a 60ms one. A caller
    near the smaller bar wants `batched_process_time_ms` on a repeatable
    proxy instead, and must not reach for this function because it is more
    convenient.

    `procs` INCLUDES the root here, where the batched path's
    `procs_per_call` also counts it -- stated explicitly because the
    difference between "the ceremony spawned 47" and "the tree contains 48
    processes" is exactly the kind of off-by-one that turns a census into
    an argument.

    stdio: `stdout_path`/`stderr_path` are opened in binary append-free
    write mode and handed to the child. Left as None, the child INHERITS
    this process's stdio -- which for an interactive harness means the
    measured command's output lands in the operator's terminal, not in
    evidence. Pass them for anything whose output is being recorded.

    Raises whatever the underlying platform path raises -- `NotImplementedError`
    off Windows/Darwin, `OSError`/`ctypes.WinError`/`RuntimeError` on a
    measurement-mechanism failure. Never silently degrades to a wrong unit.
    """
    child_env = _env_with_benchmark_origin(env)

    if IS_WINDOWS:
        out_f = open(stdout_path, "wb") if stdout_path else None
        err_f = open(stderr_path, "wb") if stderr_path else None
        try:
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                t0 = time.perf_counter()
                rc = _windows_spawn_into_job(
                    job,
                    cmd,
                    child_env,
                    cwd,
                    stdout=out_f if out_f is not None else None,
                    stderr=err_f if err_f is not None else None,
                )
                wall_ms = (time.perf_counter() - t0) * 1000.0
                info = _windows_query_job_accounting(job)
                process_time_ms = (info.TotalUserTime + info.TotalKernelTime) / 10000.0
                procs = int(info.TotalProcesses)
            finally:
                _k32.CloseHandle(wintypes.HANDLE(job))
        finally:
            if out_f is not None:
                out_f.close()
            if err_f is not None:
                err_f.close()

        return {
            "process_time_ms": round(process_time_ms, 3),
            "wall_ms": round(wall_ms, 3),
            "procs": procs,
            "rc": rc,
            "k": 1,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
        }

    if IS_DARWIN:
        if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            raise RuntimeError(
                "process_time: SIGCHLD is SIG_IGN in this process -- both XNU "
                "and Linux destroy CPU accounting for auto-reaped children "
                "under that disposition (module docstring); refusing to "
                "silently under-report rather than measuring through it"
            )
        # `_posix_spawnp_suspended` passes NULL file_actions, so the child
        # inherits this process's descriptors. Redirecting OUR OWN fds 1/2
        # around the spawn is therefore the redirection mechanism -- not a
        # workaround for a missing feature, but the same thing a shell does.
        saved = []
        out_f = open(stdout_path, "wb") if stdout_path else None
        err_f = open(stderr_path, "wb") if stderr_path else None
        try:
            if out_f is not None:
                saved.append((1, os.dup(1)))
                os.dup2(out_f.fileno(), 1)
            if err_f is not None:
                saved.append((2, os.dup(2)))
                os.dup2(err_f.fileno(), 2)
            t0 = time.perf_counter()
            process_time_ms, procs, attach_failed, rc = _darwin_one_invocation(cmd, child_env, cwd)
            wall_ms = (time.perf_counter() - t0) * 1000.0
        finally:
            for fd, backup in saved:
                os.dup2(backup, fd)
                os.close(backup)
            if out_f is not None:
                out_f.close()
            if err_f is not None:
                err_f.close()

        if attach_failed:
            # Same refusal as the batched path: an unresolved residual means
            # some subtree exited unobserved, so `procs` is a lower bound.
            raise RuntimeError(
                f"process_time: attach_failed={attach_failed} after bounded "
                f"retry -- procs would be {procs} (LOWER BOUND, not exact). "
                "Refusing to return an undercount."
            )

        return {
            "process_time_ms": round(process_time_ms, 3),
            "wall_ms": round(wall_ms, 3),
            "procs": int(procs),
            "rc": rc,
            "k": 1,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
        }

    raise NotImplementedError(
        "single_invocation_tree_process_time: no process-tree accounting "
        "primitive for this platform. Implemented on win32/darwin only; "
        "unlike batched_process_time_ms (which now also has a Linux "
        "primitive via sys.addaudithook, module docstring), this function "
        "has no Linux implementation in this chunk."
    )

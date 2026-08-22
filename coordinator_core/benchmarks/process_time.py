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

Windows and Darwin only: `batched_process_time_ms` raises `NotImplementedError`
on every other platform. The getrusage process-time half is POSIX and
verified there against Linux's own `kernel/exit.c :: wait_task_zombie()`
rollup; only the spawn-count half (this module's kqueue/EVFILT_PROC
mechanism, Darwin-specific) is unverified on Linux. A time-measured,
count-refused route remains available there and is PM-gated, not
implemented in this chunk. This Windows/Darwin split is THIS MODULE's own
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
  - Linux: unimplemented (`NotImplementedError` above) -- nothing is
    measured here at all, which is itself the honest answer for this
    platform rather than a silent zero.
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Optional, Sequence

IS_WINDOWS = sys.platform == "win32"
IS_DARWIN = sys.platform == "darwin"

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

    if IS_WINDOWS:
        return _windows_batched_process_time_ms(cmd, k, env, cwd)
    if IS_DARWIN:
        return _darwin_batched_process_time_ms(cmd, k, env, cwd)

    raise NotImplementedError(
        "batched_process_time_ms: no spawn-count primitive for this platform. "
        "The getrusage process-time half is POSIX and verified here against "
        "Linux's own kernel/exit.c :: wait_task_zombie() rollup -- only the "
        "SPAWN-COUNT half (procs_per_call, this module's kqueue/EVFILT_PROC "
        "mechanism) is Darwin-specific and unverified on Linux. A "
        "time-measured, count-refused route remains available there and is "
        "PM-gated, not implemented in this chunk."
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

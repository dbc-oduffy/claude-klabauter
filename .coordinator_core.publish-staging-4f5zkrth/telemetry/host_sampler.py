"""
coordinator_core.telemetry.host_sampler — independent host-resource sampler.

Purpose: on 2026-08-15 this box lost its entire actively-emitting session
cohort for a 2h28m total telemetry blackout -- zero ops, zero commits, zero
respawns fleet-wide (state/audits/2026-08-15-fleet-degradation-forensics.md).
``op-latency.jsonl`` shows ops stopping; nothing on the box records CPU,
memory, or the process table independently of the coordinator engine, so the
window could be bounded but never explained. This module is that independent
witness.

Load-bearing requirement (outranks every other design choice here): THIS
MODULE MUST WRITE INDEPENDENTLY OF THE COORDINATOR ENGINE. The engine cannot
record why the engine stopped. Consequences of that one property:

    - ``sample_once()`` never imports ``coordinator_core.ipc``, never goes
      through ``dispatch_message``, and is never called from an op handler.
      It is invoked as a standalone process -- deployed as
      ``python <abs-path-to-this-file>`` (direct-script invocation, see
      "Invocation-cost ratchet" below; ``python -m
      coordinator_core.telemetry.host_sampler`` also still works, e.g. under
      pytest) -- launched by an OS-level scheduler (Windows Task Scheduler /
      cron) that is itself outside the engine's process tree. When every
      ``claude``/``python`` coordinator process on the box dies at once
      (exactly what happened on 2026-08-15), the OS scheduler still fires
      the next sampler invocation on schedule -- a sampler spawned BY the
      engine (e.g. from a cadence hook) would die with it and prove nothing.
    - This module reuses ``coordinator_core.telemetry.log_rotation`` (an
      already-independent, side-effect-only primitive with no engine
      dependency of its own) for its sink's rotation, and
      ``coordinator_core.git.git_dir.resolve_git_common_dir`` (zero-spawn,
      pure-Python -- see that function's own docstring) purely to resolve
      WHERE to write, not to route the write through any engine code path.
      Both are loaded by file path (``_load_sibling``), never through
      ``coordinator_core``'s package ``__init__``.
    - Wiring the OS-level scheduler entry itself is an install-surface
      concern (Task Scheduler XML / cron line), out of this module's scope;
      this module is the thing that gets invoked, built so that invocation
      never depends on the engine being alive.

Sink: ``<git_common_dir>/coordinator-sessions/logs/host-samples.jsonl`` --
same directory as the other three known telemetry sinks
(``coordinator_core.telemetry.log_rotation``'s module docstring), rotated by
that module's shared cascade primitive rather than a fifth bespoke retention
scheme. Added to that module's ``_KNOWN_SINK_NAMES`` so a cadence sweep of
the logs dir rotates this sink too.

Cadence arithmetic (state your numbers, don't just assert them):
    ``_DEFAULT_INTERVAL_SECS = 1200`` (20 minutes), 72 invocations/day (PM
    ruling, 2026-08-16). This is the BASELINE recorder, not the whole
    instrument -- its job is to be running when nobody knew to look, so
    that an incident like 2026-08-15's 2h28m blackout can be bounded and
    characterised after the fact even though no one was watching live. At
    20 minutes, that 2h28m (8920s) window contains ~7 interior samples --
    sufficient for that job: it bounds onset, shows the broad shape
    (rising/flat/falling), and shows recovery, which is what "nobody was
    watching" needs answered. It is not sized to resolve a fast transient
    inside a single 20-minute gap -- that is a DIFFERENT job, with its own
    tool: invoke this module's entry point directly
    (``python coordinator_core/telemetry/host_sampler.py``, or
    ``sample_once()`` in-process, e.g. from a shell loop) during an active
    investigation for on-demand, high-resolution sampling, at zero
    standing cost -- nothing runs when nobody is investigating. Reach for
    that instead of raising the baseline cadence; a permanently faster
    schedule paid every day to serve an occasional need is exactly the
    accretion this fix exists to cut. 72 fires/day vs the original 120s
    figure's 720 fires/day is a 10x cut in invocation count on its own,
    compounding with the import-cost fix below (see "Invocation-cost
    ratchet") for a combined ~order-of-magnitude reduction in daily spawn
    cost on a box whose defining incident was spawn amplification.

    Each invocation's OWN in-process collection cost is measured below and
    kept under ``_MAX_SAMPLE_COST_MS``; the full process-spawn-to-exit cost
    is kept under ``_MAX_INVOCATION_COST_MS`` (see "Invocation-cost ratchet"
    below) -- both are test-enforced ratchets (mirror
    ``coordinator_core.tests.test_ipc_per_request_state
    .test_op_timeout_overrides_never_ratchet_upward`` -- a bound derived from
    what the box can afford, measured against, never fitted upward to what
    the code currently costs).

Invocation-cost ratchet (2026-08-16 addition -- the sampler's own cost was
found to be dominated by process-spawn-to-exit time, not by the in-process
collection ``_MAX_SAMPLE_COST_MS`` alone could see):
    Measured interleaved before/after on this box (subprocess.run wall
    clock, n=20+ each, genuine 50-70-session concurrent load -- noisy;
    reported as the low cluster, with an intermittent 2-3x-higher cluster
    from box load spikes unrelated to this code):
        bare interpreter startup:                        ~43-52ms floor
        BEFORE this fix (package-import path,
          ``-m coordinator_core.telemetry.host_sampler``): ~117-120ms floor
        AFTER this fix (direct-script path):               ~52-62ms floor
    Roughly a 50% cut in the common case. Isolated (disable-env, import
    only, no sampling work) the package-``__init__`` cost alone measures
    smaller in isolation (~10-17ms over bare) than the full before/after gap
    -- the difference is import-machinery cost compounding with concurrent
    disk/module-lookup contention under real load, not a measurement error;
    the live interleaved numbers above are the ones this ratchet is set
    against, not the isolated ones. Contributors avoided: the package
    ``__init__`` chain itself, and ``coordinator_core.lifecycle`` pulling in
    ``subprocess``/``hashlib``/``functools`` unconditionally (paid even
    though the hot path never spawns) via its ``repo_root_seam`` walk-up
    machinery. ``ctypes`` (~5ms) and the module's OWN deliberate 50ms
    CPU-delta sleep plus the Toolhelp32 process-table walk remain -- real
    work, not import, out of this fix's scope. This module now avoids the
    package ``__init__`` entirely (loads its 3 sibling dependencies --
    ``atomic_append``, ``log_rotation``, ``git.git_dir`` -- by file path via
    ``importlib.util``, reusing an already-imported module from
    ``sys.modules`` when one exists rather than double-loading) and resolves
    the git common dir directly at ``repo_root`` (no
    ``coordinator_core.lifecycle``/``coordinator_core.git.repo_root`` walk-up
    or subprocess-fallback machinery) -- safe because the Task Scheduler
    registration always starts the process's cwd at the exact repo root
    (see ``coordinator_core/install/host_sampler_scheduler.py``
    ``_task_xml``'s native ``WorkingDirectory`` element), so no upward walk
    is ever needed in the deployed path. ``_MAX_INVOCATION_COST_MS`` bounds
    the FULL
    ``python <path> host_sampler.py`` wall-clock, verified by a
    subprocess-spawning test (marked ``spawns_process``) -- the number a
    scheduler actually pays, which ``_MAX_SAMPLE_COST_MS`` alone cannot see.

Row shape (kept intentionally small -- this runs forever):
    {"ts": float epoch, "cpu_pct": float|null, "mem_used_mb": int,
     "mem_avail_mb": int, "mem_total_mb": int, "proc_count": int,
     "claude_proc_count": int, "python_proc_count": int,
     "sample_cost_ms": float}

Negative-spec (hard-won):
    - Never breaks a caller: ``sample_once()`` swallows every exception to a
      debug log and returns without writing, mirroring
      ``coordinator_core.telemetry.op_latency``'s and ``log_rotation``'s own
      "never raises" contract verbatim. A telemetry defect must never fail
      anything that happens to call it.
    - No third-party dependency (no ``psutil``). Windows host stats are read
      via ``ctypes`` calls into ``kernel32`` (``GetSystemTimes``,
      ``GlobalMemoryStatusEx``, ``CreateToolhelp32Snapshot`` +
      ``Process32First``/``Next``) -- no subprocess spawn anywhere in this
      module. POSIX falls back to ``os.getloadavg()`` and ``/proc``.
    - CPU utilisation on Windows is computed from TWO successive
      ``GetSystemTimes`` snapshots a short, bounded interval apart (see
      ``_WINDOWS_CPU_SAMPLE_GAP_SECS``) -- a single snapshot only gives
      cumulative counters since boot, not a point-in-time percentage. That
      gap is itself sampling cost and is folded into the measured
      ``sample_cost_ms``.
    - Handle count is NOT sampled. A per-process ``GetProcessHandleCount``
      walk over every PID in the Toolhelp32 snapshot is the same order of
      cost as the process-count enumeration this module already pays for,
      doubled, for a field the brief marks optional ("if cheaply available")
      -- not cheap enough to clear this module's own cost ratchet, so it is
      left ``null`` rather than paid for. Revisit only alongside raising
      ``_MAX_SAMPLE_COST_MS``, per the ratchet's own rule: a bound is raised
      by a rebuild record, never quietly.
    - Kill switch: ``COORDINATOR_HOST_SAMPLER_DISABLE=1`` hard-disables
      sampling, checked first and cheaply, mirroring
      ``coordinator_core.telemetry.op_latency``'s ``_DISABLE_ENV`` precedent.
    - Sink override (2026-08-16 addition -- see "Measuring the sampler must
      not pollute what it measures" below): ``sample_once(sink_override=...)``
      or ``COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE=<path>`` redirects the
      write to an explicit path, skipping git-common-dir resolution
      entirely (so it also works with no real ``.git`` present at all, e.g.
      a bare ``tmp_path`` in a benchmark harness). Absent both, behaviour is
      byte-for-byte unchanged: resolve the production sink from
      ``repo_root``/cwd exactly as before.

Measuring the sampler must not pollute what it measures (2026-08-16): every
invocation of ``sample_once()`` -- including one whose ONLY purpose is
measuring this module's own invocation cost -- appended to the production
sink by default, because the sink path was derived unconditionally from
``repo_root``. A benchmark run in a real checkout (this repo, for instance)
therefore wrote real-looking rows into the same series the OS-scheduled task
writes into, indistinguishable from genuine 20-minute-cadence samples except
by inter-row gap. This is the same shape as the review-trail collector
counting 1,913 chain-ancestry waiver files as "malformed records" -- an
instrument corrupting the very series it exists to produce is worse than no
series, because trend is exactly what a series like this is consulted for.
The fix is the sink-override mechanism above; ``sample_once()``'s own cost
test (``coordinator_core.telemetry.tests
.test_host_sampler_invocation_cost``) now sets
``COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE`` on every spawned subprocess so
its repeated invocations never land in any git-resolved sink at all, real or
fake.

    Judgment call, stated explicitly rather than left implicit: an
    OPERATOR's ad-hoc direct invocation during a live incident (the
    documented high-resolution diagnostic path, see "Cadence arithmetic"
    above) is left writing to the production sink by DEFAULT -- i.e. the
    override is opt-in, never on-by-default. A real sample taken while
    investigating a real incident is legitimate data for the series it
    lands in, arguably more valuable than a scheduled sample (it is
    timed exactly when something was wrong); silently redirecting every
    unscheduled invocation away from the production sink would blind that
    diagnostic path to its own output, the opposite of this fix's goal.
    What's opt-in is redirecting AWAY from the production sink -- an
    operator who wants an isolated diagnostic run sets the override
    explicitly; a benchmark/test harness always does.

Spec backlink: state/audits/2026-08-15-fleet-degradation-forensics.md
               state/handoffs/2026-08-15-kill-it-if-it-cannot-pay-for-itself.md
               docs/wiki/machine-load-norm.md
               docs/wiki/cost-budgets-and-the-kill-disposition.md
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Sibling dependencies are loaded by FILE PATH (never `from coordinator_core
# import ...` / `from coordinator_core.telemetry import ...`) -- see module
# docstring "Invocation-cost ratchet". A dotted import of any name under the
# `coordinator_core` package forces Python to execute
# `coordinator_core/__init__.py` (and every intermediate `__init__.py`)
# first, even for `python <path>/host_sampler.py` direct-script invocation.
# `_load_sibling` reuses an already-imported module from `sys.modules` when
# one exists (e.g. under pytest, where the package is already loaded) rather
# than double-loading -- so this costs nothing extra in that context and
# skips the package entirely when invoked standalone.
_THIS_DIR = Path(__file__).resolve().parent


def _load_sibling(modname: str, relpath: str):
    existing = sys.modules.get(modname)
    if existing is not None:
        return existing
    path = _THIS_DIR / relpath
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DISABLE_ENV = "COORDINATOR_HOST_SAMPLER_DISABLE"

# See module docstring "Sink override" / "Measuring the sampler must not
# pollute what it measures". Set by benchmarks/tests to redirect the write
# away from the production sink entirely; never set by the deployed Task
# Scheduler invocation.
_SINK_OVERRIDE_ENV = "COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE"

# See module docstring "Cadence arithmetic" -- 20 minutes (PM ruling,
# 2026-08-16, superseding the original 120s/2min derivation): enough
# interior detail to bound onset/shape/recovery across a ~2.5h incident
# window (~7 samples) while cutting daily invocation count 10x, compounding
# with the invocation-cost fix below.
_DEFAULT_INTERVAL_SECS = 1200

# Ratchet high-water mark for one sample's own wall-clock cost. Measured on
# this box: ~15-40ms dominated by the GetSystemTimes double-snapshot gap
# (see _WINDOWS_CPU_SAMPLE_GAP_SECS) plus one Toolhelp32 process walk.
# Raising this requires a rebuild record, never a quiet edit -- see
# coordinator_core.tests.test_ipc_per_request_state
# .test_op_timeout_overrides_never_ratchet_upward for the pattern this
# mirrors.
_MAX_SAMPLE_COST_MS = 250.0

# Ratchet high-water mark for the FULL process-spawn-to-exit invocation cost
# (import + collection + write) -- the number a scheduler actually pays, not
# just the in-process slice `_MAX_SAMPLE_COST_MS` bounds. Derived at the
# 20-minute/72-fires-per-day cadence (see module docstring's "Cadence
# arithmetic" and "Invocation-cost ratchet"). Measured interleaved
# before/after on this box (n=20+ each, subprocess.run wall clock under
# genuine 50-70-session concurrent load, so noisy -- reported as the tight
# low cluster, with an intermittent high cluster at 2-3x that from box
# load spikes unrelated to this code):
#     pre-fix (package-import path):  ~117-120ms floor
#     post-fix (direct-script path):  ~52-62ms floor
# roughly a 50% cut in the common case. Even a full 500ms/fire (4-8x the
# measured floor, generous headroom for a busier box, cold disk cache, or
# a load-spike run) at 72 fires/day is 36s/day of wall clock and 72
# spawns/day -- both negligible against the load norm, so 500ms is set as
# a genuine ratchet (catches a regression back toward the pre-fix
# ~117-300ms range or worse), not a number tightened to look meaningful.
# Raising this requires a rebuild record, never a quiet edit -- same rule
# as `_MAX_SAMPLE_COST_MS`.
_MAX_INVOCATION_COST_MS = 500.0

# Gap between the two GetSystemTimes snapshots used to compute a
# point-in-time CPU percentage on Windows. Short enough to keep sample cost
# low, long enough that the kernel/idle/user tick deltas aren't dominated by
# measurement noise.
_WINDOWS_CPU_SAMPLE_GAP_SECS = 0.05

# os.name, not platform.system() -- see coordinator_core.atomic_append's own
# identical rationale (platform.system() costs ~28ms on Windows resolving
# the full uname/win32_ver triple; os.name is a preset constant). Not
# reused from atomic_append.IS_WINDOWS directly because that module is now
# loaded lazily (see _load_sibling) only where actually needed (the write
# path), not unconditionally at import time.
_IS_WINDOWS = os.name == "nt"

_logger = None


def _log():
    global _logger
    if _logger is None:
        import logging
        _logger = logging.getLogger(__name__)
    return _logger


def _sink_path(git_common_dir_path: Path) -> Path:
    """Resolve the host-sampler sink path under the given git common dir."""
    return (
        Path(git_common_dir_path)
        / "coordinator-sessions"
        / "logs"
        / "host-samples.jsonl"
    )


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


def _filetime_to_100ns(ft: "_FILETIME") -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _windows_cpu_pct() -> Optional[float]:
    """Point-in-time CPU utilisation via two GetSystemTimes snapshots.

    Returns None (never raises) if the ctypes call is unavailable or fails --
    a missing CPU field must not stop the rest of the row from being written.
    """
    try:
        kernel32 = ctypes.windll.kernel32

        def _snapshot():
            idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
            ok = kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
            if not ok:
                return None
            return (
                _filetime_to_100ns(idle),
                _filetime_to_100ns(kernel),
                _filetime_to_100ns(user),
            )

        first = _snapshot()
        if first is None:
            return None
        time.sleep(_WINDOWS_CPU_SAMPLE_GAP_SECS)
        second = _snapshot()
        if second is None:
            return None

        idle_delta = second[0] - first[0]
        # kernel time INCLUDES idle time on Windows; total busy ticks are
        # (kernel - idle) + user.
        kernel_delta = second[1] - first[1]
        user_delta = second[2] - first[2]
        total_delta = kernel_delta + user_delta
        if total_delta <= 0:
            return 0.0
        busy_delta = total_delta - idle_delta
        return max(0.0, min(100.0, (busy_delta / total_delta) * 100.0))
    except Exception:
        return None


def _windows_memory_mb() -> tuple:
    """(used_mb, avail_mb, total_mb) via GlobalMemoryStatusEx, or (None, None, None)."""
    try:
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
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        if not ok:
            return (None, None, None)
        total_mb = stat.ullTotalPhys // (1024 * 1024)
        avail_mb = stat.ullAvailPhys // (1024 * 1024)
        used_mb = total_mb - avail_mb
        return (int(used_mb), int(avail_mb), int(total_mb))
    except Exception:
        return (None, None, None)


# Toolhelp32 process-snapshot constants.
_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _windows_process_counts() -> tuple:
    """(total_proc_count, claude_proc_count, python_proc_count) via Toolhelp32.

    Returns (None, None, None) on any failure -- never raises.
    """
    try:
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot == -1 or snapshot == 0:
            return (None, None, None)
        try:
            entry = _PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
            total = 0
            claude = 0
            python = 0
            found = kernel32.Process32First(snapshot, ctypes.byref(entry))
            while found:
                total += 1
                try:
                    name = entry.szExeFile.decode("utf-8", errors="ignore").lower()
                except Exception:
                    name = ""
                if "claude" in name:
                    claude += 1
                if "python" in name:
                    python += 1
                found = kernel32.Process32Next(snapshot, ctypes.byref(entry))
            return (total, claude, python)
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return (None, None, None)


def _posix_cpu_pct() -> Optional[float]:
    """1-minute load average as a percentage of CPU count -- coarse but free."""
    try:
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        return max(0.0, min(100.0, (load1 / cpu_count) * 100.0))
    except Exception:
        return None


def _posix_memory_mb() -> tuple:
    try:
        info = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                value_kb = rest.strip().split()[0]
                info[key] = int(value_kb)
        total_mb = info.get("MemTotal", 0) // 1024
        avail_mb = info.get("MemAvailable", 0) // 1024
        used_mb = total_mb - avail_mb
        return (int(used_mb), int(avail_mb), int(total_mb))
    except Exception:
        return (None, None, None)


def _posix_process_counts() -> tuple:
    try:
        total = 0
        claude = 0
        python = 0
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            total += 1
            try:
                with open(f"/proc/{name}/comm", "r") as fh:
                    comm = fh.read().strip().lower()
            except Exception:
                comm = ""
            if "claude" in comm:
                claude += 1
            if "python" in comm:
                python += 1
        return (total, claude, python)
    except Exception:
        return (None, None, None)


def _collect_row() -> dict:
    """Gather one sample's fields. Never raises -- callers already wrap this."""
    t_start = time.perf_counter()

    if _IS_WINDOWS:
        cpu_pct = _windows_cpu_pct()
        used_mb, avail_mb, total_mb = _windows_memory_mb()
        proc_count, claude_count, python_count = _windows_process_counts()
    else:
        cpu_pct = _posix_cpu_pct()
        used_mb, avail_mb, total_mb = _posix_memory_mb()
        proc_count, claude_count, python_count = _posix_process_counts()

    sample_cost_ms = (time.perf_counter() - t_start) * 1000.0

    return {
        "ts": time.time(),
        "cpu_pct": cpu_pct,
        "mem_used_mb": used_mb,
        "mem_avail_mb": avail_mb,
        "mem_total_mb": total_mb,
        "proc_count": proc_count,
        "claude_proc_count": claude_count,
        "python_proc_count": python_count,
        "sample_cost_ms": round(sample_cost_ms, 3),
    }


def sample_once(
    repo_root: Optional[Path] = None, sink_override: Optional[Path] = None
) -> Optional[dict]:
    """Collect one host-resource row and append it to the sink.

    Standalone entry point -- does NOT go through coordinator_core.ipc or any
    op-dispatch path (see module docstring's load-bearing requirement).
    Returns the row written, or None if disabled/failed (never raises -- a
    telemetry defect must never fail a caller).

    ``sink_override`` (or the ``COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE`` env
    var, checked when the argument is omitted) writes directly to that path
    instead of resolving the production sink from ``repo_root`` -- see
    module docstring "Sink override". When set, no git resolution happens at
    all (works with no ``.git`` present).
    """
    try:
        if os.environ.get(_DISABLE_ENV) == "1":
            return None

        if sink_override is None:
            env_override = os.environ.get(_SINK_OVERRIDE_ENV)
            if env_override:
                sink_override = Path(env_override)

        row = _collect_row()

        if sink_override is not None:
            sink = Path(sink_override)
        else:
            if repo_root is None:
                repo_root = Path.cwd()
            repo_root = Path(repo_root)

            # Direct check at repo_root ONLY -- no upward walk, unlike
            # coordinator_core.lifecycle.git_common_dir's repo_root_seam
            # (which this module deliberately no longer imports, see module
            # docstring "Invocation-cost ratchet"). Safe because the Task
            # Scheduler registration always starts in the exact repo root
            # before invoking this module
            # (host_sampler_scheduler._task_xml's native WorkingDirectory),
            # so cwd ==
            # repo_root == the directory containing `.git` in every
            # deployed invocation. A repo_root with no `.git` entry directly
            # on it fails closed to None here, same observable outcome as
            # the old walk-based resolver raising on total resolution
            # failure.
            dot_git = repo_root / ".git"
            if not dot_git.exists():
                return None

            git_dir = _load_sibling("coordinator_core.git.git_dir", "../git/git_dir.py")
            common_dir = git_dir.resolve_git_common_dir(repo_root)
            sink = _sink_path(common_dir)

        try:
            os.makedirs(sink.parent, exist_ok=True)
        except OSError:
            return None

        log_rotation = _load_sibling(
            "coordinator_core.telemetry.log_rotation", "log_rotation.py"
        )
        try:
            log_rotation.rotate_if_needed(sink)
        except Exception:
            pass

        atomic_append = _load_sibling("coordinator_core.atomic_append", "../atomic_append.py")
        line = json.dumps(row, separators=(",", ":")) + "\n"
        atomic_append.append_line(sink, line.encode("utf-8"))
        return row
    except Exception:
        try:
            _log().debug(
                "coordinator_core.telemetry.host_sampler: sample_once failed",
                exc_info=True,
            )
        except Exception:
            pass
        return None


def main() -> None:
    """CLI entry point.

    Deployed invocation (Task Scheduler, see
    coordinator_core/install/host_sampler_scheduler.py) is
    ``python <abs-path-to-this-file>`` -- a direct-script invocation, NOT
    ``python -m coordinator_core.telemetry.host_sampler``. The two differ in
    an import-cost-relevant way: ``-m`` always executes
    ``coordinator_core/__init__.py`` (and every intermediate package
    ``__init__.py``) first, because Python must import the dotted package
    path to find the submodule; direct-script invocation does not, and this
    module's own imports are now either stdlib or file-path-loaded (see
    ``_load_sibling``), so direct invocation never touches the package
    ``__init__`` chain at all. ``python -m ...`` still works (e.g. under
    pytest, or run manually) -- it is just no longer the deployed path.

    Takes exactly one sample and exits -- cadence is owned by the OS-level
    scheduler that invokes this (Task Scheduler / cron), never by an internal
    sleep loop, so that the sampler's liveness never depends on any
    long-lived process (including itself) surviving. See module docstring's
    load-bearing requirement.
    """
    sample_once()


if __name__ == "__main__":
    main()

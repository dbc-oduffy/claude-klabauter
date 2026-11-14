"""Falsifier for the fleet recursive-spawn guard: how far a synthetic
self-respawning tree grows inside a capped Job Object, whether one job close
reaps it, whether a depth-2 pytest session is refused (exit 70), and what guard setup
costs in process time and spawns.

SAFETY: every spawn here runs inside a Job Object whose ActiveProcessLimit this
harness sets and READS BACK before launching anything. A limit above
MAX_SAFE_LIMIT, or one the kernel did not apply, aborts. Never run the
synthetic spawner outside `_capped_job`.

Windows-only measurement; elsewhere `run()` reports `supported: false`.
Process time comes from `process_time.py`'s job-accounting reader and
`in_process_time_ms`, never wall clock.

    python coordinator_core/benchmarks/spawn_guard_overhead.py --json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import process_time as pt
from coordinator_core.temp_layout import managed_temp_dir

IS_WINDOWS = sys.platform == "win32"

DEFAULT_LIMIT = 20
MAX_SAFE_LIMIT = 50
_LIMIT_ACTIVE_PROCESS = 0x8
_STILL_ACTIVE = 259
_JOB_MEMORY_BYTES = 4 * 1024**3
_DEFAULT_DEPTH_KEY = "COORDINATOR_PYTEST_DEPTH"

# Each node tries to spawn BRANCH copies of itself down to MAX_DEPTH, then
# sleeps. A refused CreateProcess (job at its limit) is swallowed so the
# tree sits at the ceiling instead of erroring out.
_SPAWNER_SRC = """\
import subprocess, sys, time
depth = int(sys.argv[1])
if depth < {max_depth}:
    for _ in range({branch}):
        try:
            subprocess.Popen([sys.executable, sys.argv[0], str(depth + 1)])
        except OSError:
            pass
time.sleep(60)
"""
_SPAWNER_BRANCH = 2
_SPAWNER_MAX_DEPTH = 5
_OBSERVE_S = 1.5
_POLL_S = 0.02
_NESTED_TIMEOUT_S = 90.0

if IS_WINDOWS:
    from ctypes import wintypes

    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k.CreateJobObjectW.restype = wintypes.HANDLE
    _k.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k.SetInformationJobObject.restype = wintypes.BOOL
    _k.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    _k.QueryInformationJobObject.restype = wintypes.BOOL
    _k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k.AssignProcessToJobObject.restype = wintypes.BOOL
    _k.CloseHandle.argtypes = [wintypes.HANDLE]
    _k.CloseHandle.restype = wintypes.BOOL
    _k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k.OpenProcess.restype = wintypes.HANDLE
    _k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k.GetExitCodeProcess.restype = wintypes.BOOL


def _create_job(limit: int):
    """Capped kill-on-close job (`contained_run.create_capped_job`), limit verified by read-back."""
    if not 2 <= limit <= MAX_SAFE_LIMIT:
        raise ValueError(f"active process limit must be 2..{MAX_SAFE_LIMIT}, got {limit}")
    from coordinator_core.diagnostics import contained_run

    create = getattr(contained_run, "create_capped_job", None)
    if create is not None:
        job = create(limit, _JOB_MEMORY_BYTES)
    else:
        job = _harness_capped_job(contained_run, limit)
    if _read_active_limit(job) != _kernel_limit(limit):
        _k.CloseHandle(job)
        raise RuntimeError("ActiveProcessLimit was not applied; refusing to launch")
    return job


def _harness_capped_job(contained_run, limit: int):
    """Base fallback: the launcher's kill-on-close job plus an ActiveProcessLimit this harness sets."""
    job = contained_run._create_kill_on_close_job()
    info = pt._JobObjectExtendedLimitInformation()
    if not _k.QueryInformationJobObject(
        job, pt._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info), None,
    ):
        err = ctypes.get_last_error()
        _k.CloseHandle(job)
        raise ctypes.WinError(err)
    info.BasicLimitInformation.LimitFlags |= _LIMIT_ACTIVE_PROCESS
    info.BasicLimitInformation.ActiveProcessLimit = _kernel_limit(limit)
    if not _k.SetInformationJobObject(
        job, pt._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        err = ctypes.get_last_error()
        _k.CloseHandle(job)
        raise ctypes.WinError(err)
    return job


def _kernel_limit(limit: int) -> int:
    """The stored ActiveProcessLimit for a tree of `limit`: the kernel admits one process past it."""
    return limit - 1


def _read_active_limit(job) -> int:
    info = pt._JobObjectExtendedLimitInformation()
    if not _k.QueryInformationJobObject(
        job, pt._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info), None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not info.BasicLimitInformation.LimitFlags & _LIMIT_ACTIVE_PROCESS:
        return 0
    return int(info.BasicLimitInformation.ActiveProcessLimit)


def _job_pids(job) -> list:
    info = pt._JobObjectBasicProcessIdList()
    if not _k.QueryInformationJobObject(
        job, pt._JOB_OBJECT_BASIC_PROCESS_ID_LIST,
        ctypes.byref(info), ctypes.sizeof(info), None,
    ) and ctypes.get_last_error() != pt._ERROR_MORE_DATA:
        raise ctypes.WinError(ctypes.get_last_error())
    return [int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)]


def _pid_alive(pid: int) -> bool:
    h = _k.OpenProcess(pt._PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        return bool(_k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        _k.CloseHandle(h)


def _launch_in_job(job, argv, env, cwd) -> subprocess.Popen:
    """Suspended spawn, assign, resume: the child cannot fork before it is capped."""
    proc = subprocess.Popen(
        list(argv), env=env, cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=pt._CREATE_SUSPENDED | pt._CREATE_NO_WINDOW,
    )
    if not _k.AssignProcessToJobObject(job, int(proc._handle)):
        err = ctypes.get_last_error()
        proc.kill()
        raise ctypes.WinError(err)
    pt._resume_all_threads(proc.pid)
    return proc


def _measure_tree(limit: int, workdir: Path) -> dict:
    script = workdir / "recursive_spawner.py"
    script.write_text(
        _SPAWNER_SRC.format(max_depth=_SPAWNER_MAX_DEPTH, branch=_SPAWNER_BRANCH),
        encoding="utf-8")
    job = _create_job(limit)
    closed = False
    try:
        _launch_in_job(job, [sys.executable, str(script), "0"], os.environ.copy(), str(workdir))
        seen: set = set()
        max_tree = 0
        deadline = time.monotonic() + _OBSERVE_S
        while time.monotonic() < deadline:
            acct = pt._windows_query_job_accounting(job)
            max_tree = max(max_tree, int(acct.ActiveProcesses))
            seen.update(_job_pids(job))
            time.sleep(_POLL_S)
        acct = pt._windows_query_job_accounting(job)
        process_time_ms = (acct.TotalUserTime + acct.TotalKernelTime) / 1e4
        total_processes = int(acct.TotalProcesses)
        seen.update(_job_pids(job))
        _k.CloseHandle(job)
        closed = True
        time.sleep(0.5)
        survivors = sum(1 for p in seen if _pid_alive(p))
    finally:
        if not closed:
            _k.CloseHandle(job)
    return {
        "limit": limit,
        "max_tree": max_tree,
        "total_processes": total_processes,
        "survivors": survivors,
        "tree_process_time_ms": round(process_time_ms, 1),
    }


def _guard_api():
    """Returns (guarded_child_env, ENV_KEY) when the nested-session guard exists, else (None, default key)."""
    try:
        from coordinator_core.testing import nested_session_guard as g
    except ImportError:
        return None, _DEFAULT_DEPTH_KEY
    fn = getattr(g, "guarded_child_env", None)
    key = _DEFAULT_DEPTH_KEY
    try:
        from coordinator_core.testing import spawn_depth

        key = getattr(spawn_depth, "ENV_KEY", key)
    except ImportError:
        pass
    return fn, key


def _measure_nested(limit: int, workdir: Path) -> dict:
    """A grandchild pytest session (depth 2: top-level 0, child 1, grandchild 2), capped."""
    guarded, key = _guard_api()
    env = os.environ.copy()
    if guarded is not None:
        env = guarded(guarded(guarded(env)))
    else:
        env[key] = "1"
    test_file = workdir / "test_nested_probe.py"
    test_file.write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
            "--rootdir", str(workdir), str(test_file)]
    job = _create_job(limit)
    try:
        proc = _launch_in_job(job, argv, env, str(workdir))
        try:
            rc = proc.wait(timeout=_NESTED_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            rc = None
    finally:
        _k.CloseHandle(job)
    return {"nested_exit": rc, "guard_present": guarded is not None}


class _SpawnCounter:
    """Counts Popen constructions while active, so 'adds 0 spawns' is observed."""

    def __init__(self):
        self.count = 0

    def __enter__(self):
        self._orig = subprocess.Popen.__init__
        counter = self

        def counting(popen_self, *a, **kw):
            counter.count += 1
            return counting_orig(popen_self, *a, **kw)

        counting_orig = self._orig
        subprocess.Popen.__init__ = counting
        return self

    def __exit__(self, *exc):
        subprocess.Popen.__init__ = self._orig


def _measure_setup(limit: int) -> dict:
    """Process time and spawns of what a guarded launch adds before the child runs."""
    guarded, _ = _guard_api()

    def setup():
        if guarded is not None:
            guarded(dict(os.environ))
        _k.CloseHandle(_create_job(limit))

    with _SpawnCounter() as counter:
        reading = pt.in_process_time_ms(setup)
    return {
        "setup_ms": round(float(reading["process_time_ms"]), 3),
        "added_spawns": counter.count,
    }


def run(limit: int = DEFAULT_LIMIT) -> dict:
    if not IS_WINDOWS:
        return {"supported": False, "platform": sys.platform}
    with managed_temp_dir(prefix="spawn_guard_overhead_") as tmp:
        work = Path(tmp)
        tree = _measure_tree(limit, work)
        nested = _measure_nested(limit, work)
    setup = _measure_setup(limit)
    from coordinator_core.diagnostics import contained_run

    launcher = "create_capped_job" if hasattr(contained_run, "create_capped_job") else "harness_fallback"
    return {"supported": True, "launcher": launcher, **tree, **nested, **setup}


def main(argv: Optional[list] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args(argv)
    result = run(args.limit)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for k, v in result.items():
            print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

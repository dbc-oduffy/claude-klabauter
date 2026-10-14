"""Process-tree-containing subprocess runner, self-falsifying by construction.

Spec backlink: `state/audits/2026-08-07-windows-shakedown-cruise-measurements.md`
sect M1 (Process-tree containment: the bound does not bind). Measured directly on
this box: a parent -> child -> grandchild tree under a 3s
`subprocess.run(timeout=)` bound left **2 of 3 processes alive** — Windows
`timeout=` kills only the direct child, descendants are orphaned and keep
running, and the caller's `TimeoutExpired` handler reports the operation
stopped while it did not. `taskkill /PID <pid> /T /F` and a Win32 Job Object
with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` both measured 0 survivors. This
module exists because `coordinator_core` had zero occurrences of
`taskkill`/`CreateJobObject`/`AssignProcessToJobObject`/any `JOB_OBJECT_*`
constant at the time of that measurement — nothing in the engine contained a
tree. `run_contained` is a new, standalone isolated-test-area primitive; it
does not replace or modify `coordinator_core/testing/run.py`.

negative-spec:
    Do NOT replace the Job Object (Windows) / process-group kill (POSIX) path
    below with a bare `subprocess.run(timeout=)`. That form was measured on
    this box to orphan 2 of 3 processes in a parent -> child -> grandchild
    tree — the direct child dies, descendants are silently left running, and
    the caller believes the operation stopped. The entire purpose of this
    module is that it does not do that. If you find yourself "simplifying"
    `run_contained` back to a bare `subprocess.run(timeout=...)`, stop: that
    is the exact regression this file was written to prevent.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid

from coordinator_core.temp_layout import managed_tempdir

IS_WINDOWS = sys.platform == "win32"

TIMEOUT_RC = 124

GENERATES: list = []

if IS_WINDOWS:
    import ctypes.wintypes as wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    CREATE_NO_WINDOW = 0x08000000
    CREATE_SUSPENDED = 0x00000004
    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL

    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _kernel32.Thread32First.restype = wintypes.BOOL

    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _kernel32.Thread32Next.restype = wintypes.BOOL

    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE

    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD

    def _create_kill_on_close_job():
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(job)
            raise ctypes.WinError(ctypes.get_last_error())
        return job

    def _assign_to_job(job, process_handle):
        ok = _kernel32.AssignProcessToJobObject(job, process_handle)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _resume_suspended_process(pid: int) -> None:
        """Resume every thread of a CREATE_SUSPENDED process by pid.

        Closes the TOCTOU window between
        `Popen` and job assignment. The child is created suspended, assigned
        to the job while it cannot yet run any code (so it cannot spawn a
        grandchild that escapes membership), then resumed here.

        `subprocess.Popen` closes its own thread handle immediately after
        `CreateProcess` returns (a CPython internal detail on the Windows
        code path), so the suspended thread cannot be resumed via the Popen
        object itself. Re-open it via a Toolhelp32 snapshot instead. There
        is exactly one thread at this point — the child has not executed any
        code yet, so it cannot have created more.
        """
        snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        invalid = ctypes.c_void_p(-1).value
        if not snap or snap == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _THREADENTRY32()
            entry.dwSize = ctypes.sizeof(_THREADENTRY32)
            found = False
            ok = _kernel32.Thread32First(snap, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == pid:
                    found = True
                    hthread = _kernel32.OpenThread(
                        THREAD_SUSPEND_RESUME, False, entry.th32ThreadID
                    )
                    if hthread:
                        try:
                            _kernel32.ResumeThread(hthread)
                        finally:
                            _kernel32.CloseHandle(hthread)
                ok = _kernel32.Thread32Next(snap, ctypes.byref(entry))
            if not found:
                raise RuntimeError(f"no threads found for suspended pid {pid}")
        finally:
            _kernel32.CloseHandle(snap)


def derive_worker_cap() -> int:
    try:
        import psutil

        physical_cores = psutil.cpu_count(logical=False) or os.cpu_count() or 2
        usable_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        cap = min(physical_cores / 2, usable_ram_gb * 1024 / 150)
    except ImportError:
        logical_cores = os.cpu_count() or 2
        cap = logical_cores // 2

    return max(1, int(cap))


def run_contained(cmd, timeout, cwd=None, stdin_data=None):
    if IS_WINDOWS:
        return _run_contained_windows(cmd, timeout, cwd=cwd, stdin_data=stdin_data)
    return _run_contained_posix(cmd, timeout, cwd=cwd, stdin_data=stdin_data)


def _run_contained_windows(cmd, timeout, cwd=None, stdin_data=None):
    job = _create_kill_on_close_job()
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW | CREATE_SUSPENDED,
        )
        try:
            _assign_to_job(job, int(proc._handle))
        finally:
            _resume_suspended_process(proc.pid)
        try:
            stdout, stderr = proc.communicate(input=stdin_data, timeout=timeout)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            _kernel32.CloseHandle(job)
            job = None
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = b"", b""
                for pipe in (proc.stdout, proc.stderr, proc.stdin):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except Exception:
                            pass
            return TIMEOUT_RC, stdout, stderr
    finally:
        if job is not None:
            _kernel32.CloseHandle(job)
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _run_contained_posix(cmd, timeout, cwd=None, stdin_data=None):
    import signal

    from coordinator_core.win_portability import no_console_creationflags

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_data is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        **no_console_creationflags(),
    )
    try:
        stdout, stderr = proc.communicate(input=stdin_data, timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = b"", b""
        return TIMEOUT_RC, stdout, stderr
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


_SPAWN_SCRIPT_BODY = textwrap.dedent(
    """
    import subprocess
    import sys
    import time

    def main():
        tag = sys.argv[1]
        depth = int(sys.argv[2])
        hard_expiry = float(sys.argv[3])
        start = time.time()
        if depth > 0:
            subprocess.Popen(
                [sys.executable, sys.argv[0], tag, str(depth - 1), str(hard_expiry)]
            )
        while time.time() - start < hard_expiry:
            time.sleep(0.2)

    if __name__ == "__main__":
        main()
    """
)


def _write_spawn_script() -> str:
    fd, path = tempfile.mkstemp(prefix="diag-spawn-", suffix=".py", dir=managed_tempdir())
    with os.fdopen(fd, "w", newline="\n") as f:
        f.write(_SPAWN_SCRIPT_BODY)
    return path


def _count_tagged_survivors(tag: str) -> int:
    try:
        import psutil

        count = 0
        for p in psutil.process_iter(["cmdline"]):
            try:
                cmdline = p.info.get("cmdline") or []
            except Exception:
                continue
            if any(tag in part for part in cmdline):
                count += 1
        return count
    except ImportError:
        pass

    try:
        from coordinator_core.win_portability import (
            leaf_spawn_creationflags,
            no_console_creationflags,
        )

        if IS_WINDOWS:
            out = subprocess.run(
                ["wmic", "process", "get", "commandline"],
                capture_output=True,
                text=True,
                timeout=15,
                **no_console_creationflags(),
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-eo", "args"],
                capture_output=True,
                text=True,
                timeout=15,
                **leaf_spawn_creationflags(),
            ).stdout
        return sum(1 for line in out.splitlines() if tag in line)
    except Exception:
        return -1


def self_test(hard_expiry: float = 25.0) -> dict:
    results = {"hard_expiry": hard_expiry}
    script_path = _write_spawn_script()
    try:
        from coordinator_core.win_portability import no_console_creationflags

        naive_tag = f"diag-naive-{uuid.uuid4().hex}"
        naive_proc = subprocess.Popen(
            [sys.executable, script_path, naive_tag, "2", str(hard_expiry)],
            # DEVNULL, explicitly: this control's output is genuinely not
            # wanted (the verdict comes from the survivor count below, not
            # from anything the child prints). Saying so beats leaving the
            # streams unspecified, which on Windows discards them by accident
            # -- CREATE_NO_WINDOW with no stream kwarg binds the child to the
            # window-less console the flag allocates. Same observable
            # behaviour, but now it reads as a decision.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
        try:
            naive_proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            naive_proc.kill()
        time.sleep(1.0)
        naive_survivors = _count_tagged_survivors(naive_tag)
        results["naive_survivors"] = naive_survivors
        results["naive_leaked"] = naive_survivors > 0

        contained_tag = f"diag-contained-{uuid.uuid4().hex}"
        rc, _out, _err = run_contained(
            [sys.executable, script_path, contained_tag, "2", str(hard_expiry)],
            timeout=3,
        )
        time.sleep(1.0)
        contained_survivors = _count_tagged_survivors(contained_tag)
        results["contained_rc"] = rc
        results["contained_survivors"] = contained_survivors
        results["contained_leaked"] = contained_survivors > 0

        # negative-spec: an UNCOUNTABLE result is not a CLEAN one. Both counters
        # return -1 when neither psutil nor the platform text-enumeration fallback
        # can answer, and `-1 > 0` is False -- so a failed enumeration would render
        # as "contained" on the contained row. Today the naive control happens to
        # catch that (it too reads as not-leaking, which fails falsification), so the
        # verdict is correct only as a side effect of the control. Depending on that
        # is exactly the manufactured green this module exists to detect. Make the
        # uncountable case its own explicit, loud state instead.
        enumeration_failed = naive_survivors < 0 or contained_survivors < 0
        results["enumeration_failed"] = enumeration_failed
        results["falsified"] = (
            not enumeration_failed
            and bool(results["naive_leaked"])
            and not results["contained_leaked"]
        )

        time.sleep(hard_expiry + 2)
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass

    return results


def _verdict_cell(survivors: int, leaked: bool) -> str:
    if survivors < 0:
        return "UNCOUNTABLE"
    return "LEAKS" if leaked else "contains"


def _print_table(results: dict) -> None:
    print(f"{'strategy':<12} {'survivors':>10}  verdict")
    print(
        f"{'naive':<12} {results['naive_survivors']:>10}  "
        f"{_verdict_cell(results['naive_survivors'], results['naive_leaked'])}"
    )
    print(
        f"{'contained':<12} {results['contained_survivors']:>10}  "
        f"{_verdict_cell(results['contained_survivors'], results['contained_leaked'])}"
    )
    print()
    if results.get("enumeration_failed"):
        print(
            "SELF-TEST INCONCLUSIVE: process enumeration failed (neither psutil nor "
            "the platform fallback could answer), so a survivor count of -1 means "
            "NOT LOOKED, never NOT LEAKED. This is not a pass and not a failure of "
            "containment -- install psutil and re-run before trusting any verdict."
        )
        return
    if results["falsified"]:
        print("SELF-TEST PASSED: naive path leaked, contained path did not.")
    else:
        print(
            "SELF-TEST FAILED TO FALSIFY: could not demonstrate naive-leaks / "
            "contained-holds. Do not trust this harness until this is fixed."
        )


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--self-test" in argv:
        results = self_test()
        _print_table(results)
        return 0 if results["falsified"] else 1
    print("usage: python -m coordinator_core.diagnostics.contained_run --self-test")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

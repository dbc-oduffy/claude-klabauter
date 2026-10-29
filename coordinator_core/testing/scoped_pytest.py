"""Engine-owned launcher for a workflow test-runner's pytest: allow-listed, admitted, contained.

    python -m coordinator_core.testing.scoped_pytest --allow <target> [--allow <target> ...] -- <pytest args and targets>

Exit codes: pytest's own; 75 admission refused (nothing spawned); 64 target outside --allow or none
named (nothing spawned); 137 an ancestor of the launcher died and the tree was killed.

The pytest child runs in its own process group (POSIX) or a kill-on-close Job Object (Windows), so
launcher death kills it too. A watchdog watches the launcher's recorded ancestor chain, not just
its parent: a dead intermediate shell leaves the immediate parent alive.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from typing import Any, Optional, Sequence

import psutil

from coordinator_core.testing import watch_parent

ADMIT_KEY = "test-runner-pytest"
# Cold-start floor only: admit_before_spawn takes max(this, the peak recorded for ADMIT_KEY).
COLD_START_FLOOR_GIB = 1.0
EXIT_ADMISSION_REFUSED = 75
EXIT_TARGET_REFUSED = 64
EXIT_PARENT_DIED = 137
WATCH_INTERVAL_S = 2.0
_exit = os._exit  # module alias so tests can substitute it; the real call must skip finalizers

_IS_WINDOWS = sys.platform == "win32"
_SHELL_NAMES = frozenset(
    {"bash", "sh", "zsh", "dash", "fish", "cmd", "powershell", "pwsh", "env", "winpty", "timeout"}
)


def _is_shell(name: str) -> bool:
    base = (name or "").lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base in _SHELL_NAMES


def ancestor_chain(pid: Optional[int] = None) -> list:
    """(pid, create_time) of each ancestor of `pid`, nearest first, ending at the first non-shell
    one (the agent process). Create times come from watch_parent.process_create_time, the clock
    watch_parent compares against; psutil's create_time() is a different source and never matches."""
    chain: list = []
    try:
        proc = psutil.Process(pid or os.getpid())
        while True:
            parent = proc.parent()
            if parent is None:
                break
            created = watch_parent.process_create_time(parent.pid)
            chain.append((parent.pid, float("nan") if created is None else created))
            if not _is_shell(parent.name()):
                break
            proc = parent
    except psutil.Error:
        pass
    return chain


def _target_file(token: str) -> str:
    return token.split("::", 1)[0]


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.relpath(os.path.abspath(path)))).replace("\\", "/")


def path_targets(pytest_args: Sequence[str]) -> list:
    """Positional tokens that name a path: a `.py` file part (nodeid allowed) or an existing path."""
    found = []
    for tok in pytest_args:
        if not tok or tok.startswith("-"):
            continue
        part = _target_file(tok)
        if part.endswith(".py") or os.path.exists(part):
            found.append(part)
    return found


def check_allowed(allow: Sequence[str], pytest_args: Sequence[str]) -> Optional[str]:
    """None when every path target is in `allow` (equal, or under an allowed directory); else the
    refusal line naming the first offender."""
    allowed = [_norm(a) for a in allow]
    targets = path_targets(pytest_args)
    if not targets:
        return "no path target named; refusing a collection the allow-list cannot bound"
    for part in targets:
        n = _norm(part)
        if not any(n == a or n.startswith(a.rstrip("/") + "/") for a in allowed):
            return "target outside --allow: %s" % part
    return None


def _spawn(argv: list) -> tuple:
    """Start the child contained; returns (proc, kill) where kill() ends the whole tree."""
    if _IS_WINDOWS:
        from coordinator_core.diagnostics import contained_run as cr
        from coordinator_core.win_portability import no_console_passthrough_kwargs

        job = cr._create_kill_on_close_job()
        flags = cr.CREATE_NO_WINDOW | cr.CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP
        # Passthrough fds: without a std-stream kwarg a CREATE_NO_WINDOW child's pytest output is lost.
        proc = subprocess.Popen(argv, **{**no_console_passthrough_kwargs(), "creationflags": flags})
        try:
            cr._assign_to_job(job, int(proc._handle))
        except Exception:
            proc.kill()
            cr._kernel32.CloseHandle(job)
            raise
        cr._resume_suspended_process(proc.pid)
        state = {"job": job}

        def kill() -> None:
            if state["job"] is not None:
                cr._kernel32.CloseHandle(state["job"])
                state["job"] = None
            try:
                proc.kill()
            except OSError:
                pass

        return proc, kill

    def pdeathsig() -> None:
        if sys.platform.startswith("linux"):
            import ctypes

            ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG

    proc = subprocess.Popen(argv, start_new_session=True, preexec_fn=pdeathsig)

    def kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    return proc, kill


def main(argv: Optional[Sequence[str]] = None, host: Any = None) -> int:
    """Parse, allow-list, admit, spawn contained, then watch; see the module docstring for exit codes."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--" in args:
        i = args.index("--")
        head, pytest_args = args[:i], args[i + 1:]
    else:
        head, pytest_args = args, []
    parser = argparse.ArgumentParser(prog="scoped_pytest", add_help=False)
    parser.add_argument("--allow", action="append", default=[])
    opts, _ = parser.parse_known_args(head)

    refusal = check_allowed(opts.allow, pytest_args)
    if refusal:
        print("scoped_pytest: %s" % refusal, file=sys.stderr)
        return EXIT_TARGET_REFUSED

    from coordinator_core.bash_guards import _heavy_command_class as hcc

    # The launcher's own pid holds the reservation (it outlives the child and reaps with it): the
    # check and the START write share one lock, so concurrent launchers cannot both read zero.
    admission = hcc.admit_before_spawn(
        ADMIT_KEY, COLD_START_FLOOR_GIB, host=host, register_pid=os.getpid(), wait_kw={}
    )
    if not admission.admitted:
        print("scoped_pytest: %s" % admission.reason, file=sys.stderr)
        return EXIT_ADMISSION_REFUSED

    chain = ancestor_chain()
    proc, kill = _spawn([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *pytest_args])

    gone = threading.Event()

    def on_gone() -> None:
        gone.set()  # before kill(): the main thread wakes on the kill and must already see this
        kill()
        _exit(EXIT_PARENT_DIED)

    for pid, created in chain:
        watch_parent.watch_parent(pid, created, on_gone, WATCH_INTERVAL_S)
    try:
        code = proc.wait()
        return EXIT_PARENT_DIED if gone.is_set() else code
    finally:
        if proc.poll() is None:
            kill()


if __name__ == "__main__":
    sys.exit(main())

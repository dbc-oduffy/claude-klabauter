"""Engine-owned launcher for a workflow test-runner's pytest: allow-listed, admitted, contained.

    python -m coordinator_core.testing.scoped_pytest --allow <target> [--allow <target> ...] -- <pytest args and targets>

Exit codes: pytest's own; 75 admission refused (nothing spawned); 64 target outside --allow or none
named, nesting depth exceeded, or selection includes the calling test (nothing spawned); 137 an ancestor of the launcher died and the tree was killed.

The pytest child runs in its own process group (POSIX) or a process- and memory-capped kill-on-close
Job Object (Windows), with the nested-session guard env, so launcher death kills it too. A watchdog watches the launcher's recorded ancestor chain, not just
its parent: a dead intermediate shell leaves the immediate parent alive.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

import psutil

from coordinator_core import machine_resolver, temp_layout
from coordinator_core.testing import nested_session_guard, spawn_depth, watch_parent

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
            # Process.parent() can enumerate the entire PID table on Darwin.
            # A parent PID and its creation time suffice to reject PID reuse.
            parent_pid = proc.ppid()
            if parent_pid <= 0 or parent_pid == proc.pid:
                break
            parent = psutil.Process(parent_pid)
            if parent.create_time() > proc.create_time():
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
    from coordinator_core.diagnostics import contained_run as cr

    kwargs: dict = {"env": nested_session_guard.guarded_child_env(os.environ)}
    if _IS_WINDOWS:
        from coordinator_core.win_portability import no_console_passthrough_kwargs

        # Passthrough fds: without a std-stream kwarg a CREATE_NO_WINDOW child's pytest output is lost.
        kwargs.update(no_console_passthrough_kwargs())
        kwargs["creationflags"] = cr.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:

        def pdeathsig() -> None:
            if sys.platform.startswith("linux"):
                import ctypes

                ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG

        kwargs["preexec_fn"] = pdeathsig
    return cr.spawn_in_capped_job(argv, popen_kwargs=kwargs, background=True)


RUN_ROOT_KEY = "paths.pytest_run_root"


def per_run_root(repo_root: Any) -> Path:
    """Parent of every per-run pytest dir: the absolute machine-local registry value of
    `paths.pytest_run_root`, else `temp_layout.pytest_basetemp_root(repo_root)`."""
    try:
        raw = machine_resolver.registry_get(RUN_ROOT_KEY)
    except Exception:  # noqa: BLE001 -- an unreadable registry falls back to the Temp layout
        raw = None
    if raw:
        configured = Path(os.path.expanduser(raw))
        if configured.is_absolute():
            return configured
    try:
        return temp_layout.pytest_basetemp_root(repo_root)
    except ValueError:
        return temp_layout.pytest_fleet_basetemp_root()


def new_run_slug() -> str:
    return "%d-%s" % (os.getpid(), os.urandom(3).hex())


def has_basetemp(args: Sequence[str]) -> bool:
    return any(a == "--basetemp" or a.startswith("--basetemp=") for a in args)


def per_run_dirs(repo_root: Any, slug: str) -> tuple:
    """(holder, basetemp, tmp), created: `<root>/<slug>` holds `bt` (pytest's basetemp) and `tmp`
    (the child's TEMP/TMP), so no two slugs share a `pytest-current` link."""
    holder = per_run_root(repo_root) / slug
    bt, tmp = holder / "bt", holder / "tmp"
    bt.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return holder, bt, tmp


def tmp_env(base_env: Any, tmp: Path) -> dict:
    """Copy of `base_env` with TEMP, TMP and TMPDIR pointing at `tmp`."""
    env = dict(base_env)
    env.update(TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp))
    return env


def with_per_run_basetemp(pytest_args: Sequence[str]) -> list:
    """`pytest_args` unchanged when the caller passed `--basetemp`; else with a unique
    `--basetemp <per_run_root>/<pid>-<hex>` appended, so concurrent runs never
    share pytest's `pytest-of-<user>` dir and its `pytest-current` symlink."""
    args = list(pytest_args)
    if has_basetemp(args):
        return args
    root = per_run_root(os.getcwd())
    root.mkdir(parents=True, exist_ok=True)
    return [*args, "--basetemp", str(root / new_run_slug())]


def minted_basetemp(args: Sequence[str]) -> Optional[Path]:
    """The `--basetemp` value `with_per_run_basetemp` appended, else None (a caller's own is theirs)."""
    a = list(args)
    return Path(a[-1]) if len(a) >= 2 and a[-2] == "--basetemp" else None


def remove_finished_basetemp(basetemp: Optional[Path], code: int) -> None:
    """Delete a minted per-run basetemp after a completed run (exit 0 or 5, no kept marker).

    A failing run's dir is evidence and stays for the dead-owner sweep; a dir carrying
    `PYTEST_KEPT_MARKER` is the engine's deliberate keep and is never removed here. Never raises:
    a held handle leaves the dir to the sweep.
    """
    if basetemp is None or code not in (0, 5) or not basetemp.is_dir() or basetemp.is_symlink():
        return
    if (basetemp / temp_layout.PYTEST_KEPT_MARKER).exists():
        return
    try:
        temp_layout._rmtree_strict(basetemp)
    except OSError:
        pass


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

    refusal = (
        spawn_depth.refusal_for_spawn(os.environ)
        or spawn_depth.selection_includes_spawner(pytest_args, os.environ, os.getcwd())
        or check_allowed(opts.allow, pytest_args)
    )
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
    run_args = with_per_run_basetemp(pytest_args)
    minted = None if has_basetemp(pytest_args) else minted_basetemp(run_args)
    proc, kill = _spawn([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *run_args])

    gone = threading.Event()

    def on_gone() -> None:
        gone.set()  # before kill(): the main thread wakes on the kill and must already see this
        kill()
        _exit(EXIT_PARENT_DIED)

    for pid, created in chain:
        watch_parent.watch_parent(pid, created, on_gone, WATCH_INTERVAL_S)
    try:
        code = proc.wait()
        if not gone.is_set():
            remove_finished_basetemp(minted, code)
        return EXIT_PARENT_DIED if gone.is_set() else code
    finally:
        if proc.poll() is None:
            kill()


if __name__ == "__main__":
    sys.exit(main())

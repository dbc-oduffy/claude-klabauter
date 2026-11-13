"""Dump-storm falsifier: N concurrent callers against a stub `_machine_local.py` that outlives every bound.

MACHINE_LOCAL_IMPL points at a tmp stub that records its pid and sleeps 10s only under
`if __name__ == "__main__"`, so an in-process load returns at once and only a real spawn sleeps.
Pass 1 runs the callers through cross-repo-memo's dump path (`_machine_local_dump`). Pass 2 runs
them through `machine_local_spawn.dump(..., in_process=False)` so single-flight is measured rather
than bypassed; it is skipped, and recorded as skipped, where the helper module is absent. A caller
still alive at the deadline is killed, which reproduces the storm's caller death and leaves its
child orphaned unless the caller reaped it.

Per pass it prints `spawned=<n> alive_after_exit=<n>`, counted from a stub-side pid file; the last
line summarizes the worst of the executed passes. Measurement only: exit 0 either way.

Run: `python coordinator_core/benchmarks/machine_local_dump_storm.py --callers 20`
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEADLINE_S = 3.0
STUB_SLEEP_S = 10

_STUB = f'''\
import os, time

if __name__ == "__main__":
    with open(os.environ["STORM_PIDFILE"], "a") as f:
        f.write(str(os.getpid()) + "\\n")
    time.sleep({STUB_SLEEP_S})
    print("{{}}")
'''

MODES = ("cross-repo-memo", "force-spawn")


def _no_console() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        sys.path.insert(0, str(REPO_ROOT))
        from coordinator_core.bash_guards import _host_probe

        return _host_probe.creation_time(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill(pid: int) -> None:
    try:
        if sys.platform == "win32":
            sys.path.insert(0, str(REPO_ROOT))
            from coordinator_core.bash_guards import _process_kill

            ctime = _process_kill._hp.creation_time(pid)
            if ctime is not None:
                _process_kill.terminate_verified(pid, ctime)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def _caller(mode: str) -> int:
    """Child entry: one caller doing one dump through the named path."""
    sys.path.insert(0, str(REPO_ROOT))
    impl = os.environ["MACHINE_LOCAL_IMPL"]
    if mode == "cross-repo-memo":
        bin_dir = REPO_ROOT / "coordinator" / "bin"
        sys.path.insert(0, str(bin_dir))
        spec = importlib.util.spec_from_file_location("_crm_storm", bin_dir / "cross-repo-memo.py")
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        mod._machine_local_dump()
    else:
        from coordinator_core import machine_local_spawn

        machine_local_spawn.dump(impl, in_process=False)
    return 0


def _helper_present() -> bool:
    return (REPO_ROOT / "coordinator_core" / "machine_local_spawn.py").is_file()


def _run_pass(mode: str, callers: int, deadline_s: float, work: Path) -> tuple[int, int, int]:
    """(spawned, alive_after_exit, callers_killed) for one pass."""
    pass_dir = work / mode
    pass_dir.mkdir()
    stub = pass_dir / "_machine_local.py"
    stub.write_text(_STUB, encoding="utf-8")
    pidfile = pass_dir / "pids.txt"
    reg = pass_dir / "registry"
    reg.mkdir()
    env = dict(os.environ)
    env.update(
        MACHINE_LOCAL_IMPL=str(stub),
        MACHINE_LOCAL_REGISTRY_DIR=str(reg),
        COORDINATOR_SETTINGS_HOME=str(pass_dir / "settings"),
        COORDINATOR_LOCK_ROOT=str(pass_dir / "locks"),
        STORM_PIDFILE=str(pidfile),
    )
    started = time.monotonic()
    procs = [
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--caller", mode],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_no_console(),
        )
        for _ in range(callers)
    ]
    killed = 0
    while True:
        running = [p for p in procs if p.poll() is None]
        if not running:
            break
        if time.monotonic() - started >= deadline_s:
            for p in running:
                p.kill()
                p.wait()
                killed += 1
            break
        time.sleep(0.02)
    pids = [int(x) for x in pidfile.read_text().split()] if pidfile.exists() else []
    alive = [pid for pid in pids if _alive(pid)]
    for pid in alive:
        _kill(pid)
    return len(pids), len(alive), killed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--callers", type=int, default=20)
    ap.add_argument("--deadline", type=float, default=DEADLINE_S, help="seconds before a caller is killed")
    ap.add_argument("--no-force-spawn", action="store_true", help="skip pass 2")
    ap.add_argument("--caller", choices=MODES, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.caller:
        return _caller(args.caller)

    results: list[tuple[int, int]] = []
    with tempfile.TemporaryDirectory(prefix="ml-dump-storm-") as tmp:
        work = Path(tmp)
        for index, mode in enumerate(MODES, start=1):
            if mode == "force-spawn" and (args.no_force_spawn or not _helper_present()):
                print(f"pass{index} {mode}: skipped (machine_local_spawn absent or --no-force-spawn)")
                continue
            spawned, alive, killed = _run_pass(mode, args.callers, args.deadline, work)
            results.append((spawned, alive))
            print(
                f"pass{index} {mode}: callers={args.callers} spawned={spawned} "
                f"alive_after_exit={alive} callers_killed_at_deadline={killed}"
            )
    spawned_max = max((r[0] for r in results), default=0)
    alive_max = max((r[1] for r in results), default=0)
    print(f"summary: spawned={spawned_max} alive_after_exit={alive_max}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

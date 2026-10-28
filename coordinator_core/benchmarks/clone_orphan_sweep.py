"""Reap python processes orphaned inside `<repo_root>/scratch/benchmark-clones/`.

A benchmark clone's warm supervisor/server can outlive its test: the parent died or the clone
tree was removed. This sweep terminates only such orphans, outside pytest (the suite's
foreign-kill tripwire does not apply here).

Trap: a process whose parent is alive and whose clone tree exists belongs to a running peer
benchmark and is never signalled, even when it shares a clone with an orphan. pair_stop is used
only for a clone whose tree exists and in which EVERY rooted process is an orphan, since it stops
the whole engine pair.

Run: `python -m coordinator_core.benchmarks.clone_orphan_sweep --repo-root <root>`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from coordinator_core.benchmarks.isolated_clone import _normalized, _process_is_rooted_under

CLONES_SUBDIR = ("scratch", "benchmark-clones")
_TERMINATE_TIMEOUT_SECS = 5.0
_KILL_TIMEOUT_SECS = 3.0
_ROLE_ORDER = ("supervisor.py", "front_door.py", "server.py")
_WARM_SCRIPTS = _ROLE_ORDER


def _parent_is_dead(proc) -> bool:
    import psutil

    try:
        return proc.parent() is None
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError):
        return False


def _clone_top(proc, base_norm: str) -> Optional[str]:
    """Normalized `<base>/<name>` directory the process is rooted under, from cwd then cmdline."""
    import psutil

    candidates: List[str] = []
    try:
        cwd = proc.cwd()
        if cwd:
            candidates.append(_normalized(Path(cwd)))
    except (psutil.Error, OSError):
        pass
    try:
        for arg in proc.cmdline() or ():
            candidates.append(_normalized(Path(arg)) if os.path.isabs(arg) else "")
    except (psutil.Error, OSError):
        pass
    prefix = base_norm + os.sep
    for cand in candidates:
        if cand.startswith(prefix):
            name = cand[len(prefix):].split(os.sep, 1)[0]
            if name:
                return prefix + name
    return None


def _role_rank(proc) -> int:
    import psutil

    try:
        argv = [a.replace("\\", "/").rsplit("/", 1)[-1] for a in (proc.cmdline() or ())]
    except (psutil.Error, OSError):
        return len(_ROLE_ORDER)
    for rank, script in enumerate(_ROLE_ORDER):
        if script in argv:
            return rank
    return len(_ROLE_ORDER)


def _engine_root_of(proc) -> Optional[Path]:
    import psutil

    try:
        argv = proc.cmdline() or ()
    except (psutil.Error, OSError):
        return None
    for arg in argv:
        p = Path(arg)
        if p.name in _WARM_SCRIPTS and p.parent.name == "warm" and p.parent.parent.name == "coordinator_core":
            return p.parent.parent.parent
    return None


def _terminate_all(procs: list) -> List[int]:
    import psutil

    for proc in procs:
        try:
            proc.terminate()
        except (psutil.Error, OSError):
            pass
    _, alive = psutil.wait_procs(procs, timeout=_TERMINATE_TIMEOUT_SECS)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.Error, OSError):
            pass
    psutil.wait_procs(alive, timeout=_KILL_TIMEOUT_SECS)
    return [p.pid for p in procs]


def sweep(
    repo_root: str,
    *,
    parent_dead: Callable[[object], bool] = _parent_is_dead,
    stop_pair: Optional[Callable[[Path], object]] = None,
) -> List[int]:
    """Terminate orphaned processes rooted under `<repo_root>/scratch/benchmark-clones/`.

    Orphan: its clone's top directory is gone, or `parent_dead(proc)`. Returns the pids signalled.
    Never raises.
    """
    import psutil

    base = Path(repo_root).joinpath(*CLONES_SUBDIR)
    base_norm = _normalized(base)
    me = os.getpid()
    by_clone: Dict[str, List[tuple]] = {}
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.pid == me:
                    continue
                if not (proc.info.get("name") or "").lower().startswith("python"):
                    continue
                if not _process_is_rooted_under(proc, base_norm):
                    continue
                top = _clone_top(proc, base_norm)
                if top is None:
                    continue
                orphan = (not os.path.isdir(top)) or parent_dead(proc)
                by_clone.setdefault(top, []).append((proc, orphan))
            except (psutil.Error, OSError):
                continue
    except Exception:  # noqa: BLE001 -- housekeeping never fails loud
        return []

    signalled: List[int] = []
    for top, members in by_clone.items():
        orphans = [p for p, is_orphan in members if is_orphan]
        if not orphans:
            continue
        if os.path.isdir(top) and len(orphans) == len(members):
            engine_root = next((r for r in map(_engine_root_of, orphans) if r), None)
            if engine_root is not None:
                try:
                    if stop_pair is None:
                        from coordinator_core.warm.pair_stop import stop_warm_pair as stop_pair_fn
                    else:
                        stop_pair_fn = stop_pair
                    stop_pair_fn(engine_root)
                except Exception:  # noqa: BLE001 -- fall through to direct terminate
                    pass
        orphans.sort(key=_role_rank)
        live = [p for p in orphans if _still_running(p)]
        signalled.extend(p.pid for p in orphans if p not in live)
        if live:
            signalled.extend(_terminate_all(live))
    return signalled


def _still_running(proc) -> bool:
    import psutil

    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="clone_orphan_sweep")
    ap.add_argument("--repo-root", required=True)
    args = ap.parse_args(argv)
    for pid in sweep(args.repo_root):
        print(pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())

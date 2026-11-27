"""Kills codex app-server and pid-update-loop processes whose argv path or CODEX_HOME resolves under a root."""
from __future__ import annotations

import os
import time
from pathlib import Path

from _unimatrix.proc import pid_alive

MARKERS = ("app-server", "pid-update-loop")
_CONFIRM_SECONDS = 2.0


def _canon(text: str) -> Path | None:
    try:
        return Path(os.path.normcase(os.path.realpath(text)))
    except (OSError, ValueError):
        return None


def _under(text: str, root: Path) -> bool:
    if not text or "\0" in text:
        return False
    cand = _canon(text)
    return cand is not None and (cand == root or root in cand.parents)


def _is_codex_target(argv: list[str]) -> bool:
    """invariant: a marker token alone is not enough; the argv must also name codex."""
    if not argv:
        return False
    stems = {os.path.basename(a.replace("\\", "/")).lower() for a in argv}
    if not any(m in stems for m in MARKERS) and not any(m in argv for m in MARKERS):
        return False
    return any("codex" in a.lower() for a in argv)


def _argv_paths(argv: list[str]) -> list[str]:
    out = []
    for a in argv:
        out.append(a)
        if "=" in a:
            out.append(a.split("=", 1)[1])
    return out


def _owned_by_root(proc, argv: list[str], root: Path) -> bool:
    import psutil
    if any(_under(p, root) for p in _argv_paths(argv)):
        return True
    try:
        home = proc.environ().get("CODEX_HOME")
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return False
    return bool(home) and _under(home, root)


def _owned_procs(real: Path, since: float | None = None) -> list:
    import psutil
    me = os.getpid()
    found = []
    for proc in psutil.process_iter():
        if proc.pid == me:
            continue
        try:
            if since is not None and proc.create_time() < since:
                continue
            argv = proc.cmdline()
            if _is_codex_target(argv) and _owned_by_root(proc, argv, real):
                found.append(proc)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue
    return found


def alive_under(root: Path) -> bool:
    """True while any codex app-server / pid-update-loop process still resolves under `root`."""
    real = _canon(str(root))
    return real is not None and bool(_owned_procs(real))


def reap_codex_home(root: Path, since: float | None = None) -> list[int]:
    """Kill every codex app-server / pid-update-loop process owned by `root`; return the pids that are gone.

    `since` (epoch seconds) spares processes created earlier, so a live user daemon under a real CODEX_HOME survives.
    One in-process psutil enumeration, no child spawn. Processes it cannot read are skipped."""
    import psutil
    real = _canon(str(root))
    if real is None:
        return []
    victims = _owned_procs(real, since)
    for proc in victims:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    psutil.wait_procs(victims, timeout=_CONFIRM_SECONDS)
    deadline = time.monotonic() + _CONFIRM_SECONDS
    gone = []
    for proc in victims:
        while pid_alive(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if not pid_alive(proc.pid):
            gone.append(proc.pid)
    return gone

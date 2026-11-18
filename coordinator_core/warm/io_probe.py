"""Single-flight, in-thread file create+open probe armed by the first stall dump of a quiet period.

One daemon thread per window (`WINDOW_S`), one create+open+unlink per second per target (the
stalling repo's `scratch/` and one distinct second drive), logging only ops over `SLOW_MS` to
`<svc_dir>/io-probe.log`, beside `stall-dumps/`, so dumps and probe lines align by time. No spawn.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

LOG_NAME = "io-probe.log"
WINDOW_S = 20 * 60.0
SLOW_MS = 50.0
TICK_S = 1.0
_PREFIX = ".io-probe-"

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def format_line(wall: float, label: str, ms: float, path) -> str:
    """`<UTC ts>.mmmZ io-probe <label> <ms>.1ms <path>\\n` -- the fixed log line."""
    sec = int(wall)
    milli = int(round((wall - sec) * 1000))
    if milli >= 1000:
        sec, milli = sec + 1, 0
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec))
    return f"{stamp}.{milli:03d}Z io-probe {label} {ms:.1f}ms {path}\n"


def probe_once(directory: Path) -> float:
    """Create, open and unlink one file in `directory`; returns elapsed ms (also on OSError)."""
    t0 = time.perf_counter()
    name = Path(directory) / f"{_PREFIX}{os.getpid()}-{threading.get_ident()}"
    try:
        os.makedirs(directory, exist_ok=True)
        fd = os.open(name, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
        os.close(fd)
    except OSError:
        pass
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    return (time.perf_counter() - t0) * 1000.0


def run_loop(
    targets: Sequence[Tuple[str, Path]],
    log_path,
    deadline: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
    wait: Optional[Callable[[float], object]] = None,
    probe: Callable[[Path], float] = probe_once,
) -> None:
    """Probe every target once per `TICK_S` until `monotonic()` reaches `deadline`; append lines
    for ops over `SLOW_MS`. Never raises."""
    if wait is None:
        wait = threading.Event().wait
    try:
        while monotonic() < deadline:
            lines = []
            for label, directory in targets:
                ms = probe(directory)
                if ms > SLOW_MS:
                    lines.append(format_line(wall(), label, ms, directory))
            if lines:
                with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.writelines(lines)
            wait(TICK_S)
    except Exception:  # noqa: BLE001 -- diagnostics never stop the engine
        return


def _same_device(a, b) -> bool:
    """True when `a` and `b` share a volume; True too when either is unreadable (no second target)."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True


def _settings_home() -> Path:
    home = os.environ.get("COORDINATOR_SETTINGS_HOME", "").strip()
    return Path(home) if home else Path.home() / ".coordinator-claude-settings"


def targets_for(repo_root, svc_dir) -> List[Tuple[str, Path]]:
    """The repo's gitignored `scratch/`, plus the settings home or the temp dir when that sits
    on a different drive than the repo; one target when no second drive exists."""
    repo = Path(repo_root)
    out: List[Tuple[str, Path]] = [("repo", repo / "scratch")]
    for label, cand in (("settings-home", _settings_home()), ("temp", Path(tempfile.gettempdir()))):
        if not _same_device(repo, cand):
            out.append((label, cand))
            break
    return out


def arm(svc_dir, repo_root) -> bool:
    """Start the probe thread unless one is alive; True when this call started it."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        svc = Path(svc_dir)
        targets = targets_for(repo_root, svc)
        log_path = svc / LOG_NAME
        deadline = time.monotonic() + WINDOW_S

        def _run() -> None:
            run_loop(targets, log_path, deadline)

        t = threading.Thread(target=_run, name="io-probe", daemon=True)
        t.start()
        _thread = t
        return True

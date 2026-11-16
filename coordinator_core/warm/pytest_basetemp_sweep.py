"""Throttled sweeper for stale pytest basetemp dirs, run by the warm engine supervisor.

Reclaims ``<tempdir>/pytest-of-<user>/pytest-*`` dirs older than ``MAX_AGE_SECS`` whose
``.lock`` pid is dead. Only the top level of that one dir is listed; deletion proceeds in
batches of top-level children with a pause between batches; one child is removed in a single
``rmtree`` with no internal pause, so the throttle spaces children, not files.
Every failure is logged to stderr and swallowed: the sweeper never raises into its caller.
"""
from __future__ import annotations

import getpass
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

MAX_AGE_SECS = 6 * 3600
INTERVAL_SECS = 3600
BATCH_SIZE = 8
BATCH_PAUSE_SECS = 0.25

_BASETEMP_RE = re.compile(r"^pytest-\d+$")


def _log(msg: str) -> None:
    print(f"[pytest-basetemp-sweep] {msg}", file=sys.stderr)


def _default_pid_alive(pid: int) -> bool:
    from coordinator_core.session.core import pid_alive

    return pid_alive(pid)


def _basetemp_root(temp_root: Optional[str]) -> Path:
    base = Path(temp_root) if temp_root else Path(tempfile.gettempdir())
    return base / f"pytest-of-{getpass.getuser()}"


def _lock_pid(basetemp: Path) -> Optional[int]:
    """Pid recorded in ``<basetemp>/.lock``; None when the file is absent or unparseable."""
    try:
        return int((basetemp / ".lock").read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return None


def _clear_readonly_and_retry(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: _clear_readonly_and_retry(f, p, e))


def _remove_in_batches(
    basetemp: Path,
    *,
    batch_size: int,
    pause_secs: float,
    sleep: Callable[[float], None],
) -> None:
    """Delete ``basetemp`` one top-level child at a time, pausing after each batch."""
    with os.scandir(basetemp) as it:
        children = [Path(e.path) for e in it]
    for i, child in enumerate(children, 1):
        if child.is_dir() and not child.is_symlink():
            _rmtree(child)
        else:
            child.unlink()
        if i % batch_size == 0:
            sleep(pause_secs)
    basetemp.rmdir()


def sweep_once(
    *,
    temp_root: Optional[str] = None,
    now: Optional[float] = None,
    max_age_secs: float = MAX_AGE_SECS,
    pid_alive: Optional[Callable[[int], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    batch_size: int = BATCH_SIZE,
    pause_secs: float = BATCH_PAUSE_SECS,
) -> dict:
    """One sweep; returns ``{"removed": [...], "kept": int, "failed": [...]}``. Never raises."""
    report: dict = {"removed": [], "kept": 0, "failed": []}
    try:
        root = _basetemp_root(temp_root)
        clock = time.time() if now is None else now
        alive = pid_alive or _default_pid_alive
        try:
            with os.scandir(root) as it:
                entries = [e for e in it if _BASETEMP_RE.match(e.name)]
        except OSError:
            return report
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if clock - entry.stat(follow_symlinks=False).st_mtime < max_age_secs:
                    report["kept"] += 1
                    continue
                path = Path(entry.path)
                pid = _lock_pid(path)
                if pid is not None and alive(pid):
                    report["kept"] += 1
                    continue
                _remove_in_batches(path, batch_size=batch_size, pause_secs=pause_secs, sleep=sleep)
                report["removed"].append(entry.name)
                sleep(pause_secs)
            except Exception as exc:  # noqa: BLE001 -- one bad dir must not stop the sweep
                report["failed"].append(entry.name)
                _log(f"failed to remove {entry.name}: {exc!r}")
    except Exception as exc:  # noqa: BLE001 -- never raise into the supervisor
        _log(f"sweep aborted: {exc!r}")
    return report


def sweep_loop(
    *,
    stop: Optional[threading.Event] = None,
    interval_secs: float = INTERVAL_SECS,
    **sweep_kwargs,
) -> None:
    """Sweep once now, then every ``interval_secs`` until ``stop`` is set."""
    stop = stop or threading.Event()
    while not stop.is_set():
        report = sweep_once(**sweep_kwargs)
        if report["removed"] or report["failed"]:
            _log(f"removed {len(report['removed'])}, kept {report['kept']}, failed {len(report['failed'])}")
        stop.wait(interval_secs)


def start_sweeper() -> threading.Thread:
    """Start the sweep loop on a daemon thread and return it."""
    t = threading.Thread(target=sweep_loop, daemon=True, name="warm-pytest-basetemp-sweep")
    t.start()
    return t

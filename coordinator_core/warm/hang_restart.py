"""Hang auto-restart: N stall dumps inside a window make the warm server exit itself.

Evaluated where a stall is detected (`note_stall`, in memory, before any filesystem call) and
where a stall dump lands (`maybe_restart`, counting records on disk), so a healthy engine pays
nothing: no timer, no watcher process; a helper thread exists only at the threshold. The
respawn-on-next-op path restarts the server; this module only decides, logs one line, and exits.

Every decision appends one line to `<svc>/hang-restarts.log`:
`<UTC> pid=<server pid> action=restart|suppressed [reason=...] count=.. window_s=.. dumps=..`.
"""

from __future__ import annotations

import calendar
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.warm import breadcrumb

HANG_DUMP_THRESHOLD = 8
HANG_WINDOW_SECS = 60.0
RESTART_MIN_INTERVAL_SECS = 20.0
HANG_RESTART_LOG_FILENAME = "hang-restarts.log"
_LOG_TAIL_BYTES = 16384
_DUMPS_NAMED = 5
_STAMP_FMT = "%Y%m%dT%H%M%SZ"
_LINE_STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

_decision_lock = threading.Lock()


def exit_server(server_pid: int) -> None:
    """End the warm server: `os._exit` when it is this process, else a hard signal to the
    server pid (a pool worker passes its parent). No cleanup runs; the breadcrumb is
    reclaimed by the next spawn."""
    if server_pid == os.getpid():
        os._exit(0)
        return
    os.kill(server_pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def _stamp_epoch(name: str) -> Optional[int]:
    try:
        return calendar.timegm(time.strptime(name[:16], _STAMP_FMT))
    except ValueError:
        return None


def _recent_dumps(stall_dir: Path, since: float) -> list:
    """`(epoch, path)` of stall records stamped at or after `since`, oldest first."""
    found = []
    for path in stall_dir.glob("*.txt"):
        if path.name.endswith("-recycle.txt"):
            continue
        epoch = _stamp_epoch(path.name)
        if epoch is not None and epoch >= since:
            found.append((epoch, path))
    found.sort()
    return found


def _tail_lines(log: Path) -> list:
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _line_epoch(line: str) -> Optional[int]:
    try:
        return calendar.timegm(time.strptime(line.split(" ", 1)[0], _LINE_STAMP_FMT))
    except ValueError:
        return None


def _last_line(lines: list, *, restart: bool) -> tuple:
    """`(epoch, line)` of the newest restart (or suppressed) line, `(None, '')` when none."""
    marker = " action=restart " if restart else " action=suppressed "
    for line in reversed(lines):
        if marker in line + " ":
            epoch = _line_epoch(line)
            if epoch is not None:
                return epoch, line
    return None, ""


def _handover_held(svc: Path, now: float) -> bool:
    """Mirrors `breadcrumb.handover_pending_remaining_secs` for a known runtime dir
    (the caller holds `svc`, not the engine root)."""
    try:
        record = json.loads((svc / breadcrumb.HANDOVER_PENDING_FILENAME).read_text(encoding="utf-8"))
        expires_at = record["expires_at"]
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return False
        return min(float(expires_at) - now, breadcrumb.HANDOVER_PENDING_TTL_SECS) > 0.0
    except Exception:  # noqa: BLE001 -- an unreadable hint reads as not held
        return False


def _hold_reason(svc: Path, now: float) -> Optional[str]:
    if breadcrumb.publish_marker_held(svc, now=now):
        return "publish-hold"
    if _handover_held(svc, now):
        return "handover-hold"
    return None


def _append(log: Path, now: float, server_pid: int, action: str, count: int, window: float, dumps: list) -> None:
    named = ";".join(str(p) for _e, p in dumps[-_DUMPS_NAMED:])
    extra = len(dumps) - _DUMPS_NAMED
    if extra > 0:
        named += f";+{extra}-more"
    stamp = time.strftime(_LINE_STAMP_FMT, time.gmtime(now))
    with open(log, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{stamp} pid={server_pid} action={action} count={count} window_s={window:g} dumps={named}\n")


_DECISION_DEADLINE_SECS = 1.0
_stall_marks: dict = {}
_stall_marks_lock = threading.Lock()


def _disk_decision(stall_dir: Path, pid: int, now: float, count: int, window: float, min_interval: float) -> str:
    """The hold / rate-limit / log leg of an in-memory trigger. Reads and appends
    files, so the caller bounds it."""
    svc = stall_dir.parent
    log = svc / HANG_RESTART_LOG_FILENAME
    with _decision_lock:
        lines = _tail_lines(log)
        last_restart, _ = _last_line(lines, restart=True)
        reason = _hold_reason(svc, now)
        if reason is None and last_restart is not None and now - last_restart < min_interval:
            reason = "rate-limited"
        if reason is not None:
            last_sup, line = _last_line(lines, restart=False)
            if last_sup is None or now - last_sup >= window or f"reason={reason}" not in line:
                _append(log, now, pid, f"suppressed reason={reason}", count, window, [])
            return "suppressed"
        _append(log, now, pid, "restart", count, window, [])
    return "restart"


def note_stall(
    stall_dir,
    *,
    server_pid: Optional[int] = None,
    exit_fn: Optional[Callable[[int], None]] = None,
    threshold: int = HANG_DUMP_THRESHOLD,
    window: float = HANG_WINDOW_SECS,
    min_interval: float = RESTART_MIN_INTERVAL_SECS,
    deadline: float = _DECISION_DEADLINE_SECS,
) -> Optional[str]:
    """Count a stall in memory the instant it is detected, before any filesystem call.

    `maybe_restart` counts stall records on disk, so it cannot see a stall of the very
    filesystem it reads. At `threshold` marks inside `window` (per `stall_dir`), the
    hold / rate-limit / log leg runs on a helper thread bounded by `deadline`; a hold
    or rate limit suppresses, an unfinished leg (stalled disk) restarts anyway.
    Returns `"restart"`, `"suppressed"` or None. Never raises.
    """
    try:
        stall_dir = Path(stall_dir)
        mono = time.monotonic()
        pid = os.getpid() if server_pid is None else server_pid
        with _stall_marks_lock:
            marks = [m for m in _stall_marks.get(str(stall_dir), []) if mono - m < window]
            marks.append(mono)
            _stall_marks[str(stall_dir)] = marks
            count = len(marks)
            if count < threshold:
                return None
            _stall_marks.pop(str(stall_dir), None)
        verdict: list = []

        def _leg() -> None:
            try:
                verdict.append(_disk_decision(stall_dir, pid, time.time(), count, window, min_interval))
            except Exception:  # noqa: BLE001 -- an unreadable hold reads as no hold
                verdict.append("restart")

        helper = threading.Thread(target=_leg, name="hang-restart-decision", daemon=True)
        helper.start()
        helper.join(deadline)
        if verdict and verdict[0] == "suppressed":
            return "suppressed"
        (exit_fn or exit_server)(pid)
        return "restart"
    except Exception:  # noqa: BLE001 -- the dump path never raises
        return None


def maybe_restart(
    stall_dir,
    *,
    server_pid: Optional[int] = None,
    now: Optional[float] = None,
    exit_fn: Optional[Callable[[int], None]] = None,
    threshold: int = HANG_DUMP_THRESHOLD,
    window: float = HANG_WINDOW_SECS,
    min_interval: float = RESTART_MIN_INTERVAL_SECS,
) -> Optional[str]:
    """Count stall records in the last `window` seconds (only those after the previous
    restart line); at `threshold`, log one line and call `exit_fn(server_pid)`.

    Returns `"restart"`, `"suppressed"` or None (below threshold). A publish marker or
    handover-pending hold, or a restart less than `min_interval` ago, logs a suppressed
    line (at most one per reason per window) and does not exit. Never raises.
    """
    try:
        stall_dir = Path(stall_dir)
        current = time.time() if now is None else now
        pid = os.getpid() if server_pid is None else server_pid
        svc = stall_dir.parent
        log = svc / HANG_RESTART_LOG_FILENAME
        with _decision_lock:
            lines = _tail_lines(log)
            last_restart, _ = _last_line(lines, restart=True)
            since = current - window
            if last_restart is not None:
                since = max(since, last_restart + 1)
            dumps = _recent_dumps(stall_dir, since)
            if len(dumps) < threshold:
                return None
            reason = _hold_reason(svc, current)
            if reason is None and last_restart is not None and current - last_restart < min_interval:
                reason = "rate-limited"
            if reason is not None:
                last_sup, line = _last_line(lines, restart=False)
                if last_sup is None or current - last_sup >= window or f"reason={reason}" not in line:
                    _append(log, current, pid, f"suppressed reason={reason}", len(dumps), window, dumps)
                return "suppressed"
            _append(log, current, pid, "restart", len(dumps), window, dumps)
        (exit_fn or exit_server)(pid)
        return "restart"
    except Exception:  # noqa: BLE001 -- the dump path never raises
        return None

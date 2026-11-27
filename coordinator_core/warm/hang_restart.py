"""Hang auto-restart: N stall dumps inside a window make the warm server exit itself.

Evaluated where a stall is detected (`note_stall`, in memory, before any filesystem call) and
where a stall dump lands (`maybe_restart`, counting records on disk), so a healthy engine pays
nothing: no timer, no watcher process; a helper thread exists only at the threshold. The
respawn-on-next-op path restarts the server; this module only decides, logs one line, and exits.

Every decision appends one line to `<svc>/hang-restarts.log`:
`<UTC> pid=<server pid> action=restart|suppressed|io-stall [reason=...] count=.. window_s=.. backoff_s=.. dumps=..`
(`io-stall`: the window's stalled legs are blocked reads, so restarting would not clear the disk; report only.)
(`suppressed reason=io-saturated-by-peer`: a non-engine process tops the box's read sample, bounded by `IO_SATURATED_MAX_DEFER_SECS`;
`suppressed reason=worker-replaced`: a held pool worker was retired alone while peers served, before any whole-engine restart.)
(`backoff_s` is the wait owed the previous restart at decision time, 0 for a first restart).
"""

from __future__ import annotations

import calendar
import json
import os
import re
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.warm import breadcrumb

HANG_DUMP_THRESHOLD = 8
HANG_WINDOW_SECS = 60.0
# A replacement server must outlive its own cold start: its pool workers import for ~10s under the
# hook backlog that killed its predecessor, and that import stall alone reaches HANG_DUMP_THRESHOLD.
# 20s made every replacement self-exit before it warmed (a restart loop, observed 2026-11-11).
RESTART_MIN_INTERVAL_SECS = 90.0
# The wait after the Nth consecutive restart is RESTART_MIN_INTERVAL_SECS * 2**(N-1), up to this cap.
RESTART_BACKOFF_CAP_SECS = 900.0
# A previous restart older than this ends the chain: the next restart is a first one again. It
# exceeds the cap so a capped wait is never mistaken for quiet.
RESTART_QUIET_WINDOW_SECS = 1800.0
HANG_RESTART_LOG_FILENAME = "hang-restarts.log"
_LOG_TAIL_BYTES = 16384
_DUMPS_NAMED = 5
# Header token a stall record carries when its holders are blocked on I/O rather than hung; the
# writer imports this so the two sides cannot drift.
IO_WAIT_STALL_CLASS = "io-wait"
IO_WAIT_HEADER = f"stall-class: {IO_WAIT_STALL_CLASS}"
_HEADER_LINES = 6
_STAMP_FMT = "%Y%m%dT%H%M%SZ"
_LINE_STAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

_decision_lock = threading.Lock()


def _kill_quietly(proc) -> None:
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 -- gone, or not ours to kill
        pass


def exit_server(server_pid: int) -> None:
    """End the warm server and its whole process tree. Never raises.

    The descendants are snapshotted first and killed (this process excepted), then the server
    (a pool worker passes its parent); `os._exit` runs last and only when the caller is the
    server or one of its descendants, so a worker's parent is signalled before the worker dies.
    A failed snapshot falls back to the server pid alone. No cleanup runs; the breadcrumb is
    reclaimed by the next spawn."""
    me = os.getpid()
    children: list = []
    try:
        import psutil

        children = psutil.Process(server_pid).children(recursive=True)
    except Exception:  # noqa: BLE001 -- NoSuchProcess / AccessDenied: pid-only kill below
        children = []
    in_tree = server_pid == me or any(child.pid == me for child in children)
    for child in children:
        if child.pid != me:
            _kill_quietly(child)
    if server_pid != me:
        try:
            os.kill(server_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    if in_tree:
        os._exit(0)


def _stamp_epoch(name: str) -> Optional[int]:
    try:
        return calendar.timegm(time.strptime(name[:16], _STAMP_FMT))
    except ValueError:
        return None


_IMPORT_SCAN_BYTES = 65536
_THREAD_HEADER_SUFFIX = "(most recent call first):"
_FRAME_RE = re.compile(r'^\s*File "([^"]*)", line \d+ in (\S+)')
_IMPORT_WAIT_FUNCS = frozenset({"_path_stat", "find_spec"})


def _is_import_wait_top(frame_line: str) -> bool:
    """True when a stack's top frame sits in an importlib path stat / spec lookup or zipimport."""
    match = _FRAME_RE.match(frame_line)
    if match is None:
        return False
    filename, func = match.groups()
    if "zipimport" in filename:
        return True
    if func == "_path_stat":
        return True
    return func == "find_spec" and "importlib" in filename


def _has_import_wait_top(text: str) -> bool:
    expect_top = False
    for line in text.splitlines():
        if expect_top:
            expect_top = False
            if _is_import_wait_top(line):
                return True
        elif line.rstrip().endswith(_THREAD_HEADER_SUFFIX):
            expect_top = True
    return False


def _is_io_wait_record(path: Path) -> bool:
    """True when the record is held on the filesystem: its header carries `stall-class: io-wait`, or
    a thread's top frame is an import wait (importlib `_path_stat` / `find_spec`, zipimport).
    Unreadable reads as hung."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = [fh.readline() for _ in range(_HEADER_LINES)]
            if any(line.strip() == IO_WAIT_HEADER for line in head):
                return True
            rest = fh.read(_IMPORT_SCAN_BYTES)
    except OSError:
        return False
    return _has_import_wait_top("".join(head) + rest)


def _window_io_held(stall_dir: Path, since: float) -> bool:
    """True when held (io-wait / import-wait) records outnumber hung ones stamped at or after `since`."""
    held = hung = 0
    for path in stall_dir.glob("*.txt"):
        if path.name.endswith("-recycle.txt"):
            continue
        epoch = _stamp_epoch(path.name)
        if epoch is None or epoch < since:
            continue
        if _is_io_wait_record(path):
            held += 1
        else:
            hung += 1
    return held > hung


def _recent_dumps(stall_dir: Path, since: float, io_wait: bool = False) -> list:
    """`(epoch, path)` of hung-stall records (io-wait records when `io_wait`) stamped at or after
    `since`, oldest first.

    Recycle records are neither; io-wait records are not hang evidence."""
    found = []
    for path in stall_dir.glob("*.txt"):
        if path.name.endswith("-recycle.txt"):
            continue
        epoch = _stamp_epoch(path.name)
        if epoch is not None and epoch >= since and _is_io_wait_record(path) == io_wait:
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


def _backoff_secs(lines: list, now: float, base: float) -> float:
    """The wait the next restart owes the previous one, from consecutive restart lines in `lines`.

    A chain is restart lines each less than the quiet window after the one before it, ending
    within the quiet window of `now`; `n` of them owe `base * 2**(n-1)` up to the cap. 0.0 when
    there is no chain (no restart, or the last one is older than the quiet window)."""
    epochs = [
        e
        for line in lines
        if " action=restart " in line + " " and (e := _line_epoch(line)) is not None
    ]
    if not epochs or now - epochs[-1] >= RESTART_QUIET_WINDOW_SECS:
        return 0.0
    n = 1
    for newer, older in zip(reversed(epochs[1:]), reversed(epochs[:-1])):
        if newer - older >= RESTART_QUIET_WINDOW_SECS:
            break
        n += 1
    return min(base * 2 ** min(n - 1, 30), max(RESTART_BACKOFF_CAP_SECS, base))


STALLED_SUCCESSOR_REASON = "stalled-successor"
IO_SATURATED_REASON = "io-saturated-by-peer"
WORKER_REPLACED_REASON = "worker-replaced"
# A top reader below this rate is noise, not saturation.
IO_SATURATION_MIN_MBPS = 4.0
# Suppression for a saturated box is bounded: a chain of io-saturated suppressions this old stops
# shielding, so a genuinely hung engine still reaches the whole-engine restart.
IO_SATURATED_MAX_DEFER_SECS = 600.0
_BOX_IO_KEY = "box-io-read-top"
_BOX_IO_SCAN_BYTES = 16384
_BOX_IO_RECORDS_SCANNED = 8
_THREADS_MARKER = "--- threads ---"

_replacer_lock = threading.Lock()
_worker_replacer: Optional[Callable[[], bool]] = None


def set_worker_replacer(fn: Optional[Callable[[], bool]]) -> None:
    """Register the server's in-place replacement: `fn()` retires every held pool worker alone and
    returns True when it retired at least one. None unregisters."""
    global _worker_replacer
    with _replacer_lock:
        _worker_replacer = fn


def _try_replace_held_worker() -> bool:
    with _replacer_lock:
        fn = _worker_replacer
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001 -- a failed replacement falls through to the restart
        return False


def _engine_pids(server_pid: int) -> frozenset:
    pids = {server_pid, os.getpid()}
    try:
        import psutil

        pids.update(child.pid for child in psutil.Process(server_pid).children(recursive=True))
    except Exception:  # noqa: BLE001 -- pid-only set below
        pass
    return frozenset(pids)


def _top_box_reader(path: Path) -> Optional[tuple]:
    """`(pid, MB/s)` of the first entry of a record's `box-io-read-top<N>` header line, None when
    the record carries none or it is unparseable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(_BOX_IO_SCAN_BYTES)
    except OSError:
        return None
    for line in head.split(_THREADS_MARKER, 1)[0].splitlines():
        if not line.startswith(_BOX_IO_KEY):
            continue
        entry = line.partition(":")[2].split("|", 1)[0].strip()
        _name, _sep, rest = entry.rpartition(" pid=")
        parts = rest.split()
        if len(parts) != 2 or not parts[1].endswith("MB/s"):
            return None
        try:
            return int(parts[0]), float(parts[1][: -len("MB/s")])
        except ValueError:
            return None
    return None


def _box_io_saturated(stall_dir: Path, since: float, server_pid: int) -> bool:
    """True when the newest window record carrying a box-side sample names a non-engine process as its
    top reader at or above `IO_SATURATION_MIN_MBPS`: the disk is being drained by a peer, which a
    restart cannot clear. Reads at most a few records' headers."""
    records = []
    for path in stall_dir.glob("*.txt"):
        if path.name.endswith("-recycle.txt"):
            continue
        epoch = _stamp_epoch(path.name)
        if epoch is not None and epoch >= since:
            records.append((epoch, path))
    records.sort(reverse=True)
    for _epoch, path in records[:_BOX_IO_RECORDS_SCANNED]:
        top = _top_box_reader(path)
        if top is not None:
            pid, mbps = top
            return mbps >= IO_SATURATION_MIN_MBPS and pid not in _engine_pids(server_pid)
    return False


def _saturation_defer_spent(lines: list, now: float) -> bool:
    """True when the unbroken chain of io-saturated suppressions (no restart between) began
    `IO_SATURATED_MAX_DEFER_SECS` or more before `now`."""
    first = None
    for line in reversed(lines):
        if " action=restart " in line + " ":
            break
        if f" reason={IO_SATURATED_REASON} " in line + " ":
            epoch = _line_epoch(line)
            if epoch is not None:
                first = epoch
    return first is not None and now - first >= IO_SATURATED_MAX_DEFER_SECS


def _line_pid(line: str) -> Optional[int]:
    for token in line.split():
        if token.startswith("pid="):
            try:
                return int(token[4:])
            except ValueError:
                return None
    return None


def _never_progressed() -> bool:
    """True when this process has not completed an op since boot."""
    with _progress_lock:
        return _progress_last is None


def _gate(
    svc: Path,
    lines: list,
    now: float,
    base: float,
    pid: Optional[int] = None,
    io_held: bool = False,
    successor_check: bool = True,
    io_saturated: bool = False,
) -> tuple:
    """`(reason, backoff_s)`: why a restart is suppressed (None when allowed) and the wait owed.

    `io_saturated` (a non-engine process is draining the disk) suppresses as `io-saturated-by-peer`
    until the chain of such suppressions is `IO_SATURATED_MAX_DEFER_SECS` old.

    A backoff never shields a successor (pid differs from the last restart line's) that has not
    completed an op since boot is stalled re-importing, and its restart is `stalled-successor`
    (`pid` must be this process: the progress stamp is per process).
    Not when `io_held`: the window's stalls are filesystem / import waits a restart re-creates,
    nor when `successor_check` is False (the abandoned-thread recycle, which a backoff always shields).
    Holds still apply."""
    backoff = _backoff_secs(lines, now, base)
    reason = _hold_reason(svc, now)
    if reason is None and io_saturated and not _saturation_defer_spent(lines, now):
        reason = IO_SATURATED_REASON
    if reason is None and backoff > 0.0:
        last_restart, last_line = _last_line(lines, restart=True)
        if last_restart is not None and now - last_restart < backoff:
            last_pid = _line_pid(last_line)
            if (
                not io_held
                and successor_check
                and pid == os.getpid()
                and last_pid is not None
                and pid != last_pid
                and _never_progressed()
            ):
                return STALLED_SUCCESSOR_REASON, backoff
            reason = "backoff"
    return reason, backoff


def _append(
    log: Path,
    now: float,
    server_pid: int,
    action: str,
    count: int,
    window: float,
    dumps: list,
    backoff_s: float = 0.0,
    source: Optional[str] = None,
) -> None:
    named = ";".join(str(p) for _e, p in dumps[-_DUMPS_NAMED:])
    extra = len(dumps) - _DUMPS_NAMED
    if extra > 0:
        named += f";+{extra}-more"
    stamp = time.strftime(_LINE_STAMP_FMT, time.gmtime(now))
    src = f" source={source}" if source else ""
    with open(log, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(
            f"{stamp} pid={server_pid} action={action} count={count} window_s={window:g}"
            f" backoff_s={backoff_s:g}{src} dumps={named}\n"
        )


def _record_decision(
    svc: Path,
    pid: int,
    now: float,
    count: int,
    window: float,
    base: float,
    dumps: list,
    source: Optional[str],
    lines: list,
    progress: bool = False,
    io_held: bool = False,
    successor_check: bool = True,
    io_saturated: bool = False,
    replace_held: bool = False,
) -> str:
    """Apply the progress / hold / backoff gate to `lines` and append the one line it earns; caller holds the lock.

    `replace_held`: a restart the gate would allow, while a peer worker is still completing ops,
    first tries the registered in-place replacement (`set_worker_replacer`) and, when it retired a
    worker, is suppressed as `worker-replaced`."""
    log = svc / HANG_RESTART_LOG_FILENAME
    reason, backoff = _gate(svc, lines, now, base, pid, io_held, successor_check, io_saturated)
    if progress:
        reason = "progress"
    if reason is None and replace_held and progressed_within(window or HANG_WINDOW_SECS):
        if _try_replace_held_worker():
            reason = WORKER_REPLACED_REASON
    if reason == STALLED_SUCCESSOR_REASON:
        _append(log, now, pid, f"restart reason={reason}", count, window, dumps, backoff, source)
        return "restart"
    if reason is not None:
        last_sup, line = _last_line(lines, restart=False)
        if last_sup is None or now - last_sup >= window or f"reason={reason}" not in line:
            _append(log, now, pid, f"suppressed reason={reason}", count, window, dumps, backoff, source)
        return "suppressed"
    _append(log, now, pid, "restart", count, window, dumps, backoff, source)
    return "restart"


def decide(svc_dir, *, server_pid: int, source: str, now: Optional[float] = None) -> str:
    """Hold / backoff decision for a restart a caller wants (`source` names the caller).

    Logs one line to `<svc_dir>/hang-restarts.log` and returns `"restart"` or `"suppressed"`;
    the caller ends the server (`exit_server`). Reads and appends files, so the caller bounds it."""
    svc = Path(svc_dir)
    current = time.time() if now is None else now
    with _decision_lock:
        lines = _tail_lines(svc / HANG_RESTART_LOG_FILENAME)
        return _record_decision(svc, server_pid, current, 0, 0.0, RESTART_MIN_INTERVAL_SECS, [], source, lines)


_DECISION_DEADLINE_SECS = 1.0
_stall_marks: dict = {}
_first_suppress: dict = {}
_stall_marks_lock = threading.Lock()


_progress_lock = threading.Lock()
_progress_last: Optional[float] = None


def note_progress() -> None:
    """Record that a pool worker finished an op. O(1), in memory, no filesystem.

    The stamp is per process: it lives in the server, which alone makes the restart decision
    (a pool worker, with `_WORKER_STALL_DIR` set, only writes its record)."""
    global _progress_last
    mono = time.monotonic()
    with _progress_lock:
        _progress_last = mono


def progressed_within(window: float) -> bool:
    """True when an op completed in the last `window` seconds (monotonic)."""
    with _progress_lock:
        last = _progress_last
    return last is not None and time.monotonic() - last < window


# Progress excuses a stall burst only briefly. With ~30 workers, cheap guard ops keep completing
# while the accept loop or the dispatch pool is wedged, so "something finished" alone held a
# wedged server alive for 5 minutes (2026-11-22 22:52-22:57Z, pid 50052). Past this many seconds
# of back-to-back threshold bursts, progress no longer suppresses and the server restarts.
PROGRESS_GRACE_SECS = 30.0
_progress_excused_since: dict = {}


def _progress_excuses(key: str, mono: float, window: float, grace: float) -> bool:
    """True while progress may still suppress this burst; caller holds `_stall_marks_lock`.

    A burst more than `window` after the previous excused one starts a fresh grace period: the
    stall episode ended in between."""
    since, last = _progress_excused_since.get(key, (None, None))
    if since is None or last is None or mono - last > window:
        since = mono
    _progress_excused_since[key] = (since, mono)
    return mono - since < grace


def _disk_decision(
    stall_dir: Path, pid: int, now: float, count: int, window: float, min_interval: float, progress: bool = False
) -> str:
    """The progress / hold / backoff / log leg of an in-memory trigger. Reads and appends
    files, so the caller bounds it."""
    svc = stall_dir.parent
    with _decision_lock:
        lines = _tail_lines(svc / HANG_RESTART_LOG_FILENAME)
        io_held = _window_io_held(stall_dir, now - window)
        io_saturated = _box_io_saturated(stall_dir, now - window, pid)
        return _record_decision(
            svc, pid, now, count, window, min_interval, [], None, lines, progress, io_held,
            io_saturated=io_saturated, replace_held=True,
        )


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
    hold / backoff / log leg runs on a helper thread bounded by `deadline`; a hold
    or backoff suppresses. An unfinished leg (a disk too stalled to read the hold file) suppresses
    only while this `stall_dir`'s first finished suppression, held in memory, is younger than
    `IO_SATURATED_MAX_DEFER_SECS`; with none, or past it, it restarts, so a disk that never
    answers cannot wedge the engine forever.
    Returns `"suppressed"`, `"restart"` (a finished leg that allowed it) or None. Never raises.
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
            progress = progressed_within(window) and _progress_excuses(
                str(stall_dir), mono, window, PROGRESS_GRACE_SECS
            )
        verdict: list = []

        def _leg() -> None:
            try:
                verdict.append(_disk_decision(stall_dir, pid, time.time(), count, window, min_interval, progress))
            except Exception:  # noqa: BLE001 -- an unreadable hold reads as no hold
                verdict.append("restart")

        helper = threading.Thread(target=_leg, name="hang-restart-decision", daemon=True)
        helper.start()
        helper.join(deadline)
        key = str(stall_dir)
        if progress:
            return "suppressed"
        if verdict and verdict[0] == "suppressed":
            with _stall_marks_lock:
                _first_suppress.setdefault(key, time.monotonic())
            return "suppressed"
        if not verdict:
            with _stall_marks_lock:
                first = _first_suppress.get(key)
            if first is not None and time.monotonic() - first < IO_SATURATED_MAX_DEFER_SECS:
                return "suppressed"
        with _stall_marks_lock:
            _first_suppress.pop(key, None)
        (exit_fn or exit_server)(pid)
        return "restart"
    except Exception:  # noqa: BLE001 -- the dump path never raises
        return None


# The question the threshold answers: how many mutating handler threads can be stuck past their
# abandoned dispatch before the engine has lost enough thread capacity that waiting for a hand
# stop costs more than a restart? One slow op abandons itself; four wedged at once (the
# 2026-11-18 io-wait pile-up held three mutating legs) is a leak that does not drain.
ABANDONED_MUTATING_RECYCLE_THRESHOLD = 4
ABANDONED_SOURCE = "abandoned-mutating"


def maybe_restart_on_abandoned(
    count: int,
    *,
    svc_dir,
    server_pid: Optional[int] = None,
    exit_fn: Optional[Callable[[int], None]] = None,
    threshold: int = ABANDONED_MUTATING_RECYCLE_THRESHOLD,
    min_interval: float = RESTART_MIN_INTERVAL_SECS,
    deadline: float = _DECISION_DEADLINE_SECS,
) -> Optional[str]:
    """Recycle the server when `count` abandoned mutating threads are still running.

    `count` comes from memory (`dispatch_abandon`), so no filesystem read precedes the exit and
    the io-stall no-exit carve-out of `maybe_restart` does not apply: the stalled disk is the
    case this trigger exists for. The hold / backoff / log leg runs on a helper thread bounded by
    `deadline`; a publish or handover hold or a restart inside the backoff suppresses, an
    unfinished leg restarts anyway. Replays nothing: admitted records read engine-restarted.
    Returns `"restart"`, `"suppressed"` or None (below threshold). Never raises.
    """
    try:
        if count < threshold:
            return None
        pid = os.getpid() if server_pid is None else server_pid
        svc = Path(svc_dir)
        verdict: list = []

        def _leg() -> None:
            try:
                with _decision_lock:
                    lines = _tail_lines(svc / HANG_RESTART_LOG_FILENAME)
                    verdict.append(
                        _record_decision(
                            svc, pid, time.time(), count, 0.0, min_interval, [], ABANDONED_SOURCE, lines,
                            successor_check=False,
                        )
                    )
            except Exception:  # noqa: BLE001 -- an unreadable hold reads as no hold
                verdict.append("restart")

        helper = threading.Thread(target=_leg, name="hang-restart-abandoned", daemon=True)
        helper.start()
        helper.join(deadline)
        if verdict and verdict[0] == "suppressed":
            return "suppressed"
        (exit_fn or exit_server)(pid)
        return "restart"
    except Exception:  # noqa: BLE001 -- the abandon path never raises
        return None


# A claim race answers one op undispatched while a sibling worker holds the claim, so busy_workers
# is above 0 or the next op is served. Consecutive undispatched answers with no worker busy cannot
# be a race: the pool is wedged. Eight in a row is a pool answering nothing else.
UNDISPATCHED_WEDGE_THRESHOLD = 8
UNDISPATCHED_SOURCE = "undispatched-wedge"
UNDISPATCHED_OUTCOME = "undispatched"
_undispatched_streaks: dict = {}
_undispatched_lock = threading.Lock()


def note_undispatched(
    svc_dir,
    *,
    server_pid: Optional[int] = None,
    busy_workers: int,
    outcome: Optional[str],
    exit_fn: Optional[Callable[[int], None]] = None,
    threshold: int = UNDISPATCHED_WEDGE_THRESHOLD,
    min_interval: float = RESTART_MIN_INTERVAL_SECS,
    deadline: float = _DECISION_DEADLINE_SECS,
) -> Optional[str]:
    """Count a served op answered undispatched with no busy worker; at `threshold` in a row, decide a restart.

    O(1) and in memory below threshold (no filesystem). Any other outcome, or `busy_workers > 0`,
    resets the streak. Never resets on `progressed_within`: the pool settles an undispatched
    answer through `note_progress`, so progress is the wedge's own signature. At threshold the
    hold / backoff / log leg (`_gate` backoff, source `undispatched-wedge`) runs on a helper thread
    bounded by `deadline`; a hold or backoff suppresses, an unfinished leg restarts anyway.
    Returns `"restart"`, `"suppressed"` or None (below threshold). Never raises.
    """
    try:
        key = str(svc_dir)
        with _undispatched_lock:
            if outcome != UNDISPATCHED_OUTCOME or busy_workers > 0:
                _undispatched_streaks.pop(key, None)
                return None
            count = _undispatched_streaks.get(key, 0) + 1
            if count < threshold:
                _undispatched_streaks[key] = count
                return None
            _undispatched_streaks.pop(key, None)
        pid = os.getpid() if server_pid is None else server_pid
        svc = Path(svc_dir)
        verdict: list = []

        def _leg() -> None:
            try:
                with _decision_lock:
                    lines = _tail_lines(svc / HANG_RESTART_LOG_FILENAME)
                    verdict.append(
                        _record_decision(
                            svc, pid, time.time(), count, 0.0, min_interval, [], UNDISPATCHED_SOURCE, lines
                        )
                    )
            except Exception:  # noqa: BLE001 -- an unreadable hold reads as no hold
                verdict.append("restart")

        helper = threading.Thread(target=_leg, name="hang-restart-undispatched", daemon=True)
        helper.start()
        helper.join(deadline)
        if verdict and verdict[0] == "suppressed":
            return "suppressed"
        (exit_fn or exit_server)(pid)
        return "restart"
    except Exception:  # noqa: BLE001 -- the served-row path never raises
        return None


def _report_io_stall(
    svc: Path, pid: int, now: float, stall_dir: Path, since: float, threshold: int, window: float, lines: list
) -> Optional[str]:
    """Log one `action=io-stall` line per window when io-wait records reach `threshold`; never restarts."""
    io_dumps = _recent_dumps(stall_dir, since, io_wait=True)
    if len(io_dumps) < threshold:
        return None
    for line in reversed(lines):
        if " action=io-stall " in line + " ":
            epoch = _line_epoch(line)
            if epoch is not None and now - epoch < window:
                return "io-stall"
            break
    _append(svc / HANG_RESTART_LOG_FILENAME, now, pid, "io-stall", len(io_dumps), window, io_dumps, 0.0, None)
    return "io-stall"


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

    Returns `"restart"`, `"suppressed"`, `"io-stall"` (io-wait records alone reached `threshold`:
    logged, no exit) or None (below threshold). A publish marker or
    handover-pending hold, or a restart inside the backoff (`min_interval * 2**(n-1)` after the
    nth consecutive restart, capped), logs a suppressed line (at most one per reason per window)
    and does not exit. Never raises.
    """
    try:
        stall_dir = Path(stall_dir)
        current = time.time() if now is None else now
        pid = os.getpid() if server_pid is None else server_pid
        svc = stall_dir.parent
        with _decision_lock:
            lines = _tail_lines(svc / HANG_RESTART_LOG_FILENAME)
            last_restart, _ = _last_line(lines, restart=True)
            since = current - window
            if last_restart is not None:
                since = max(since, last_restart + 1)
            dumps = _recent_dumps(stall_dir, since)
            if len(dumps) < threshold:
                return _report_io_stall(svc, pid, current, stall_dir, since, threshold, window, lines)
            progress = progressed_within(window)
            if progress:
                with _stall_marks_lock:
                    progress = _progress_excuses(str(stall_dir), time.monotonic(), window, PROGRESS_GRACE_SECS)
            io_saturated = _box_io_saturated(stall_dir, since, pid)
            verdict = _record_decision(
                svc, pid, current, len(dumps), window, min_interval, dumps, None, lines, progress,
                io_saturated=io_saturated, replace_held=True,
            )
        if verdict == "suppressed":
            return "suppressed"
        (exit_fn or exit_server)(pid)
        return "restart"
    except Exception:  # noqa: BLE001 -- the dump path never raises
        return None

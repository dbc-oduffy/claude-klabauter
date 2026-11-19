"""
coordinator_core.testing.suite_mutex — machine-wide, cross-repo test-suite mutex.

Purpose: serialize full/fast test-suite invocations to ONE at a time across
every repo and every concurrent Claude session on the box. This is layer 6 of
the DR-088 test-breadth ladder: even a fully authorized Tier-U run must not
overlap another one. The harm is not politeness or wall-clock — concurrent
suite runs against shared trees produce *untrustworthy* output: mid-edit reads
raise fake assertion failures against constants HEAD already defines
correctly, and a test opening a large native-backed store while another
session holds its write lock aborts the pytest process outright.

Design (mirrors the atomic-mkdir shape formerly used by
``coordinator_core/ops/ceremony/ceremony_lock.py`` — cite, not import; that
now-removed commit-path lock was keyed on a git worktree and delegated its
liveness verdict to the session registry, neither of which is meaningful for
a machine-wide, repo-agnostic lock whose holder is a bare test-runner
process):

  - Lock directory: ``<settings-home>/test-suite-mutex.lock/``, directly at
    the settings-home root rather than under a plane-named subdirectory. The
    lock is machine-wide across every repo AND every engine plane (the
    source-tree checkout and the published ``claude-klabauter`` mirror), and
    a plane-named segment is exactly the kind of literal publish rewrites per
    plane — the source plane read ``claude-klabauter`` while the mirror plane
    read ``claude-klabauter``, so the two planes took different "machine-wide"
    locks and ran concurrently
    (``docs/plans/2026-09-11-boundary-identifiers-survive-publish.md``).
    Follows the ``<settings-home>/.coordinator-venv.lock`` precedent in
    ``coordinator_core/install/ensure_venv.py``: an out-of-repo,
    plane-independent path with no rewritable segment.
    ``os.mkdir`` is atomic on POSIX and on Windows — exactly one racing
    process observes success, every other observes ``FileExistsError``. Never
    check-then-write.
  - Holder metadata (``meta.json``) is written INSIDE the lock dir after the
    winning mkdir, atomically via a temp file + ``os.replace``.

PID-LIVENESS (load-bearing distinction): the repo lesson
``claim-lock-pid-is-not-liveness`` says a lock's recorded PID is often the
short-lived *CLI writer's* ``$$``, dead by design, and therefore worthless as
a liveness signal. That lesson does NOT apply here, and the difference is the
whole reason PID checking is safe in this module: the PID recorded here is the
PID of the process that **actually runs the suite** for the lock's whole
lifetime (``os.getpid()`` of the acquirer by default, or an explicit ``pid=``
handed in by a short-lived acquirer that spawns the runner). A dead PID here
therefore means the suite run genuinely ended — normally or by crash — so
reclaiming is correct rather than a wrongful takeover. Callers that acquire
from a process which will NOT outlive the run MUST pass ``pid=`` explicitly.

Staleness has independent signals; any one alone marks the lock stale:
  1. Dead PID (see above).
  2. Reused PID — the recorded process was created after the lock's
     ``started_at``, so it is not the process that took the lock.
  3. Stale heartbeat — ``held()`` refreshes ``heartbeat_at`` while the block
     runs; a lock whose heartbeat stopped (its owner died, even if an orphaned
     child still holds the pid) is stale after ``HEARTBEAT_STALE_SECS``.
  4. Age past ``STALE_TTL_SECS``, applied only to a holder that is not shown
     alive. A holder shown alive is never reclaimed on age alone.

Negative-spec — what this lock is NOT:
    - NOT a per-repo lock. It is deliberately machine-global; two different
      repos' suites contend for the same lock because they contend for the
      same CPU, disk, and shared native-backed stores.
    - NOT a general-purpose task lock. Do not reuse it to serialize
      ceremonies, commits, pipeline writes, or embedding jobs — a
      long-running unrelated holder would block the very test runs this
      exists to admit one-at-a-time.

PATH PARAMETER (``holder``/``acquire``/``release``/``held`` take a keyword-only
``path=``): every public entry point defaults to ``lock_path()`` and is
unchanged for every existing caller. The parameter exists so
``coordinator_core.testing.tier_t_slots`` can build a K-SLOT semaphore out of K
independent lock dirs without reimplementing this module's liveness, TTL-split,
grace-window and reclaim logic — all of which were bought with incidents and
must not be forked. What is shared is the single-slot PRIMITIVE; what is NOT
shared is the policy above. A caller passing ``path=`` is taking one slot of a
different lock and inherits this module's staleness semantics, not its
one-at-a-time-machine-wide meaning. Do not read a non-default ``path=`` holder
as "the suite mutex is held".
 The commit pipeline no longer holds a
      ceremony-wide mutex (``ceremony_lock.py`` was removed as a live
      mechanism, PM ruling 2026-08-07); single-file RMW uses
      ``coordinator_core/locked_write.py``.
    - NOT an authorization check. It answers "may this run start *now*",
      never "is this caller entitled to run a suite at all" — tier/grant
      enforcement is the ``bash_guards`` layer's job.
    - NOT a lock server. Blocking acquirers are served FIFO through a ticket
      queue (``<lock>.queue/``): only the oldest live ticket may ``mkdir``, so
      a releaser that retries at once queues behind existing waiters. Ticket
      names embed ``time.time_ns()``; a wall-clock step can misorder two
      tickets once, which is accepted.
    - Does NOT raise from ``holder()`` — a mutex whose read path can explode
      would take down every guard that consults it.
    - Does NOT use ``fcntl``, ``flock``, ``os.getuid``, POSIX signals for
      liveness, or a hardcoded ``/tmp``: Windows and macOS are both
      first-class.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from coordinator_core._settings_home import settings_home

_LOG = logging.getLogger(__name__)

# A full suite in this repo runs ~10 minutes; the slowest consumer-repo suites
# are a few times that. 45 minutes is ~4.5x the local full-suite budget — long
# enough that no honest run is ever reclaimed out from under itself, short
# enough that a crash which somehow left a live-but-wedged PID behind (a
# hung runner, a stopped process, a PID reused by an unrelated long-lived
# process) self-heals within one working session rather than wedging the fleet.
STALE_TTL_SECS: float = 45 * 60.0

# A holder that cannot be shown alive expires at `STALE_TTL_SECS`; one that is
# alive is never reclaimed on age (the fast tier alone has run past 22 minutes,
# a full-tier gate past 45). `held()` stamps `heartbeat_at` into the lock every
# `HEARTBEAT_INTERVAL_SECS` from the acquirer's own process; a lock carrying one
# that is older than `HEARTBEAT_STALE_SECS` has lost its owner, even when the
# recorded pid (an orphaned child of a dead wrapper) still reads alive. A lock
# with no `heartbeat_at` (a raw `acquire()` caller) opts out of that signal.
HEARTBEAT_INTERVAL_SECS: float = 30.0
HEARTBEAT_STALE_SECS: float = 5 * 60.0

# A reused pid is detected when its creation instant is later than `started_at`
# by more than this (`started_at` is truncated to whole seconds).
_CREATE_TIME_SLACK_SECS: float = 5.0

# Grace window for a lock dir whose meta.json is absent, truncated, or
# unparseable. mkdir and the metadata write are two operations, so a reader can
# legitimately observe the gap between them. Inside the window the lock is
# reported held (with unknown metadata) rather than reclaimed — reclaiming
# there would hand the lock to a second runner while the first is still
# starting. Outside it, missing metadata means a process died between the two
# writes and the lock is stale.
_META_GRACE_SECS: float = 30.0

_LOCK_DIRNAME = "test-suite-mutex.lock"
_META_FILENAME = "meta.json"

# DR-088 layer 6 take-side wait bound, shared by every real acquirer in this
# repo (`full_runner`, `validate-fast-and-packageability.py` `fast`,
# `workday-complete-step1-validate.py`). Originally established in
# `full_runner.py` (R10, 2026-07-28) and copy-pasted into a
# `coordinator/bin/lib/` shim for the two later bin/ call sites; folded onto
# this one shared copy since a `coordinator_core` package module importing
# from a `coordinator/bin/lib/` shim would have been the wrong dependency
# direction. A full suite in this repo runs ~10 minutes; the slowest
# consumer-repo suites are a few times that. 600s is well past that budget —
# long enough that no honest run is ever reclaimed out from under itself,
# short enough that a genuinely wedged holder does not stall a caller
# indefinitely; a timed-out acquire is a refusal (EX_TEMPFAIL), never a run.
MUTEX_WAIT_SECS: float = 600.0

# EX_TEMPFAIL, matching `with-suite-mutex`'s held-mutex refusal.
EX_TEMPFAIL: int = 75

_POLL_INITIAL_SECS = 0.1
_POLL_MAX_SECS = 5.0
_POLL_BACKOFF = 1.5

_UNKNOWN_OWNER = "<unknown>"

_QUEUE_SUFFIX = ".queue"
_TICKET_NAME_RE = re.compile(r"^\d{20}-\d+$")
_TICKET_CREATE_ATTEMPTS = 5

# An `adoptable` ticket is written by a `--wait` process that exits as soon as
# the mutex is free, before the follow-on run invocation acquires; pid liveness
# alone would prune it at once. It is held this long so the run can adopt it,
# and pruned after so an abandoned `--wait` never wedges the queue head.
_TICKET_ADOPT_GRACE_SECS: float = 60.0

# Cap on immediate mkdir retries after `holder()` reports the lock free — see
# the guarded branch in `acquire()`.
_RECLAIM_RETRY_LIMIT = 3


def mutex_owner(prefix: str) -> str:
    """Mutex owner id for the calling process.

    Prefers the coordinator session id so the guard's deny text names
    something an operator can act on ("held by <session>"), falling back to
    a ``<prefix>-pid-<pid>`` string when no session is in scope — an
    anonymous owner still serializes correctly, it just reads less well in
    the deny. ``prefix`` names the caller (e.g. ``"full_runner"``,
    ``"suite-mutex"``) so `holder()` output distinguishes which take-side
    path is holding the lock when no session id was available; callers MUST
    NOT collapse this to a single shared prefix.
    """
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "COORDINATOR_SESSION_ID"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return "%s-pid-%d" % (prefix, os.getpid())


def timeout_refusal(wait_secs: float) -> str:
    """Refusal text for a wait that timed out, naming the current holder."""
    current = holder() or {}
    return "[REFUSED] suite mutex held by %s; waited %ss; suite not run (exit %d)" % (
        current.get("owner", _UNKNOWN_OWNER),
        int(wait_secs),
        EX_TEMPFAIL,
    )


def lock_path() -> Path:
    """Return the machine-wide lock directory path under the settings home.

    Resolved fresh on every call (never cached at import time) so a test or a
    caller that repoints ``COORDINATOR_SETTINGS_HOME`` is honoured.
    """
    return settings_home() / _LOCK_DIRNAME


def _now_iso() -> str:
    """Return the current instant as an ISO-8601 UTC string with a Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: object) -> Optional[float]:
    """Parse an ISO-8601 UTC stamp into a POSIX timestamp, or None if unusable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _dir_mtime(path: Path) -> Optional[float]:
    """Return the lock dir's mtime as a POSIX timestamp, or None if unreadable."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _pid_alive(pid: int) -> Optional[bool]:
    """Return True/False for PID liveness, or None when it cannot be determined.

    Delegates to ``coordinator_core.ask_spool.record.pid_state``, the engine's
    one spawn-free probe (``GetExitCodeProcess == STILL_ACTIVE`` on Windows,
    ``os.kill(pid, 0)`` on POSIX). None — doubt — is deliberately distinct from
    False: an indeterminate read leaves the holder presumed alive, with
    ``STALE_TTL_SECS`` and the heartbeat as the staleness signals.
    """
    if pid <= 0:
        return None
    from coordinator_core.ask_spool.record import pid_state

    try:
        return pid_state(pid)
    except Exception as exc:
        _LOG.warning("suite_mutex: PID liveness indeterminate for pid %s: %s", pid, exc)
        return None


def _pid_create_time(pid: int) -> Optional[float]:
    """Creation instant of ``pid`` (epoch seconds), or None when unreadable."""
    from coordinator_core.ask_spool.record import pid_create_time

    try:
        return pid_create_time(pid)
    except Exception:
        return None


def _pid_reused(pid: int, started_at: str) -> bool:
    """True when ``pid`` names a process created after the lock was stamped.

    The recorded pid is always a process that existed before the stamp (the
    acquirer, or the child the wrapper spawned first), so a later creation
    instant means the pid was recycled for an unrelated process.
    """
    started = _parse_iso(started_at)
    created = _pid_create_time(pid)
    return started is not None and created is not None and created > started + _CREATE_TIME_SLACK_SECS


def _read_meta(path: Path) -> Optional[dict]:
    """Read and shape-validate the holder metadata, or None if unusable.

    Returns None for an absent, unreadable, truncated, non-JSON, or
    non-object metadata file — every corrupt-file case collapses to the same
    "metadata unknown" verdict rather than an exception.
    """
    try:
        raw = (path / _META_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    pid = parsed.get("pid")
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        pid_int = 0

    owner = parsed.get("owner")
    cmd = parsed.get("cmd")
    started_at = parsed.get("started_at")
    meta = {
        "pid": pid_int,
        "owner": owner if isinstance(owner, str) and owner else _UNKNOWN_OWNER,
        "started_at": started_at if isinstance(started_at, str) and started_at else "",
        "cmd": cmd if isinstance(cmd, str) else "",
    }
    heartbeat_at = parsed.get("heartbeat_at")
    if isinstance(heartbeat_at, str) and heartbeat_at:
        meta["heartbeat_at"] = heartbeat_at
    return meta


_REPLACE_ATTEMPTS = 6
_REPLACE_RETRY_SECS = 0.02


def _replace_meta(path: Path, payload: dict) -> None:
    """Atomically replace meta.json; retries a transient PermissionError (<=300ms total backoff)."""
    tmp_path = path / f".{_META_FILENAME}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(str(tmp_path), str(path / _META_FILENAME))
            return
        except PermissionError:
            # Windows denies the rename while a reader holds meta.json open; the denial is transient.
            if attempt == _REPLACE_ATTEMPTS - 1:
                _unlink_quiet(tmp_path)
                raise
            time.sleep(_REPLACE_RETRY_SECS * (attempt + 1))
        except BaseException:
            _unlink_quiet(tmp_path)
            raise


def _unlink_quiet(tmp_path: Path) -> None:
    try:
        tmp_path.unlink()
    except OSError:
        pass


def _write_meta(path: Path, *, pid: int, owner: str, cmd: str) -> None:
    """Atomically stamp holder metadata inside an already-acquired lock dir."""
    _replace_meta(path, {"pid": pid, "owner": owner, "started_at": _now_iso(), "cmd": cmd})


def touch_heartbeat(owner: str, *, path: Optional[Path] = None) -> bool:
    """Stamp ``heartbeat_at`` on the lock ``owner`` holds. True iff it was stamped.

    Never raises and never recreates a freed lock dir: a lock that was released,
    reclaimed or re-taken by another owner is left alone. Trap: the read-then-replace
    is not atomic, so a beat stalled past ``HEARTBEAT_STALE_SECS`` could overwrite a
    successor's meta; the owner check narrows that window but cannot close it.
    """
    try:
        path = lock_path() if path is None else path
        meta = _read_meta(path)
        if meta is None or meta["owner"] != owner:
            return False
        meta["heartbeat_at"] = _now_iso()
        _replace_meta(path, meta)
        return True
    except Exception as exc:
        _LOG.warning("suite_mutex: heartbeat for %s failed: %s", owner, exc)
        return False


def _age_secs(path: Path, meta: Optional[dict]) -> Optional[float]:
    """Return the lock's age in seconds, preferring ``started_at`` over dir mtime.

    Wall-clock rather than monotonic by necessity — the acquiring process is a
    different process from the reader, and monotonic clocks are not comparable
    across processes.
    """
    started = _parse_iso(meta.get("started_at")) if meta else None
    if started is None:
        started = _dir_mtime(path)
    if started is None:
        return None
    return max(0.0, time.time() - started)


def _reclaim(path: Path, reason: str, meta: Optional[dict]) -> None:
    """Remove a lock dir judged stale, logging why. Best-effort, never raises.

    A losing race against another reclaimer or a concurrent acquirer is
    benign: the removals are individually tolerant of already-gone paths, and
    whichever process next calls ``os.mkdir`` arbitrates the winner.
    """
    owner = (meta or {}).get("owner", _UNKNOWN_OWNER)
    pid = (meta or {}).get("pid", 0)
    _LOG.warning(
        "suite_mutex: reclaiming stale lock at %s (%s; owner=%s pid=%s)",
        path,
        reason,
        owner,
        pid,
    )
    try:
        (path / _META_FILENAME).unlink()
    except OSError:
        pass
    for stray in _iter_stray_tmp(path):
        try:
            stray.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


def _iter_stray_tmp(path: Path):
    """Yield leftover metadata temp files inside the lock dir. Never raises."""
    try:
        return [p for p in path.iterdir() if p.name.startswith(f".{_META_FILENAME}.")]
    except OSError:
        return []


def holder(*, path: Optional[Path] = None) -> Optional[dict]:
    """Return the current holder, or None when the mutex is free.

    The returned dict is ``{"pid": int, "owner": str, "started_at": str
    (ISO-8601 UTC), "cmd": str}``. ``owner`` is ``"<unknown>"`` and ``pid`` is
    ``0`` for a lock caught mid-acquire (dir created, metadata not yet
    written) inside the grace window — still a genuine holder, just not yet
    self-describing.

    Cheap (one stat plus at most one small read), never raises, and
    self-heals: a lock found stale by dead PID or by TTL is reclaimed here,
    before reporting, so a crashed run never wedges the fleet and a caller
    that polls ``holder()`` sees the truth rather than a ghost.
    """
    try:
        path = lock_path() if path is None else path
        if not path.is_dir():
            return None

        meta = _read_meta(path)
        age = _age_secs(path, meta)

        # A holder proven alive is never reclaimed on age: only a pid that was
        # recycled or a heartbeat that stopped marks it stale.
        holder_alive = _pid_alive(meta["pid"]) if meta is not None else None

        if age is not None and age > STALE_TTL_SECS and holder_alive is not True:
            _reclaim(path, f"age {age:.0f}s exceeds TTL {STALE_TTL_SECS:.0f}s", meta)
            return None

        if meta is not None and holder_alive is not False:
            if holder_alive is True and _pid_reused(meta["pid"], meta["started_at"]):
                _reclaim(path, f"pid {meta['pid']} was created after the lock was taken (reused)", meta)
                return None
            heartbeat = _parse_iso(meta.get("heartbeat_at"))
            if heartbeat is not None and time.time() - heartbeat > HEARTBEAT_STALE_SECS:
                _reclaim(path, f"heartbeat older than {HEARTBEAT_STALE_SECS:.0f}s", meta)
                return None

        if meta is None:
            if age is not None and age > _META_GRACE_SECS:
                _reclaim(path, "metadata absent or unreadable past grace window", None)
                return None
            return {"pid": 0, "owner": _UNKNOWN_OWNER, "started_at": "", "cmd": ""}

        if holder_alive is False:
            _reclaim(path, f"holder pid {meta['pid']} is not alive", meta)
            return None

        return meta
    except Exception as exc:
        _LOG.warning("suite_mutex: holder() swallowed an unexpected error: %s", exc)
        return None


def _try_mkdir(path: Path) -> bool:
    """Attempt the single atomic ``os.mkdir`` acquire. True iff this call won."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        os.mkdir(str(path))
    except FileExistsError:
        return False
    return True


def _holder_is_self_or_ancestor(holder_pid: int) -> bool:
    """True when ``holder_pid`` is this process or one of its ancestors.

    Same-owner re-entry is for a nested acquire inside the holder's own run (the
    wrapper holds on the child's pid; the runner it spawned re-acquires). Two
    sibling processes sharing one session-id owner are NOT nested: the second
    must wait. An unknown pid (0) is never an ancestor. When the process tree
    cannot be read the answer is True, keeping the owner-only behaviour.
    """
    if holder_pid <= 0:
        return False
    if holder_pid == os.getpid():
        return True
    try:
        import psutil

        return any(p.pid == holder_pid for p in psutil.Process().parents())
    except Exception as exc:
        _LOG.warning("suite_mutex: ancestry unreadable, treating same owner as nested: %s", exc)
        return True


def _queue_dir(path: Path) -> Path:
    """Ticket queue directory beside the lock dir at ``path``."""
    return path.parent / (path.name + _QUEUE_SUFFIX)


def _ticket_time_secs(name: str) -> float:
    """Arrival instant (epoch seconds) encoded in a ticket name."""
    return int(name.split("-", 1)[0]) / 1e9


def _read_ticket(qdir: Path, name: str) -> Optional[dict]:
    """Shape-validated ticket metadata, or None for an unreadable or partial file."""
    try:
        parsed = json.loads((qdir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        pid = int(parsed.get("pid"))
    except (TypeError, ValueError):
        return None
    owner = parsed.get("owner")
    created = parsed.get("created")
    handed = parsed.get("handed_at")
    return {
        "pid": pid,
        "owner": owner if isinstance(owner, str) else _UNKNOWN_OWNER,
        "created": created if isinstance(created, str) else "",
        "adoptable": parsed.get("adoptable") is True,
        "handed_at": handed if isinstance(handed, (int, float)) and not isinstance(handed, bool) else None,
    }


def _ticket_alive(name: str, meta: Optional[dict], now: float) -> bool:
    """Whether a ticket still holds its place; pid doubt (None) counts as alive."""
    age = max(0.0, now - _ticket_time_secs(name))
    if meta is None:
        # O_EXCL create and the body write are two steps; a reader can land between them.
        return age <= _META_GRACE_SECS
    if meta["adoptable"]:
        # The grace runs from the hand-off (`handed_at`), not the ticket's creation:
        # a `--wait` that queued longer than the grace would otherwise lose its place at once.
        handed_at = meta["handed_at"]
        grace_age = age if handed_at is None else max(0.0, now - handed_at)
        return grace_age <= _TICKET_ADOPT_GRACE_SECS
    alive = _pid_alive(meta["pid"])
    if alive is False:
        return False
    if alive is True:
        return not _pid_reused(meta["pid"], meta["created"])
    return age <= STALE_TTL_SECS


def _live_tickets(qdir: Path) -> list:
    """Live tickets as ``[(name, meta)]`` in arrival order, pruning dead ones. Never raises."""
    try:
        names = sorted(n for n in os.listdir(str(qdir)) if _TICKET_NAME_RE.match(n))
    except OSError:
        return []
    now = time.time()
    live = []
    for name in names:
        meta = _read_ticket(qdir, name)
        if _ticket_alive(name, meta, now):
            live.append((name, meta))
        else:
            _unlink_quiet(qdir / name)
    return live


def _write_ticket(qdir: Path, name: str, meta: dict, *, replace: bool) -> bool:
    body = json.dumps(meta, sort_keys=True) + "\n"
    try:
        if replace:
            tmp = qdir / f".{name}.{uuid.uuid4().hex}.tmp"
            tmp.write_text(body, encoding="utf-8", newline="\n")
            try:
                os.replace(str(tmp), str(qdir / name))
            except OSError:
                _unlink_quiet(tmp)
                raise
            return True
        fd = os.open(str(qdir / name), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        return True
    except FileExistsError:
        raise
    except OSError as exc:
        _LOG.warning("suite_mutex: could not write ticket %s: %s", name, exc)
        return False


def take_ticket(owner: str, *, adoptable: bool = False, pid: Optional[int] = None,
                path: Optional[Path] = None) -> Optional[str]:
    """Take a place at the back of the queue; returns the ticket name, or None on failure.

    ``pid`` defaults to the calling process and is the pid the ticket's liveness
    is judged on: the ACQUIRING process, never a runner child. An ``adoptable``
    ticket survives its writer's death for ``_TICKET_ADOPT_GRACE_SECS`` and is
    adopted by the next ``acquire()`` of the same ``owner``. Never raises.
    """
    qdir = _queue_dir(lock_path() if path is None else path)
    meta = {
        "pid": os.getpid() if pid is None else int(pid),
        "owner": owner,
        "created": _now_iso(),
        "adoptable": bool(adoptable),
    }
    try:
        qdir.mkdir(parents=True, exist_ok=True)
        for _ in range(_TICKET_CREATE_ATTEMPTS):
            name = "%020d-%d" % (time.time_ns(), meta["pid"])
            try:
                return name if _write_ticket(qdir, name, meta, replace=False) else None
            except FileExistsError:
                continue
    except OSError as exc:
        _LOG.warning("suite_mutex: could not take a ticket under %s: %s", qdir, exc)
    return None


def drop_ticket(name: str, *, path: Optional[Path] = None) -> None:
    """Remove a ticket; a missing one is fine. Never raises."""
    _unlink_quiet(_queue_dir(lock_path() if path is None else path) / name)


def _adopt_ticket(qdir: Path, live: list, owner: str) -> Optional[str]:
    """Claim the oldest ``adoptable`` ticket of ``owner`` for this process, keeping its place."""
    for name, meta in live:
        if meta is not None and meta["adoptable"] and meta["owner"] == owner:
            claimed = dict(meta, pid=os.getpid(), adoptable=False)
            if _write_ticket(qdir, name, claimed, replace=True):
                return name
    return None


def acquire(owner: str, cmd: str, timeout: float = 0.0, *, pid: Optional[int] = None,
            path: Optional[Path] = None) -> bool:
    """Acquire the machine-wide suite mutex for ``owner``. True iff held on return.

    ``timeout=0`` (the default) is non-blocking: one attempt, then False, and
    False whenever a live waiter is queued — it never jumps the queue.
    ``timeout>0`` takes a ticket and polls with exponential backoff up to that
    many seconds, going only when its ticket is the oldest live one; the ticket
    is dropped on acquire, timeout and failure. A releaser re-entering here
    takes a NEW ticket behind existing waiters. Failure is always a False
    return, never an exception — a guard consulting this must be able to deny
    cleanly.

    Re-entrant: if the lock is already held by this same ``owner``, this is a
    no-op success (True), not a deadlock and not a second acquisition. The
    matching ``release(owner)`` frees it — this mutex does not reference-count
    nested acquires, which is why re-entrancy is deliberately shallow.

    ``pid`` records who is judged for liveness. It defaults to
    ``os.getpid()``, correct when the acquiring process is the one that runs
    the suite. A short-lived acquirer that spawns the runner elsewhere MUST
    pass the runner's PID explicitly, or its lock will be reclaimed as stale
    the moment it exits — see the module docstring's PID-LIVENESS section.
    """
    effective_pid = os.getpid() if pid is None else int(pid)
    path = lock_path() if path is None else path
    qdir = _queue_dir(path)
    blocking = timeout > 0
    deadline = time.monotonic() + max(0.0, timeout)
    interval = _POLL_INITIAL_SECS
    reclaim_retries = 0
    ticket: Optional[str] = None

    try:
        while True:
            # Re-entry is decided before any ticket exists, so a nested acquire
            # never queues behind waiters that are themselves waiting on it.
            held_now = holder(path=path)
            if held_now is not None and held_now["owner"] == owner and _holder_is_self_or_ancestor(held_now["pid"]):
                return True

            live = _live_tickets(qdir)
            if ticket is not None and all(name != ticket for name, _ in live):
                ticket = None
            if ticket is None:
                ticket = _adopt_ticket(qdir, live, owner)
                if ticket is None and blocking:
                    ticket = take_ticket(owner, path=path)
                if ticket is not None:
                    live = _live_tickets(qdir)

            # A blocking caller goes only from the head of the queue; a
            # non-blocking one without a ticket may go only into an empty queue,
            # and never jumps ahead of a live waiter.
            if ticket is not None:
                may_try = bool(live) and live[0][0] == ticket
            else:
                may_try = not live

            if may_try:
                if _try_mkdir(path):
                    try:
                        _write_meta(path, pid=effective_pid, owner=owner, cmd=cmd)
                    except OSError as exc:
                        _LOG.warning(
                            "suite_mutex: acquired %s but could not stamp metadata — "
                            "releasing rather than holding an unattributable lock: %s",
                            path,
                            exc,
                        )
                        _reclaim(path, "metadata stamp failed immediately after acquire", None)
                        return False
                    return True

                current = holder(path=path)
                if current is None:
                    # The dir existed but resolved to no holder — it was just freed or
                    # reclaimed, so retry the mkdir immediately. Bounded, because a lock
                    # dir that cannot be removed (an undeletable stray inside it) would
                    # otherwise spin this loop hot forever, including at timeout=0.
                    reclaim_retries += 1
                    if reclaim_retries <= _RECLAIM_RETRY_LIMIT:
                        continue
                    _LOG.warning(
                        "suite_mutex: %s reports no holder but could not be acquired after "
                        "%d attempts — treating as held",
                        path,
                        reclaim_retries,
                    )
                    return False
                if current["owner"] == owner and _holder_is_self_or_ancestor(current["pid"]):
                    return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(interval, remaining))
            interval = min(interval * _POLL_BACKOFF, _POLL_MAX_SECS)
    finally:
        if ticket is not None:
            drop_ticket(ticket, path=path)


def release(owner: str, *, path: Optional[Path] = None) -> None:
    """Release the mutex if ``owner`` holds it; otherwise log and do nothing.

    A release by a non-owner — a stale caller whose lock was already reclaimed
    and re-acquired by someone else, or a plain bug — is a logged no-op and
    never an exception. One session must not be able to stomp another
    session's live lock, and a spurious release must not crash the releaser
    mid-teardown.
    """
    path = lock_path() if path is None else path
    if not path.is_dir():
        return

    meta = _read_meta(path)
    current_owner = (meta or {}).get("owner")
    if current_owner is not None and current_owner != owner:
        _LOG.warning(
            "suite_mutex: release(%s) ignored — lock at %s is held by %s",
            owner,
            path,
            current_owner,
        )
        return

    try:
        (path / _META_FILENAME).unlink()
    except OSError:
        pass
    for stray in _iter_stray_tmp(path):
        try:
            stray.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError as exc:
        _LOG.warning("suite_mutex: release(%s) could not remove %s: %s", owner, path, exc)


@contextmanager
def held(owner: str, cmd: str, timeout: float = 0.0, *, pid: Optional[int] = None,
         path: Optional[Path] = None) -> Iterator[bool]:
    """Context manager holding the mutex for the block; yields whether it was acquired.

    ``with held(owner, cmd) as got:`` yields True when the mutex was acquired
    and False when it was not — the caller decides whether a denied
    acquisition is fatal, since a guard wants to deny politely while a
    ceremony wants to abort. Release happens on normal exit and on exception
    alike, and only when this call actually acquired: a False yield releases
    nothing, so a failed acquire can never tear down the real holder's lock.
    """
    acquired = acquire(owner, cmd, timeout, pid=pid, path=path)
    stop = threading.Event()
    beat: Optional[threading.Thread] = None
    if acquired:
        touch_heartbeat(owner, path=path)

        def _beat() -> None:
            while not stop.wait(HEARTBEAT_INTERVAL_SECS):
                touch_heartbeat(owner, path=path)

        beat = threading.Thread(target=_beat, name="suite-mutex-heartbeat", daemon=True)
        beat.start()
    try:
        yield acquired
    finally:
        if beat is not None:
            stop.set()
            beat.join(timeout=5)
        if acquired:
            release(owner, path=path)

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

Design (mirrors the atomic-mkdir shape of
``coordinator_core/ops/ceremony/ceremony_lock.py`` — cite, not import; that
lock is keyed on a git worktree and delegates its liveness verdict to the
session registry, neither of which is meaningful for a machine-wide,
repo-agnostic lock whose holder is a bare test-runner process):

  - Lock directory: ``<settings-home>/claude-klabauter/test-suite-mutex.lock/``.
    ``os.mkdir`` is atomic on POSIX and on Windows — exactly one racing
    process observes success, every other observes ``FileExistsError``. Never
    check-then-write.
  - Holder metadata (``meta.json``) is written INSIDE the lock dir after the
    winning mkdir, atomically via a temp file + ``os.replace``.
  - Placement follows claude-klabauter's C11 durable-data-plane convention (CLAUDE.md
    § Durable-data plane): out-of-repo durable data lives under
    ``$(coordinator-settings-home)/claude-klabauter/``, never an ad-hoc ``~/``
    path and never inside a repo tree — a per-repo lock would not serialize
    the cross-repo case that causes the incident.

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

Staleness has two independent signals; either one alone marks the lock stale:
  1. Dead PID (see above).
  2. Hard TTL (``STALE_TTL_SECS``) — age-expired is stale regardless of PID,
     so a wedged-but-alive runner cannot hold the fleet hostage forever.

Negative-spec — what this lock is NOT:
    - NOT a per-repo lock. It is deliberately machine-global; two different
      repos' suites contend for the same lock because they contend for the
      same CPU, disk, and shared native-backed stores.
    - NOT a general-purpose task lock. Do not reuse it to serialize
      ceremonies, commits, pipeline writes, or embedding jobs — a
      long-running unrelated holder would block the very test runs this
      exists to admit one-at-a-time. Ceremony critical sections use
      ``coordinator_core/ops/ceremony/ceremony_lock.py``; single-file RMW
      uses ``coordinator_core/locked_write.py``.
    - NOT an authorization check. It answers "may this run start *now*",
      never "is this caller entitled to run a suite at all" — tier/grant
      enforcement is the ``bash_guards`` layer's job.
    - NOT a fairness queue. Waiters poll with backoff; acquisition order
      under contention is unspecified.
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

_POLL_INITIAL_SECS = 0.1
_POLL_MAX_SECS = 5.0
_POLL_BACKOFF = 1.5

_UNKNOWN_OWNER = "<unknown>"

# Cap on immediate mkdir retries after `holder()` reports the lock free — see
# the guarded branch in `acquire()`.
_RECLAIM_RETRY_LIMIT = 3


def lock_path() -> Path:
    """Return the machine-wide lock directory path under the settings home.

    Resolved fresh on every call (never cached at import time) so a test or a
    caller that repoints ``COORDINATOR_SETTINGS_HOME`` is honoured.
    """
    return settings_home() / "claude-klabauter" / _LOCK_DIRNAME


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

    Delegates to ``coordinator_core.session.core.pid_alive``, which is the
    engine's single cross-platform primitive (``os.kill(pid, 0)`` on POSIX,
    ``psutil.pid_exists`` on Windows where no ``kill(pid, 0)`` equivalent
    exists). None — the indeterminate verdict, raised there as
    ``MissingPsutilError`` on a Windows host with psutil absent — is
    deliberately distinct from False: an indeterminate read must leave the
    holder presumed alive (fail-closed-to-keep), leaving the TTL as the only
    staleness signal, rather than reporting every Windows holder dead and
    letting two suites run at once.
    """
    if pid <= 0:
        return None
    from coordinator_core.session.core import pid_alive as _engine_pid_alive

    try:
        return bool(_engine_pid_alive(pid))
    except Exception as exc:
        _LOG.warning(
            "suite_mutex: PID liveness indeterminate for pid %s — presuming alive "
            "(TTL remains the only staleness signal): %s",
            pid,
            exc,
        )
        return None


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
    return {
        "pid": pid_int,
        "owner": owner if isinstance(owner, str) and owner else _UNKNOWN_OWNER,
        "started_at": started_at if isinstance(started_at, str) and started_at else "",
        "cmd": cmd if isinstance(cmd, str) else "",
    }


def _write_meta(path: Path, *, pid: int, owner: str, cmd: str) -> None:
    """Atomically stamp holder metadata inside an already-acquired lock dir."""
    payload = {
        "pid": pid,
        "owner": owner,
        "started_at": _now_iso(),
        "cmd": cmd,
    }
    tmp_path = path / f".{_META_FILENAME}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp_path), str(path / _META_FILENAME))


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


def holder() -> Optional[dict]:
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
        path = lock_path()
        if not path.is_dir():
            return None

        meta = _read_meta(path)
        age = _age_secs(path, meta)

        if age is not None and age > STALE_TTL_SECS:
            _reclaim(path, f"age {age:.0f}s exceeds TTL {STALE_TTL_SECS:.0f}s", meta)
            return None

        if meta is None:
            if age is not None and age > _META_GRACE_SECS:
                _reclaim(path, "metadata absent or unreadable past grace window", None)
                return None
            return {"pid": 0, "owner": _UNKNOWN_OWNER, "started_at": "", "cmd": ""}

        if _pid_alive(meta["pid"]) is False:
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


def acquire(owner: str, cmd: str, timeout: float = 0.0, *, pid: Optional[int] = None) -> bool:
    """Acquire the machine-wide suite mutex for ``owner``. True iff held on return.

    ``timeout=0`` (the default) is non-blocking: one attempt, then False.
    ``timeout>0`` polls with exponential backoff up to that many seconds
    before giving up and returning False. Failure is always a False return,
    never an exception — a guard consulting this must be able to deny cleanly.

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
    path = lock_path()
    deadline = time.monotonic() + max(0.0, timeout)
    interval = _POLL_INITIAL_SECS
    reclaim_retries = 0

    while True:
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

        current = holder()
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
        if current["owner"] == owner:
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))
        interval = min(interval * _POLL_BACKOFF, _POLL_MAX_SECS)


def release(owner: str) -> None:
    """Release the mutex if ``owner`` holds it; otherwise log and do nothing.

    A release by a non-owner — a stale caller whose lock was already reclaimed
    and re-acquired by someone else, or a plain bug — is a logged no-op and
    never an exception. One session must not be able to stomp another
    session's live lock, and a spurious release must not crash the releaser
    mid-teardown.
    """
    path = lock_path()
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
def held(owner: str, cmd: str, timeout: float = 0.0, *, pid: Optional[int] = None) -> Iterator[bool]:
    """Context manager holding the mutex for the block; yields whether it was acquired.

    ``with held(owner, cmd) as got:`` yields True when the mutex was acquired
    and False when it was not — the caller decides whether a denied
    acquisition is fatal, since a guard wants to deny politely while a
    ceremony wants to abort. Release happens on normal exit and on exception
    alike, and only when this call actually acquired: a False yield releases
    nothing, so a failed acquire can never tear down the real holder's lock.
    """
    acquired = acquire(owner, cmd, timeout, pid=pid)
    try:
        yield acquired
    finally:
        if acquired:
            release(owner)

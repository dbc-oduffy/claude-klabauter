"""
coordinator_core.ops.ceremony.ceremony_lock — per-worktree ceremony critical-section lock.

Purpose: provide ``ceremony_lock()``, a context-manager mutex keyed on the
WORKTREE (git common dir), not on session or artifact identity, so that two
concurrent ``wsc_commit`` invocations targeting the SAME tree contest the SAME
lock. The lock spans a multi-step stage -> commit -> push critical section
(C6 holds it across a network push), so it needs holder-identity stamping and
dead-holder reclaim -- properties a plain flock does not provide.

Sibling precedent (cite, do NOT reuse): coordinator_core/locked_write.py
(flock-backed single-write RMW lock, docs/plans/2026-07-06-cross-process-rmw-
file-locking.md). Not reused here because locked_rmw guards one crash-safe
file write with a fixed 10s local-write timeout; this lock guards a
multi-step critical section that includes a network round-trip (fetch+push,
tens of seconds) and must recognise its own holder across re-entrant calls
within the same ceremony session.

Design
------
  - Lock directory: ``<git-common-dir>/coordinator-ceremony-locks/<name>/``.
    ``os.mkdir`` is atomic -- exactly one racing process observes success;
    all others observe FileExistsError.
  - On acquire, the holder's session-id is stamped into a ``holder`` file
    inside the lock dir.
  - Re-entrant: a second acquire by the SAME holder session-id succeeds
    idempotently (checks the stamped holder file before treating an existing
    lock dir as contention) -- this is resume-safety: a ceremony that halts
    mid-critical-section and resumes in the same session must not deadlock
    against its own prior lock.
  - Dead-holder reclaim: if the lock dir exists and is held by a DIFFERENT
    session, the liveness of that other session is checked via the bridge
    (see RAW-PID-LIVENESS below). A dead holder's lock dir is removed and
    reclaimed. A live holder causes polling-with-backoff up to ``timeout``.

RAW-PID-LIVENESS (AC10, load-bearing): the dead-holder check delegates the
liveness VERDICT to ``coordinator_core.liveness.cs_claim_holder_live``
(liveness.py:235) and MUST NOT read meta.json / stable_pid / last_activity
directly, and MUST NOT call ps / kill / psutil, nor re-derive the bash
verdict. Bash's ``_cs_claim_holder_live <cdir>`` reads ``<cdir>/session_id``
and calls ``_cs_session_live "$sid"`` -- a lookup keyed GLOBALLY by session
id via the session registry, independent of ``cdir`` itself (confirmed
against example-doctrine-repo's ``coordinator-session.sh`` ``_cs_session_dir``, which resolves
purely from ``sid``; example-doctrine-repo e34f2484, 2026-07-22). So any directory that carries the holder's sid in a
``session_id`` file satisfies the bridge's contract -- it need not be a real
per-artifact claim dir. This module reuses the lock's OWN real lock
directory (see ``_stamp_holder``/``_try_acquire``) as that carrier: the
holder file records the sid for re-entrant/ownership checks, and a sibling
``session_id`` file (identical content, written at the same acquire) is what
the bridge reads. A False/missing bridge result means the holder is dead and
the lock is reclaimable. This module owns the LOCK mechanism only; it never
invents its own liveness check.

Review: code-reviewer P0 Finding 1 -- the prior wiring built a synthetic,
never-created claim path (``<git-common-dir>/coordinator-sessions/<sid>/
claims/<artifact>/``) that the bridge could never read, so every holder --
live or dead -- resolved as dead and was reclaimed on the first poll,
defeating AC4 cross-session serialization. Fixed by pointing the bridge at
the lock's own real, already-populated directory instead of a path that was
pure string construction.

Spec backlink: chunk C4, docs/plans/2026-07-11-wsc-concurrent-tree-safety-hardening.md
"""

from __future__ import annotations
import sys

import logging
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from coordinator_core.liveness import cs_claim_holder_live

_LOG = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT_SECS: float = 75.0

_LOCK_ROOT_DIRNAME = "coordinator-ceremony-locks"
_HOLDER_FILENAME = "holder"
_SESSION_ID_FILENAME = "session_id"


class CeremonyLockTimeout(Exception):
    """Raised when a ceremony lock cannot be acquired within ``timeout`` seconds.

    Fail-closed: the caller never entered the critical section and holds
    nothing.
    """


def _lock_root(git_common_dir: Path) -> Path:
    """Return the parent directory that holds all named ceremony lock dirs."""
    return git_common_dir / _LOCK_ROOT_DIRNAME


def _lock_dir(git_common_dir: Path, name: str) -> Path:
    """Return the lock directory for the named critical section on this tree."""
    return _lock_root(git_common_dir) / name


def _read_holder(lock_path: Path) -> Optional[str]:
    """Return the stamped holder session-id, or None if unreadable/absent."""
    holder_file = lock_path / _HOLDER_FILENAME
    try:
        return holder_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        print(f"skip: _read_holder: return holder_file.read_text(encoding=\"utf-8\").strip() or None failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


def _stamp_holder(lock_path: Path, sid: str) -> None:
    """Write ``sid`` into the lock dir's holder file AND its session_id file
    (both best-effort atomic).

    The holder file is this module's own re-entrancy/ownership record. The
    session_id file exists solely so the lock dir ALSO satisfies
    cs_claim_holder_live's on-disk contract (``<claim_dir>/session_id`` ->
    bash reads it and delegates to the global session registry) -- see the
    module docstring's RAW-PID-LIVENESS section for why reusing the lock's
    own directory (rather than a separate synthetic claim path) is correct.
    """
    holder_file = lock_path / _HOLDER_FILENAME
    tmp_path = lock_path / f".{_HOLDER_FILENAME}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_text(sid, encoding="utf-8")
    os.replace(str(tmp_path), str(holder_file))

    session_id_file = lock_path / _SESSION_ID_FILENAME
    tmp_sid_path = lock_path / f".{_SESSION_ID_FILENAME}.{uuid.uuid4().hex}.tmp"
    tmp_sid_path.write_text(sid, encoding="utf-8")
    os.replace(str(tmp_sid_path), str(session_id_file))


def _is_holder_dead(lock_path: Path, holder_sid: str) -> bool:
    """Return True iff the recorded holder session is CONFIRMED no longer live.

    Delegates the verdict to cs_claim_holder_live, passing the lock's OWN
    directory -- populated with a session_id file at stamp time -- never a
    synthetic never-created path. Never inspects meta.json / stable_pid /
    last_activity, never calls ps / kill / psutil.

    Fail-closed-to-keep: cs_claim_holder_live now PROPAGATES exceptions on an
    indeterminate/errored liveness read rather than collapsing them to
    ``False`` (2026-07-21 fix -- see liveness.py's module docstring). An
    indeterminate read here must NOT be treated as "holder dead" -- doing so
    would reclaim a lock out from under a session that might still be alive
    mid-critical-section (stage -> commit -> push). On any exception this
    returns False (holder presumed alive); ``acquire()``'s poll loop then
    keeps waiting up to its own ``timeout`` and raises CeremonyLockTimeout if
    the read never resolves -- safe (nothing reclaimed), unlike a false
    "dead" verdict (a live holder's critical section reclaimed underneath it).
    """
    try:
        return not cs_claim_holder_live(str(lock_path))
    except Exception as exc:
        _LOG.warning(
            "ceremony_lock: cs_claim_holder_live raised for %s (holder %s) — "
            "treating as alive (fail-closed-to-keep): %s",
            lock_path, holder_sid, exc,
        )
        return False


def _try_acquire(lock_path: Path, sid: str) -> bool:
    """Attempt a single atomic mkdir-based acquire; stamp holder on success.

    Returns True on success, False if the lock dir already exists.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(str(lock_path))
    except FileExistsError:
        print(f"skip: _try_acquire: os.mkdir(str(lock_path)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    _stamp_holder(lock_path, sid)
    return True


def acquire(
    worktree_root: Path,
    *,
    name: str,
    session_id: str,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECS,
    git_common_dir: Optional[Path] = None,
) -> Path:
    """Acquire the named ceremony lock for ``worktree_root``, blocking up to ``timeout``.

    Re-entrant for the same ``session_id``: if the lock dir already exists
    and is stamped with this same session id, returns immediately without
    re-mkdir-ing (idempotent resume-safety -- a halted-and-resumed ceremony
    in the same session never deadlocks against its own prior acquire).

    Contention from a DIFFERENT session polls with backoff. Each poll first
    checks whether the current holder is dead (via the liveness bridge); a
    dead holder's lock dir is removed and reclaimed immediately rather than
    waiting out the remainder of ``timeout``.

    Args:
        worktree_root: any path inside the target git worktree.
        name: the critical-section name (lock dirs are namespaced by this).
        session_id: the identity to stamp as holder; also the identity
            checked for re-entrancy and reclaim.
        timeout: maximum seconds to wait for a live foreign holder to
            release. Sized for a network round-trip (fetch+push), default
            75s -- do NOT reuse locked_write.py's 10s single-write budget.
        git_common_dir: pre-resolved git common dir, if the caller already
            has it (avoids a redundant subprocess call). Resolved via
            coordinator_core.lifecycle.git_common_dir if omitted.

    Returns:
        The acquired lock directory path.

    Raises:
        CeremonyLockTimeout: a live foreign holder never released within
            ``timeout`` seconds. Fail-closed -- nothing was acquired.
    """
    if git_common_dir is None:
        from coordinator_core.lifecycle import git_common_dir as _resolve_common_dir
        try:
            git_common_dir = _resolve_common_dir(worktree_root)
        except RuntimeError:
            git_common_dir = worktree_root / ".git"

    lock_path = _lock_dir(git_common_dir, name)

    deadline = time.monotonic() + timeout
    interval = 0.05
    max_interval = 1.0

    while True:
        if _try_acquire(lock_path, session_id):
            return lock_path

        holder_sid = _read_holder(lock_path)
        if holder_sid == session_id:
            return lock_path

        if holder_sid is None or _is_holder_dead(lock_path, holder_sid):
            try:
                (lock_path / _HOLDER_FILENAME).unlink()
            except OSError:
                print(f"skip: acquire: (lock_path / _HOLDER_FILENAME).unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            try:
                (lock_path / _SESSION_ID_FILENAME).unlink()
            except OSError:
                print(f"skip: acquire: (lock_path / _SESSION_ID_FILENAME).unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            try:
                lock_path.rmdir()
            except OSError:
                print(f"skip: acquire: lock_path.rmdir() failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CeremonyLockTimeout(
                f"Could not acquire ceremony lock {name!r} on {worktree_root} "
                f"within {timeout}s (held by live session {holder_sid!r})"
            )
        time.sleep(min(interval, remaining))
        interval = min(interval * 1.5, max_interval)


def release(lock_path: Path, *, session_id: str) -> None:
    """Release a ceremony lock previously returned by acquire().

    Only removes the lock dir if it is still stamped with ``session_id`` --
    a mismatch (e.g. the lock was reclaimed as dead and re-acquired by a
    different session in the interim) is a no-op rather than an error, since
    ownership has already moved on.
    """
    holder_sid = _read_holder(lock_path)
    if holder_sid is not None and holder_sid != session_id:
        return
    try:
        (lock_path / _HOLDER_FILENAME).unlink()
    except OSError:
        print(f"skip: release: (lock_path / _HOLDER_FILENAME).unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass
    try:
        (lock_path / _SESSION_ID_FILENAME).unlink()
    except OSError:
        print(f"skip: release: (lock_path / _SESSION_ID_FILENAME).unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass
    try:
        lock_path.rmdir()
    except OSError:
        print(f"skip: release: lock_path.rmdir() failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass


@contextmanager
def ceremony_lock(
    worktree_root: Path,
    *,
    name: str,
    session_id: str,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECS,
    git_common_dir: Optional[Path] = None,
) -> Iterator[Path]:
    """Context manager: acquire the named ceremony lock for the duration of the block.

    ``with ceremony_lock(worktree_root, name="wsc-commit", session_id=sid):``
    guards the entire stage -> commit -> push critical section. Re-entrant
    for the same session_id -- nested/resumed use from the same ceremony
    session never deadlocks. See acquire()/release() for the full contract.
    """
    lock_path = acquire(
        worktree_root,
        name=name,
        session_id=session_id,
        timeout=timeout,
        git_common_dir=git_common_dir,
    )
    try:
        yield lock_path
    finally:
        release(lock_path, session_id=session_id)

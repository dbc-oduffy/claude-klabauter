"""
coordinator_core.session.day_branch_cut_lock — tree-keyed mutex serialising the
day-branch cut across concurrent sessions sharing one working tree.

Purpose: under the PM ruling of 2026-08-18 ("we cut automatically if we're on
main") every session boot on ``main`` wants to cut ``work/{machine}/{today}``.
50-70 concurrent LLM sessions is this machine's average (project CLAUDE.md
§ Load norm), so without serialisation N sessions race one ``git checkout -b``.
Exactly one session cuts; the rest INHERIT the winner's branch, which needs no
work from them because the tree is shared.

Keyed on the WORKTREE, never the session id — a session-keyed lock cannot
serialise sessions. Specifically keyed on
``coordinator_core.git.git_dir.resolve_git_common_dir(repo_root)``, with the
lock file placed INSIDE the common dir, exactly as
``coordinator_core/hooks/auto_push.py::_pending_record_path`` already does.

    Negative-spec — do NOT key this on a hashed path string. ``X:\\DoE-claude``,
    ``X:/DoE-claude``, a substituted-drive view and a UNC view all denote the
    same tree and hash differently, and sessions reach the tree by different
    routes (harness cwd, ``CLAUDE_PROJECT_DIR``, a ``-C`` argument). A hash
    mismatch fails OPEN: two sessions take two different locks, both "win",
    both cut. Letting git resolve the identity makes every route agree.

    Negative-spec — this is a TRANSIENT MUTEX, NOT A WORK-CLAIM. The PM ruling
    that a crashed holder releases its claim but never auto-archives (``DR-065``,
    amended by ``DR-084``) governs BATONS in ``coordinator_core/session/claims.py``
    — a four-predicate-gated ship-vs-abandon decision over long-lived work
    claims, deliberately conservative because discarding a baton discards real
    work. Cited here to be distinguished from, not assumed: this lock is a
    sub-second mutex over one ``git checkout -b``, and importing claim
    semantics would wedge a tree-wide invariant behind a dead process. The
    idiom actually REUSED is ``auto_push.py``'s pending record — ``holder_pid``
    plus ``hold_until``, takeover on CONFIRMED-DEAD holder OR
    ``_STALE_GRACE_SECONDS`` elapsed past ``hold_until``
    (``auto_push._record_is_stale``). PID-liveness gives immediate takeover on
    a crashed holder instead of every peer polling a corpse for the full grace
    window; age stays the ceiling, not the only signal.

    Negative-spec — Windows: open then CLOSE the lock file rather than holding
    the handle open. A held handle turns a reaper's or a takeover's unlink into
    a sharing violation.

Spec backlink: DoE-claude ``docs/plans/2026-08-18-enforce-day-branch-cut-tree-invariant.md``
chunk C2, delivered to this repo by cross-repo memo
``2026-08-18-doe-claude-em-day-branch-cut-tree-invariant-engine-work.md``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.session import core as session_core

_LOCK_NAME = "coordinator-day-branch-cut.json"

#: How long a holder asserts it will still be cutting. A local ``checkout -b``
#: is ~30ms; this is generous headroom for a loaded box, not an estimate of
#: the work.
_HOLD_WINDOW_SECONDS = 10.0

#: Grace past ``hold_until`` before a peer calls a still-live holder stale.
#: Mirrors ``auto_push._STALE_GRACE_SECONDS`` deliberately.
_STALE_GRACE_SECONDS = 60.0


class CutLockVerdict(NamedTuple):
    """Outcome of an acquire attempt.

    acquired: True iff THIS process holds the lock and must perform the cut.
    holder_pid / holder_sid: the incumbent when ``acquired`` is False (may be
      None when the record was unreadable).
    reason: operator-readable one-liner.
    """

    acquired: bool
    holder_pid: Optional[int]
    holder_sid: Optional[str]
    reason: str


def lock_path(repo_root: str | Path) -> Path:
    """``<git-common-dir>/coordinator-day-branch-cut.json``."""
    return resolve_git_common_dir(repo_root) / _LOCK_NAME


def read_record(repo_root: str | Path) -> Optional[dict]:
    """The lock record, or None if absent/corrupt/unreadable.

    A corrupt or partially-written record reads as None — exactly like "no
    lock" — rather than raising, mirroring ``auto_push._read_pending_record``.
    """
    try:
        text = lock_path(repo_root).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def holder_alive(pid) -> Optional[bool]:
    """True/False when PID liveness is known, None when it cannot be probed.

    Shared by ``day_branch_cut_lock`` and ``warm.push_cadence``'s sweep lock —
    the git-common-dir holder-liveness CHECK the two lock PROTOCOLS both
    wrap. Contract, stated deliberately: unknown liveness is ``None`` and
    NEVER a verdict (never coerced to True or False by a caller); a
    non-``int`` ``pid`` is unknown, and a probe that raises is unknown.
    """
    if not isinstance(pid, int):
        return None
    try:
        return session_core.pid_alive(pid)
    except Exception:  # noqa: BLE001 -- unknown liveness is not a verdict
        return None


def record_is_stale(record: dict, now: Optional[float] = None) -> bool:
    """Holder confirmed dead, OR ``hold_until`` more than the grace past.

    PID-dead is checked FIRST so a crashed holder is taken over immediately
    rather than every peer polling a corpse for the full grace window.
    """
    now = time.time() if now is None else now
    if holder_alive(record.get("holder_pid")) is False:
        return True
    hold_until = record.get("hold_until")
    return isinstance(hold_until, (int, float)) and now > hold_until + _STALE_GRACE_SECONDS


def _try_create(path: Path, payload: dict) -> bool:
    """Atomically create the lock file, or return False if it already exists.

    ``O_CREAT | O_EXCL`` is the filesystem atomicity primitive this module's
    whole guarantee rests on — exactly one racer's create succeeds. The handle
    is closed immediately (Windows sharing-violation negative-spec above).
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)
    return True


def acquire(
    repo_root: str | Path,
    *,
    session_id: str = "",
    now: Optional[float] = None,
    pid: Optional[int] = None,
) -> CutLockVerdict:
    """Try to become the one session that cuts.

    Returns ``acquired=True`` when this process created the lock (or took over
    a stale one). Returns ``acquired=False`` naming the incumbent otherwise —
    a loser must NEVER attempt an unserialised cut of its own; see
    ``coordinator/lib/session_ensure_branch.py``'s INHERITED path for the
    poll-and-inherit behaviour a loser runs instead.
    """
    now = time.time() if now is None else now
    pid = os.getpid() if pid is None else pid
    path = lock_path(repo_root)
    payload = {
        "holder_pid": pid,
        "holder_sid": session_id,
        "hold_until": now + _HOLD_WINDOW_SECONDS,
    }

    if _try_create(path, payload):
        return CutLockVerdict(True, pid, session_id, "acquired")

    record = read_record(repo_root)
    if record is None or record_is_stale(record, now):
        # Take over: unlink then re-create under O_EXCL. Exactly one racer's
        # unlink-plus-create wins; the rest see the winner's fresh record.
        try:
            path.unlink()
        except OSError:
            pass
        if _try_create(path, payload):
            return CutLockVerdict(True, pid, session_id, "acquired (stale holder taken over)")
        record = read_record(repo_root)

    holder_pid = record.get("holder_pid") if isinstance(record, dict) else None
    holder_sid = record.get("holder_sid") if isinstance(record, dict) else None
    return CutLockVerdict(
        False,
        holder_pid if isinstance(holder_pid, int) else None,
        holder_sid if isinstance(holder_sid, str) and holder_sid else None,
        f"cut lock held by pid={holder_pid} sid={holder_sid or 'unknown'}",
    )


def release(repo_root: str | Path, *, pid: Optional[int] = None) -> bool:
    """Drop the lock if this process holds it. Never raises.

    A foreign-held record is left alone: releasing someone else's mutex is how
    two sessions end up cutting.
    """
    pid = os.getpid() if pid is None else pid
    record = read_record(repo_root)
    if record is not None and record.get("holder_pid") != pid:
        return False
    try:
        lock_path(repo_root).unlink()
    except OSError:
        return False
    return True

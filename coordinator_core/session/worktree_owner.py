"""Owner verdict for a git worktree, read from the platform's `locked` reason.

The Claude Code platform locks every agent worktree it creates with the reason
`claude agent <name> (pid N[ start S])` (or `claude session ...`). This module
turns `(locked, lock_reason)` into one of four verdicts:

- `unattributed`: not locked; no owner evidence, the caller keeps its own gate.
- `foreign`: locked, reason is not platform-shaped (or the predicate failed);
  never reapable.
- `live`: platform-shaped and the pid's process exists (and, when the reason
  carries `start`, its create time matches); never reapable.
- `dead`: platform-shaped and the pid has no process, or the process's create
  time does not match `start` (a recycled pid); the only reapable verdict.

Invariant: the registry session id is attribution only and never decides
liveness. No subprocess is spawned; one psutil lookup and at most one registry
file read per call. Any exception fails closed to `foreign`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import psutil

from coordinator_core.session import harness_registry
from coordinator_core.session.core import _STABLE_PID_EPOCH_TOLERANCE_SECS

# Private import by contract: harness_registry has no public alias for the
# procStart converter, and the lock reason's `start` is the same raw value.
from coordinator_core.session.harness_registry import _proc_start_to_epoch

_PLATFORM_REASON = re.compile(
    r"^claude (?:agent|session) .{1,255} \(pid (\d{1,10})(?: start (.{1,255}))?\)$"
)


@dataclass(frozen=True)
class WorktreeOwner:
    verdict: str  # "live" | "dead" | "unattributed" | "foreign"
    pid: int | None  # from the lock reason; None unless platform-shaped
    session_id: str | None  # registry record for pid whose start matches; attribution only
    basis: str  # one-line reason, folded into the sweep's detail string


def worktree_owner(locked: bool, lock_reason: str) -> WorktreeOwner:
    """Classify a worktree's owner; never raises, fails closed to `foreign`."""
    try:
        return _classify(locked, lock_reason)
    except Exception as exc:
        return WorktreeOwner("foreign", None, None, f"owner predicate failed: {exc!r}")


def _classify(locked: bool, lock_reason: str) -> WorktreeOwner:
    if not locked:
        return WorktreeOwner("unattributed", None, None, "worktree is not locked")
    match = _PLATFORM_REASON.match(lock_reason or "")
    if match is None:
        return WorktreeOwner("foreign", None, None, "lock reason is not platform-shaped")
    pid = int(match.group(1))
    start_raw = match.group(2)

    try:
        create_time = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return WorktreeOwner("dead", pid, None, f"pid {pid} has no process")

    if start_raw is not None:
        start_epoch = _proc_start_to_epoch(start_raw)
        if start_epoch is None:
            return WorktreeOwner(
                "foreign", pid, None, f"lock reason start {start_raw!r} is unparseable"
            )
        if abs(create_time - start_epoch) > _STABLE_PID_EPOCH_TOLERANCE_SECS:
            return WorktreeOwner(
                "dead", pid, None, f"pid {pid} was recycled (create time differs from start)"
            )

    return WorktreeOwner("live", pid, _attributed_session(pid, create_time), f"pid {pid} is running")


def _attributed_session(pid: int, create_time: float) -> str | None:
    found = harness_registry.record_for_pid(pid)
    if found is None:
        return None
    session_id, record = found
    if abs(record.start_epoch - create_time) > _STABLE_PID_EPOCH_TOLERANCE_SECS:
        return None
    return session_id

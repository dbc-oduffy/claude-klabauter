"""Cross-process admission gate for a landing's worktree byte reads.

Pool workers are processes, so several landings over one dirty tree each read and hash the same
bytes at once and saturate the disk. `admit(root)` holds one advisory lock per repo for the span of
a landing; a second landing waits OUTSIDE `land_deadline.scope`, so its queue wait never counts
against the land budget, and times out as a typed `LandAdmissionTimeout` before any tree read.

Invariants: the lock is `locked_write.held_lock` (cross-process, released by the OS on holder death),
never a threading lock. It is keyed on the git common dir resolved at call time, so every worktree
of one repo shares it. `admit` is re-entrant within one context: a nested landing of the same repo
is a no-op, since `held_lock` itself deadlocks on a same-process re-acquire. It is also re-entrant
across threads and processes for the same non-empty holder label (the run id): a contended gate
whose recorded holder is that label and a live pid admits at once without taking or releasing the
lock. With no caller wait, a contended gate is queued behind for as long as the recorded holder's
pid is alive (the dispatcher clamp still bounds it); an unreadable record gets DEFAULT_WAIT_SECS.
An explicit wait is a fixed bound. Acquisition is try-once first, so a dead holder's OS-freed
lock is taken immediately.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Optional

from coordinator_core import dispatch_abandon
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.locked_write import LockTimeout, held_lock

#: Admission wait when neither the caller nor a bound dispatch deadline names one.
DEFAULT_WAIT_SECS = 20.0

_LOCK_NAME = "land-byte-gate"

#: Slice of the wait after which a contended gate's holder record is re-read: a same-run holder
#: writes its label just after taking the lock, so the first read can miss it.
_RECHECK_SECS = 0.25

#: `(common_dir_key, admitted_at)` of the landing holding the gate in this context; None = not held.
_HELD: ContextVar[Optional[tuple]] = ContextVar("land_byte_gate_held", default=None)


class LandAdmissionTimeout(Exception):
    """The byte gate was not acquired inside the wait; no tree read happened."""

    def __init__(self, root: Path, waited_secs: float, detail: str = "") -> None:
        super().__init__(
            f"land byte gate for {root} not acquired within {waited_secs:.1f}s"
            + (f" ({detail})" if detail else "")
        )
        self.root = root
        self.waited_secs = waited_secs


def _wait_budget(wait_secs: Optional[float]) -> float:
    """The caller's wait (or the default), clamped to the dispatcher's remaining time less its
    reply margin."""
    wait = DEFAULT_WAIT_SECS if wait_secs is None else float(wait_secs)
    return _clamp_to_dispatcher(wait)


def _clamp_to_dispatcher(wait: float) -> float:
    left = dispatch_abandon.remaining()
    if left is not None:
        wait = min(wait, max(0.0, left - dispatch_abandon.REPLY_MARGIN_SECS))
    return max(0.0, wait)


def _holder_alive(busy: LockTimeout) -> bool:
    """True when the contended gate's recorded holder names a live pid."""
    record = busy.holder
    if not isinstance(record, dict):
        return False
    from coordinator_core.session import core

    return bool(core.pid_alive(record.get("pid")))


def _held_by_own_run(busy: LockTimeout, holder_label: str) -> bool:
    """True when the contended gate's recorded holder is this run id and its process is alive.

    An empty label never matches. The OS frees a dead holder's lock, so a recorded dead pid
    here is stale metadata and never a same-holder admit."""
    record = busy.holder
    if not holder_label or not isinstance(record, dict) or record.get("holder") != holder_label:
        return False
    from coordinator_core.session import core

    return bool(core.pid_alive(record.get("pid")))


@contextmanager
def admit(
    root: Path,
    wait_secs: Optional[float] = None,
    *,
    holder_label: str = "",
    anchor_root: Optional[Path] = None,
) -> Iterator[float]:
    """Hold the repo's byte gate for the block, yielding the `time.monotonic()` admission instant.

    Raises `LandAdmissionTimeout` when the gate is not acquired inside the wait, before the block
    runs. `anchor_root` is the lock-namespace opt-out `held_lock` offers the test suite; production
    callers omit it."""
    common = Path(resolve_git_common_dir(root))
    key = str(common)
    held = _HELD.get()
    if held is not None and held[0] == key:
        yield held[1]
        return
    # No caller wait: queue behind a live holder for as long as it lives (the dispatcher clamp still
    # bounds it); an unreadable holder record gets the default grace.
    follow_holder = wait_secs is None
    wait = _wait_budget(wait_secs)
    started = time.monotonic()
    unreadable_since: Optional[float] = None
    target = common / _LOCK_NAME
    own_run = False
    with ExitStack() as stack:
        try:
            try:
                stack.enter_context(
                    held_lock(target, anchor_root=anchor_root, timeout=0, holder_label=holder_label)
                )
            except LockTimeout as busy:
                latest = busy
                own_run = _held_by_own_run(busy, holder_label)
                while not own_run:
                    if follow_holder:
                        now = time.monotonic()
                        if _holder_alive(latest):
                            unreadable_since = None
                            left = _clamp_to_dispatcher(float("inf"))
                        else:
                            unreadable_since = now if unreadable_since is None else unreadable_since
                            left = _clamp_to_dispatcher(DEFAULT_WAIT_SECS - (now - unreadable_since))
                    else:
                        left = max(0.0, wait - (time.monotonic() - started))
                    try:
                        stack.enter_context(
                            held_lock(
                                target, anchor_root=anchor_root,
                                timeout=min(left, _RECHECK_SECS), holder_label=holder_label,
                            )
                        )
                        break
                    except LockTimeout as again:
                        if left <= _RECHECK_SECS:
                            raise
                        latest = again
                        own_run = _held_by_own_run(again, holder_label)
        except LockTimeout as exc:
            raise LandAdmissionTimeout(Path(root), time.monotonic() - started, str(exc)) from None
        if own_run:
            yield time.monotonic()
            return
        admitted_at = time.monotonic()
        token = _HELD.set((key, admitted_at))
        try:
            yield admitted_at
        finally:
            _HELD.reset(token)

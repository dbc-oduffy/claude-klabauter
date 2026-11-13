"""Cross-process admission gate for a landing's worktree byte reads.

Pool workers are processes, so several landings over one dirty tree each read and hash the same
bytes at once and saturate the disk. `admit(root)` holds one advisory lock per repo for the span of
a landing; a second landing waits OUTSIDE `land_deadline.scope`, so its queue wait never counts
against the land budget, and times out as a typed `LandAdmissionTimeout` before any tree read.

Invariants: the lock is `locked_write.held_lock` (cross-process, released by the OS on holder death),
never a threading lock. It is keyed on the git common dir resolved at call time, so every worktree
of one repo shares it. `admit` is re-entrant within one context: a nested landing of the same repo
is a no-op, since `held_lock` itself deadlocks on a same-process re-acquire.
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
    left = dispatch_abandon.remaining()
    if left is not None:
        wait = min(wait, max(0.0, left - dispatch_abandon.REPLY_MARGIN_SECS))
    return max(0.0, wait)


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
    wait = _wait_budget(wait_secs)
    started = time.monotonic()
    with ExitStack() as stack:
        try:
            stack.enter_context(
                held_lock(common / _LOCK_NAME, anchor_root=anchor_root, timeout=wait, holder_label=holder_label)
            )
        except LockTimeout as exc:
            raise LandAdmissionTimeout(Path(root), time.monotonic() - started, str(exc)) from None
        admitted_at = time.monotonic()
        token = _HELD.set((key, admitted_at))
        try:
            yield admitted_at
        finally:
            _HELD.reset(token)

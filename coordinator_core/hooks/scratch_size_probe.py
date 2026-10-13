"""Bounded, in-process size read of the scratch trees for the session-start banner.

One iterative no-follow ``os.scandir`` pass bounded by an entry count and, as backstop, a
deadline in walking-thread CPU time. It spawns nothing, keeps no path list and never raises;
a walk that reaches a bound returns the counts read so far with ``finished=False``. Sole
importer: ``project_orientation``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from coordinator_core.install.junction import is_junction

SCRATCH_WARN_BYTES = 500 * 1024 * 1024
WALK_ENTRY_LIMIT = 4000
WALK_ALLOWANCE_SECS = 0.1
DEADLINE_STRIDE_ENTRIES = 256


@dataclass(frozen=True)
class ScratchRead:
    """``finished`` is False when a bound stopped the walk; counts are then partial.

    ``over_entries`` marks the entry-count stop; a deadline stop leaves it False.
    """

    finished: bool
    entries: int
    bytes: int
    over_entries: bool = False


def _entry_is_junction(e: os.DirEntry) -> bool:
    probe = getattr(e, "is_junction", None)
    return probe() if probe is not None else is_junction(e.path)


def read_scratch(
    roots: Sequence[str | os.PathLike[str]], *, clock: Callable[[], float] = time.thread_time
) -> ScratchRead:
    """Total entries and regular-file bytes under ``roots``, read in order under one deadline.

    A root that is missing is skipped; a root that is itself a link is read through. Links
    found under a root are skipped, never followed. ``clock`` is called for the deadline and
    for each deadline test only. ``WALK_ENTRY_LIMIT`` is read at call time and tested before
    each entry is counted, one count across all roots.
    """
    deadline = clock() + WALK_ALLOWANCE_SECS
    entries = 0
    total = 0
    for root in roots:
        stack = [os.fspath(root)]
        while stack:
            if clock() >= deadline:
                return ScratchRead(False, entries, total)
            top = stack.pop()
            try:
                it = os.scandir(top)
            except (OSError, ValueError):
                continue
            seen = 0
            with it:
                while True:
                    try:
                        e = next(it)
                    except StopIteration:
                        break
                    except OSError:
                        break
                    seen += 1
                    if seen % DEADLINE_STRIDE_ENTRIES == 0 and clock() >= deadline:
                        return ScratchRead(False, entries, total)
                    try:
                        if e.is_symlink() or _entry_is_junction(e):
                            continue
                        if entries >= WALK_ENTRY_LIMIT:
                            return ScratchRead(False, entries, total, True)
                        entries += 1
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
    return ScratchRead(True, entries, total)

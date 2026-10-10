"""Landing marker: lets a publish writer hold engine readers out of its swap window.

Imported by coordinator_core/__init__.py on every hook start: stdlib only, no coordinator_core
import. The marker lives at <engine_root>/.git/coordinator-engine-landing and carries one field,
the epoch deadline after which it reads as stale.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

MARKER_NAME = "coordinator-engine-landing"

# A reader past the gate when the marker rises finishes inside this before the writer swaps.
SWAP_GRACE_S: float = 0.3

# Reader's max park. Held under the DR-344 500ms bar; the swap itself is renames, so a park
# that outlives this means the writer is wedged, and the reader proceeds rather than stall.
WAIT_BUDGET_S: float = 0.45

_POLL_S = 0.02

# One overrun report per process: a hook that polls repeatedly must not repeat the line.
_reported_overrun = False


def marker_path(engine_root: Path) -> Optional[Path]:
    """Marker location for an engine root, or None when <root>/.git is not a directory."""
    git_dir = Path(engine_root) / ".git"
    if not git_dir.is_dir():
        return None
    return git_dir / MARKER_NAME


def begin_swap(engine_root: Path, *, deadline_s: float) -> Optional[Path]:
    """Raise the marker (tmp + os.replace) with a deadline deadline_s from now; None if unguardable."""
    target = marker_path(engine_root)
    if target is None:
        return None
    tmp = target.with_name(f"{MARKER_NAME}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"deadline_epoch": time.time() + deadline_s}), encoding="utf-8")
    os.replace(tmp, target)
    return target


def end_swap(engine_root: Path) -> None:
    """Lower the marker; a missing marker is not an error."""
    target = marker_path(engine_root)
    if target is None:
        return
    for attempt in range(10):
        try:
            target.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            # Windows: a reader holding the marker open (no FILE_SHARE_DELETE) blocks the unlink.
            if attempt == 9:
                raise
            time.sleep(_POLL_S)


def swap_in_progress(engine_root: Path) -> bool:
    """True while a live marker exists; a missing, unreadable, or past-deadline marker reads False."""
    path = os.path.join(os.fspath(engine_root), ".git", MARKER_NAME)
    # The miss path is the hook hot path: one stat, no open. The contract tests pin this.
    try:
        os.stat(path)
    except OSError:
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            deadline = float(json.load(fh)["deadline_epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return time.time() < deadline


def wait_until_clear(
    engine_root: Path,
    *,
    budget_s: float = WAIT_BUDGET_S,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Stat-poll until no swap is live; False (reported on stderr) when budget_s runs out."""
    global _reported_overrun
    start = time.monotonic()
    while swap_in_progress(engine_root):
        if time.monotonic() - start >= budget_s:
            if _reported_overrun:
                return False
            _reported_overrun = True
            print(
                f"engine landing marker still live after {budget_s}s at {engine_root}; "
                "proceeding. Remove .git/" + MARKER_NAME + " if no publish is running.",
                file=sys.stderr,
            )
            return False
        sleep(_POLL_S)
    return True

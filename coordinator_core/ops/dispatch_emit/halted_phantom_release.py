"""Which halted holders are phantom: owner session gone and no hunk of their own left to hold?

`phantom_holders(root, run_ids, withheld_by_run, dirty, attributable)` returns the run ids H for
which both hold:

1. H's emission receipt names an owner session (`halted_run.recorded_emitter`) and
   `session.liveness.session_live` reads it not live. No receipt is an unknown owner, not a dead one.
2. H withholds at least one path, and `halted_hunk_attribution.reader`'s `attributable` holds none
   of the withheld paths that are dirty. A clean withheld path carries no hunk.

Invariants: liveness is checked first, so `dirty()` is never called when no holder has a dead
owner; no git spawn of its own (`dirty` is the caller's shared reader); never raises -- any error
returns the empty set. It records nothing; the caller records the release.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def phantom_holders(
    root: Path,
    run_ids: Iterable[str],
    withheld_by_run: dict,
    dirty: Callable[[], set],
    attributable: Callable[[str, Iterable[str]], set],
) -> set:
    """The subset of `run_ids` that is phantom per the module docstring; empty on any error."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.session.liveness import session_live

        dead = []
        for run_id in run_ids:
            owner = halted_run.recorded_emitter(Path(root), run_id)
            if owner is None or session_live(owner):
                continue
            dead.append(run_id)
        if not dead:
            return set()
        dirt = {_norm(p) for p in dirty()}
        phantom = set()
        for run_id in dead:
            withheld = {_norm(p) for p in withheld_by_run.get(run_id, ()) if p}
            withheld.discard("")
            if not withheld:
                continue
            hot = withheld & dirt
            if not hot or not attributable(run_id, hot):
                phantom.add(run_id)
        return phantom
    except Exception:  # noqa: BLE001 -- unreadable evidence holds
        return set()

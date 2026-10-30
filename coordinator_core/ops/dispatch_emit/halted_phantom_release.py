"""Which halted holders are phantom: the session that owned the run is gone?

`phantom_holders(root, run_ids, withheld_by_run)` returns the run ids H for which both hold:

1. H's emission receipt names an owner session (`halted_run.recorded_emitter`) and
   `session.liveness.session_live` reads it not live. No receipt is an unknown owner, not a dead one.
2. H withholds at least one path.

A dead owner releases regardless of any hunk it left on a withheld path: a claim whose claimant is
not live never withholds a commit. Invariants: no git spawn and no dirty read; never raises -- any
error returns the empty set. It records nothing; the caller records the release.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


def phantom_holders(root: Path, run_ids: Iterable[str], withheld_by_run: dict) -> set:
    """The subset of `run_ids` that is phantom per the module docstring; empty on any error."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.session.liveness import session_live

        phantom = set()
        for run_id in run_ids:
            if not any(p for p in withheld_by_run.get(run_id, ())):
                continue
            owner = halted_run.recorded_emitter(Path(root), run_id)
            if owner is None or session_live(owner):
                continue
            phantom.add(run_id)
        return phantom
    except Exception:  # noqa: BLE001 -- unreadable evidence holds
        return set()

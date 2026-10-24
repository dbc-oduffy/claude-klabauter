"""Up-front refusal for a landing whose declared path is withheld by an unreleased halted run."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

REFUSED_COHOLD = "cohold-declared-path"


def refusal(held: dict, run_id: str, root: Path) -> Optional[str]:
    """Register message naming each held path, its holder run id(s) and their release calls; None when `held` is empty.

    `held` maps a declared path to the halted run ids holding it. `run_id` is the run now committing.
    """
    if not held:
        return None
    from coordinator_core.ops.dispatch_emit import halted_hold_release

    parts = []
    for path in sorted(held):
        for holder in sorted(set(held[path])):
            calls = " | ".join(halted_hold_release.release_calls(root, holder))
            parts.append(f"`{path}` co-held by halted run {holder}; release: {calls}")
    return f"run {run_id} declares paths held elsewhere: " + "; ".join(parts)

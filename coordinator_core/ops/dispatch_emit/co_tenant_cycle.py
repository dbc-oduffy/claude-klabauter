"""Co-tenant need cycles: runs that each wait on a path another of them owns, so none can ever land.

An edge X -> Y reads "X cannot land until Y does". It exists when X's `stage_wait` record lists Y in
`blockers`, or when X is a halted co-tenant run (`halted` starting `co_tenant`) and `owner_of` names Y
the holder of one of X's own paths. A halted marker persists no owner or peer path (its detail is prose
and is never parsed), so the halted edge is derived from the paths X itself withholds or declares: a path
both X and Y hold is the contention that halted X. `cycle_through` walks from the owner the needer is
blocked on looking for a way back to the needer; `joint_remedy` renders the one call that lands the
cycle's runs together. In-process reads only: no spawn, no git.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

CAUSE_CYCLE = "co_tenant_cycle"
_CO_TENANT_PREFIX = "co_tenant"


def _halted_co_tenant(root: Path, run_id: str) -> bool:
    try:
        marker = halted_run.read_strict(record_dir(root, run_id))
    except (ValueError, OSError):
        return False
    if not marker or marker.get("status") != halted_run.STATUS_HALTED:
        return False
    return str(marker.get("halted") or "").startswith(_CO_TENANT_PREFIX)


def needs(root: Path, run_id: str) -> tuple:
    """The runs `run_id` is waiting on, sorted."""
    from coordinator_core.ops.dispatch_emit import co_tenant_owner

    out = {b for b in ((stage_wait.read(root, run_id) or {}).get("blockers") or ()) if isinstance(b, str) and b}
    if _halted_co_tenant(root, run_id):
        entry = halted_withheld_index.entries(root).get(run_id)
        for path in (*entry.paths, *entry.declared) if entry is not None else ():
            found = co_tenant_owner.owner_of(root, path, exclude_run_id=run_id)
            if found["run_id"]:
                out.add(found["run_id"])
    out.discard(run_id)
    return tuple(sorted(out))


def cycle_through(root: Path, needer: str, owner: str) -> Optional[tuple]:
    """The need cycle `(needer, owner, ...)` that closes back on `needer`, or None.

    The caller has established `needer -> owner`; the walk follows `owner`'s needs, depth first over
    sorted edges, visiting each run once (so it ends on any graph, bounded by the run count). The
    first path found back to `needer` is returned with `needer` first and not repeated."""
    root = Path(root)
    if not needer or not owner or needer == owner:
        return None
    seen = {needer, owner}
    stack: list = [((needer, owner), iter(needs(root, owner)))]
    while stack:
        path, edges = stack[-1]
        for nxt in edges:
            if nxt == needer:
                return path
            if nxt not in seen:
                seen.add(nxt)
                stack.append(((*path, nxt), iter(needs(root, nxt))))
                break
        else:
            stack.pop()
    return None


def joint_remedy(cycle) -> str:
    """The `coordinator-invoke dispatch.land_run` call that lands every run of `cycle` as one joint landing."""
    return "coordinator-invoke dispatch.land_run '" + json.dumps({"joint": list(cycle)}) + "'"

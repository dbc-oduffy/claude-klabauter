"""Which run owns a peer-dirty path a run's clean export needs, and what the needing run does about it.

`owner_of` reads the live-run index (fallback `live_run_writes.live_footprints`) and the halted-withheld
index; `resolve` parks the needing run behind a live owner through `stage_wait.park` (the owner's
`run_token.consume`/`discard` wakes it) or returns the typed cause for a halted or unattributable owner.
In-process reads only: no spawn, no poll.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index, live_run_index, live_run_writes, run_lineage, run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

PARKED = "co-tenant-wait"
CAUSE_TYPED = "co_tenant_dependency"
CAUSE_HALTED_OWNER = "co_tenant_dependency_on_halted_run"
_INDEX_DEADLINE_MS = 150.0


def _norm(path: str) -> str:
    return live_run_writes._norm_s(path)


def _live_footprints(root: Path, exclude_run_id: str) -> Mapping[str, Optional[frozenset]]:
    index = live_run_index.read(root, _INDEX_DEADLINE_MS, exclude_run_id=exclude_run_id)
    if index is live_run_index.MISSING or index is live_run_index.STALLED:
        return live_run_writes.live_footprints(root, exclude_run_id=exclude_run_id)
    return index


def _halted_marker(root: Path, run_id: str) -> Optional[dict]:
    try:
        marker = halted_run.read_strict(record_dir(root, run_id))
    except (ValueError, OSError):
        return None
    return marker if marker and marker.get("status") == halted_run.STATUS_HALTED else None


def _names(path: str, held: tuple) -> bool:
    """True when `held` (repo-relative paths) holds `path` itself or a directory containing it."""
    return bool(live_run_writes.overlaps([path], {"_": frozenset(held)}))


def owner_of(root: Path, path: str, *, exclude_run_id: str) -> dict:
    """`{run_id, state}` of the run holding `path` uncommitted: state "live" (a run with a live token whose
    staged footprint names it), else "halted" (a halted record whose withheld or declared paths name it;
    the most recently halted), else `{run_id: None, state: None}`. `exclude_run_id`, its chain kin and a live
    run already parked behind `exclude_run_id` (a two-run wait cycle that only the deadline would break)
    are never the owner."""
    root = Path(root)
    path = _norm(path)
    live = live_run_writes.overlaps([path], _live_footprints(root, exclude_run_id))
    candidates = sorted(
        rid for rid in live
        if rid != exclude_run_id and run_token.is_live(root, rid)
        and exclude_run_id not in ((stage_wait.read(root, rid) or {}).get("blockers") or ())
    )
    halted: dict[str, int] = {}
    for rid, entry in halted_withheld_index.entries(root).items():
        if rid != exclude_run_id and rid not in live and _names(path, (*entry.paths, *entry.declared)):
            halted[rid] = entry.at_ns
    kin = run_lineage.chain_kin(root, exclude_run_id, [*candidates, *halted])
    for rid in candidates:
        if rid not in kin:
            return {"run_id": rid, "state": "live"}
    for rid in sorted(halted, key=lambda r: (halted[r], r), reverse=True):
        if rid not in kin and _halted_marker(root, rid) is not None:
            return {"run_id": rid, "state": "halted"}
    return {"run_id": None, "state": None}


def _park_behind(root: Path, run_id: str, owner: str, exc) -> bool:
    record = run_token._read_rec(root, run_id) or {}
    parked = stage_wait.park(
        root, run_id, [owner], [exc.peer],
        session_id=record.get("session_id") or "", sizing_rel=record.get("sizing_rel") or "", reason=PARKED,
    )
    if parked is None:
        return False
    if not run_token.is_live(root, owner):
        stage_wait.clear(root, run_id)
        return False
    return True


def resolve(root: Path, run_id: str, exc) -> dict:
    """Park `run_id` behind the live owner of `exc.peer` -> `{parked: "co-tenant-wait", owner, module,
    symbol, importer}`; a halted owner -> `{cause: "co_tenant_dependency_on_halted_run", owner,
    owner_refire, module, symbol, importer}` and no park record; no owner -> `{cause:
    "co_tenant_dependency", owner: None, ...}`. An owner that ended between attribution and the park
    (its wake already ran) is un-parked and attributed once more."""
    root = Path(root)
    module = getattr(exc, "module", "") or (exc.symbol if getattr(exc, "missing", "") == "module" else "")
    named = {"module": module, "symbol": exc.symbol, "importer": exc.importer}
    for _ in range(2):
        found = owner_of(root, exc.peer, exclude_run_id=run_id)
        if found["state"] == "live" and _park_behind(root, run_id, found["run_id"], exc):
            return {"parked": PARKED, "owner": found["run_id"], **named}
        if found["state"] == "halted":
            action = (_halted_marker(root, found["run_id"]) or {}).get(halted_run.HALT_NEXT_ACTION_FIELD)
            call = action.get("call") if isinstance(action, dict) else None
            return {
                "cause": CAUSE_HALTED_OWNER, "owner": found["run_id"],
                "owner_refire": call if isinstance(call, str) else None, **named,
            }
        if found["state"] is None:
            break
    return {"cause": CAUSE_TYPED, "owner": None, **named}

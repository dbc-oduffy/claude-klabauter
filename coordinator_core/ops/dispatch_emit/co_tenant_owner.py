"""Which run owns a peer-dirty path a run's clean export needs, and what the needing run does about it.

`owner_of` reads the live-run index (fallback `live_run_writes.live_footprints`) and the halted-withheld
index; `resolve` parks the needing run behind a live owner through `stage_wait.park` (the owner's
`run_token.consume`/`discard` wakes it) or returns the typed cause for a halted or unattributable owner.
In-process reads only: no spawn, no poll.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional

from coordinator_core.ops.dispatch_emit import ask_contract, co_tenant_cycle, co_tenant_kept, halted_run, halted_withheld_index, live_run_index, live_run_writes, run_lineage, run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

PARKED = "co-tenant-wait"
CAUSE_TYPED = "co_tenant_dependency"
CAUSE_HALTED_OWNER = "co_tenant_dependency_on_halted_run"
CAUSE_CYCLE = co_tenant_cycle.CAUSE_CYCLE
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
    return marker if marker and marker.get("status") in (halted_run.STATUS_HALTED, halted_run.STATUS_SUPERSEDED) else None


def _launcherless_halted(root: Path, run_id: str) -> bool:
    """A run whose token is live but whose halt marker is written and which holds no launcher escrow: nothing
    will end it, so a wait behind it never wakes."""
    return run_token.escrowed(root, run_id) is None and _halted_marker(root, run_id) is not None


def _proceeded_without(root: Path, run_id: str) -> frozenset:
    record = stage_wait.read(root, run_id) or {}
    held = record.get("proceeded_without")
    return frozenset(x for x in held if isinstance(x, str)) if isinstance(held, list) else frozenset()


_STAMP = re.compile(r"\d{8}T\d{6}")


def _landed_successor(root: Path, run_id: str, sizing_rel: object) -> Optional[str]:
    """A later run of `run_id`'s sizing whose run-stage reads `landed`, else None: its record dirs are
    `warp-<sizing stem>-<stamp>-<hex>` or `ask-<stamp>-<sizing hex>` with a stamp past `run_id`'s. One
    directory listing, one stage read per name match; no spawn."""
    if not isinstance(sizing_rel, str) or not sizing_rel:
        return None
    rel = sizing_rel.replace("\\", "/")
    stem = PurePosixPath(rel).stem
    hexes = run_lineage.sizing_hexes([rel])
    mine = (_STAMP.findall(run_id) or [""])[-1]
    try:
        with os.scandir(record_dir(root, run_id).parent) as it:
            names = sorted(e.name for e in it)
    except (OSError, ValueError):
        return None
    for name in names:
        stamp = (_STAMP.findall(name) or [""])[-1]
        if name == run_id or not stamp or stamp <= mine:
            continue
        if not (name.startswith(f"warp-{stem}-") or (name.startswith("ask-") and name.rsplit("-", 1)[-1] in hexes)):
            continue
        if (ask_contract.read_run_stage(root, name) or {}).get("stage") == "landed":
            return name
    return None


def _halted_age_s(root: Path, run_id: str, entry) -> Optional[int]:
    """Seconds since `run_id` halted: its index `entry`'s write time, else its `halted.json` mtime; None when neither reads."""
    at_ns = entry.at_ns if entry and entry.at_ns else 0
    if not at_ns:
        try:
            at_ns = (record_dir(root, run_id) / halted_run.HALTED_FILE).stat().st_mtime_ns
        except (OSError, ValueError):
            return None
    return max(0, int(time.time() - at_ns / 1e9))


def _stranded(root: Path, run_id: str) -> bool:
    """A run with no halt marker whose stage reads `staged` and which holds no live token or launcher escrow:
    nothing will end it, so it is attributed like a halted run."""
    if (ask_contract.read_run_stage(root, run_id) or {}).get("stage") != "staged":
        return False
    return run_token.escrowed(root, run_id) is None and not run_token.is_live(root, run_id)


def _halt_predates_dirt(root: Path, run_id: str, entry, path: str) -> bool:
    """True when `run_id` halted before `path` was last written in the worktree, so its halt cannot have left
    that dirt; False when either time is unreadable."""
    halted_ns = entry.at_ns if entry and entry.at_ns else 0
    if not halted_ns:
        try:
            halted_ns = (record_dir(root, run_id) / halted_run.HALTED_FILE).stat().st_mtime_ns
        except (OSError, ValueError):
            return False
    try:
        return halted_ns < (root / path).stat().st_mtime_ns
    except OSError:
        return False


def _refire_call(root: Path, run_id: str, entry) -> Optional[str]:
    """The refire call of `run_id`'s halt marker; a stranded run, which has none, gets an engine-ask resume of its sizing."""
    marker = _halted_marker(root, run_id)
    if marker is None:
        if not (entry and entry.sizing_rel):
            return None
        return ask_contract.refire_next_action_for(
            "stranded_staged", run_id=run_id, sizing_rel=entry.sizing_rel, detail="stranded staged run: resume it",
        ).get("call")
    action = marker.get(halted_run.HALT_NEXT_ACTION_FIELD)
    call = action.get("call") if isinstance(action, dict) else None
    return call if isinstance(call, str) else None


def _names(path: str, held: tuple) -> bool:
    """True when `held` (repo-relative paths) holds `path` itself or a directory containing it."""
    return bool(live_run_writes.overlaps([path], {"_": frozenset(held)}))


def owner_of(root: Path, path: str, *, exclude_run_id: str, include_waiting: bool = False) -> dict:
    """`{run_id, state}` of the run holding `path` uncommitted: state "live" (a run with a live token whose
    staged footprint names it), else "halted" (a halted record, or a stranded `staged` run with no halt
    marker and no live launcher, whose withheld or declared paths name it; a run withholding it or keeping
    it outranks one that only declares it, then the most recently halted; a marked run that only declares
    it and halted before the path's worktree mtime is never the owner), else `{run_id: None, state: None}`.
    `exclude_run_id`, its chain kin and a live
    run already parked behind `exclude_run_id` are never the owner, unless `include_waiting`: `resolve` keeps
    that run so the two-run wait cycle is named instead of read as no owner."""
    root = Path(root)
    path = _norm(path)
    live = live_run_writes.overlaps([path], _live_footprints(root, exclude_run_id))
    skipped = _proceeded_without(root, exclude_run_id)
    candidates = sorted(
        rid for rid in live
        if rid != exclude_run_id and rid not in skipped and run_token.is_live(root, rid)
        and (include_waiting or exclude_run_id not in ((stage_wait.read(root, rid) or {}).get("blockers") or ()))
    )
    halted: dict[str, tuple] = {}
    for rid, entry in halted_withheld_index.entries(root).items():
        if rid == exclude_run_id or rid in live or rid in skipped or not _names(path, (*entry.paths, *entry.declared)):
            continue
        covers = _names(path, entry.paths) or _names(path, tuple(co_tenant_kept.kept_paths(root, rid)))
        halted[rid] = (1 if covers else 0, entry.at_ns)
    kin = run_lineage.chain_kin(root, exclude_run_id, [*candidates, *halted])
    for rid in candidates:
        if rid not in kin:
            return {"run_id": rid, "state": "live"}
    entries = halted_withheld_index.entries(root)
    for rid in sorted(halted, key=lambda r: (halted[r], r), reverse=True):
        if rid in kin:
            continue
        entry = entries.get(rid)
        marker = _halted_marker(root, rid)
        if marker is None:
            if not _stranded(root, rid):
                continue
            sizing_rel = entry.sizing_rel if entry else None
        else:
            if not halted[rid][0] and _halt_predates_dirt(root, rid, entry, path):
                continue
            sizing_rel = marker.get("sizing_rel") or (entry.sizing_rel if entry else None)
        if _landed_successor(root, rid, sizing_rel) is None:
            return {"run_id": rid, "state": "halted"}
    return {"run_id": None, "state": None}


def _park_behind(root: Path, run_id: str, owner: str, exc) -> bool:
    if _launcherless_halted(root, owner):
        return False
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
    symbol, importer}`; a halted owner (never one whose sizing has a later landed run) -> `{cause:
    "co_tenant_dependency_on_halted_run", owner, chain: [run_id, owner], owner_halted_age_s,
    owner_refire, module, symbol, importer}` and no park record; no owner -> `{cause:
    "co_tenant_dependency", owner: None, ...}`. An owner whose own needs lead back to `run_id` ->
    `{cause: "co_tenant_cycle", cycle: [run ids], remedy, owner, ...}` with no park record and no `parked`
    key (`clean_overlay_verify` reads `parked` before `cause`). An owner that ended between attribution and
    the park (its wake already ran) is un-parked and attributed once more."""
    root = Path(root)
    module = getattr(exc, "module", "") or (exc.symbol if getattr(exc, "missing", "") == "module" else "")
    named = {"module": module, "symbol": exc.symbol, "importer": exc.importer}
    for _ in range(2):
        found = owner_of(root, exc.peer, exclude_run_id=run_id, include_waiting=True)
        cycle = co_tenant_cycle.cycle_through(root, run_id, found["run_id"]) if found["run_id"] else None
        if cycle:
            return {
                "cause": CAUSE_CYCLE, "cycle": list(cycle), "remedy": co_tenant_cycle.joint_remedy(cycle),
                "owner": found["run_id"], **named,
            }
        if found["state"] == "live" and _park_behind(root, run_id, found["run_id"], exc):
            return {"parked": PARKED, "owner": found["run_id"], **named}
        if found["state"] == "halted" or (found["state"] == "live" and _launcherless_halted(root, found["run_id"])):
            entry = halted_withheld_index.entries(root).get(found["run_id"])
            return {
                "cause": CAUSE_HALTED_OWNER, "owner": found["run_id"], "chain": [run_id, found["run_id"]],
                "owner_halted_age_s": _halted_age_s(root, found["run_id"], entry),
                "owner_refire": _refire_call(root, found["run_id"], entry), **named,
            }
        if found["state"] is None:
            break
    return {"cause": CAUSE_TYPED, "owner": None, **named}

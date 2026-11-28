"""Which run owns a peer-dirty path a run's clean export needs, and what the needing run does about it.

`owner_of` reads the live-run index (fallback `live_run_writes.live_footprints`) and names only a live run;
`peer_hunk_runs` also reads the halted-withheld index, for attribution. `resolve` parks the needing run
behind a live owner through `stage_wait.park` (the owner's `run_token.consume`/`discard` wakes it) or
returns the typed cause when no live run holds the path. In-process reads only: no spawn, no poll.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional

from coordinator_core.ops.dispatch_emit import ask_contract, co_tenant_cycle, co_tenant_kept, halted_run, halted_withheld_index, live_run_index, live_run_writes, run_lineage, run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

PARKED = "co-tenant-wait"
CAUSE_TYPED = "co_tenant_dependency"
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


def _withheld_by(root: Path, run_id: str) -> tuple:
    """The paths `run_id` left uncommitted at its halt: its marker's `run_artifacts.withheld` plus its index
    entry's paths. () with neither record, which `work_landed_at_head` reads as "withheld nothing known"
    (every dirty declared path then keeps the run unlanded)."""
    marker = _halted_marker(root, run_id) or {}
    withheld = (marker.get("run_artifacts") or {}).get("withheld")
    held = [p for p in withheld if isinstance(p, str)] if isinstance(withheld, (dict, list)) else []
    entry = halted_withheld_index.entries(root).get(run_id)
    return tuple(_norm(p) for p in (*held, *(entry.paths if entry else ())))


def work_landed_at_head(root: Path, run_id: str) -> bool:
    """True when the run's declared write set (`review_declared_paths` plus row `writes` of its manifest) holds
    at least one path that differs between the run's base and HEAD and is clean in the working tree, and every
    other declared path is clean too or dirty with a hunk the run did not withhold at its halt (a later run's
    dirt, never this run's own): its work is committed. Dirt on a path the run did withhold, or any dirt when
    the run's withheld set is unknown, keeps it unlanded. False on an unreadable manifest, no recorded
    base, an empty write set or any unreadable tree: nothing then proves the work landed. In-process object
    reads only; no spawn."""
    try:
        import json

        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_objects import read_object
        from coordinator_core.git.git_state import _parse_tree_entries, head_sha
        from coordinator_core.ops.dispatch_emit import ask_fanout_contract
        from coordinator_core.ops.dispatch_emit.halted_run_landed import _clean_at_head, _manifest_write_set

        doc = json.loads((record_dir(root, run_id) / "manifest.json").read_text(encoding="utf-8"))
        paths = _manifest_write_set(doc)
        base = ask_fanout_contract.pinned_run_base(root, run_id, doc)
        head = head_sha(root)
        if not paths or not base or not head:
            return False
        common = resolve_git_common_dir(root)
        dirs: dict = {}

        def _dir(tree_sha: str) -> Optional[dict]:
            if tree_sha not in dirs:
                got = read_object(common, tree_sha)
                dirs[tree_sha] = _parse_tree_entries(got[1]) if got and got[0] == "tree" else None
            return dirs[tree_sha]

        clean = _clean_at_head(root, common, _dir, paths, head)
        held = _withheld_by(root, run_id)
        if any(p not in clean and (not held or _names(p, held)) for p in paths):
            return False
        return bool(clean - _clean_at_head(root, common, _dir, paths, base))
    except Exception:  # noqa: BLE001 -- an unreadable record proves nothing landed
        return False


_STAMP = re.compile(r"\d{8}T\d{6}")


def _landed_successor(root: Path, run_id: str, sizing_rel: object) -> Optional[str]:
    """A later run of `run_id`'s sizing whose run-stage reads `landed`, or reads `halted` with its work landed
    at HEAD (`work_landed_at_head`: a hand-landed run), else None: its record dirs are
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
        stage = (ask_contract.read_run_stage(root, name) or {}).get("stage")
        if stage == "landed" or (stage == "halted" and work_landed_at_head(root, name)):
            return name
    return None


def _names(path: str, held: tuple) -> bool:
    """True when `held` (repo-relative paths) holds `path` itself or a directory containing it."""
    return bool(live_run_writes.overlaps([path], {"_": frozenset(held)}))


def peer_hunk_runs(root: Path, path: str, *, exclude_run_id: str) -> tuple:
    """Other runs (live footprint, halted withheld or kept paths) whose records name `path`, chain kin dropped; attribution only, never a hold."""
    root = Path(root)
    path = _norm(path)
    live = live_run_writes.overlaps([path], _live_footprints(root, exclude_run_id))
    named = {rid for rid in live if rid != exclude_run_id and run_token.is_live(root, rid)}
    for rid, entry in halted_withheld_index.entries(root).items():
        if rid == exclude_run_id or not _names(path, (*entry.paths, *entry.declared)):
            continue
        if _names(path, entry.paths) or _names(path, tuple(co_tenant_kept.kept_paths(root, rid))):
            named.add(rid)
    kin = run_lineage.chain_kin(root, exclude_run_id, sorted(named))
    return tuple(sorted(rid for rid in named if rid not in kin))


def owner_of(root: Path, path: str, *, exclude_run_id: str, include_waiting: bool = False) -> dict:
    """`{run_id, state}` of the live run holding `path` uncommitted (a run with a live token whose staged
    footprint names it), else `{run_id: None, state: None}`: a halted run holds nothing (`peer_hunk_runs`
    names it for attribution). `exclude_run_id`, its chain kin and a live run already parked behind
    `exclude_run_id` are never the owner, unless `include_waiting`: `resolve` keeps that run so the two-run
    wait cycle is named instead of read as no owner."""
    root = Path(root)
    path = _norm(path)
    live = live_run_writes.overlaps([path], _live_footprints(root, exclude_run_id))
    skipped = _proceeded_without(root, exclude_run_id)
    candidates = sorted(
        rid for rid in live
        if rid != exclude_run_id and rid not in skipped and run_token.is_live(root, rid)
        and (include_waiting or exclude_run_id not in ((stage_wait.read(root, rid) or {}).get("blockers") or ()))
    )
    kin = run_lineage.chain_kin(root, exclude_run_id, candidates)
    for rid in candidates:
        if rid not in kin:
            return {"run_id": rid, "state": "live"}
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
    symbol, importer}`; no live owner (a halted run's record only, or none) -> `{cause:
    "co_tenant_dependency", owner: None, peer_runs: [halted or other runs naming the path], ...}` and no
    park record. An owner whose own needs lead back to `run_id` ->
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
        if found["state"] is None or _launcherless_halted(root, found["run_id"]):
            break
    return {
        "cause": CAUSE_TYPED, "owner": None,
        "peer_runs": list(peer_hunk_runs(root, exc.peer, exclude_run_id=run_id)), **named,
    }

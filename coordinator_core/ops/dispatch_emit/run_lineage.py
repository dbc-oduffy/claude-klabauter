"""coordinator_core.ops.dispatch_emit.run_lineage -- which earlier runs are a run's own kin.

Purpose: the lineage readers `ask_stage` once owned, in a module with a light import closure so
`hunk_provenance` and `pre_existing_ruling` reach them without loading `ask_stage` (and through it
`emit`, `pathspec` and `inventory_mint`).

Negative-spec:
    - Imports nothing from `ask_stage`, `emit`, `pathspec` or `inventory_mint`.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Iterable

from coordinator_core.ops._path_guard import safe_id


def chain_kin(root: Path, run_id: str, candidates: Iterable[str]) -> set[str]:
    """The `candidates` that are `run_id`'s own chain: a record under `run_id` plus a `-` suffix (a
    stage call with a suffixed id), runs bound to the same sizing (a chained
    run's `chained_from` ancestor and descendants carry the sizing it hands on) and any run on its
    `resumed_from` chain. Their claims are the same work, never a peer's overlap.
    A run's sizings are its bound one plus its `earlier_sizings`, so a refire rebound to a newer sizing
    still owns the runs bound to the one it replaced; an `ask-` run with no recorded sizing at all is
    kin when its trailing hex is the hex a sizing of this run carries.
    Trap: a run with no bound sizing has no kin by sizing."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        tokens = {r.get("run_id"): r.get("sizing_rel") for r in run_token.held_records(root)}
    except (OSError, ValueError, KeyError, TypeError):
        tokens = {}

    def sizings_of(rid: str) -> set[str]:
        try:
            rec = rl.read_run(record_dir(root, rid)) or {}
        except ValueError:
            rec = {}
        rels = [rec.get("sizing_rel") or tokens.get(rid), *(rec.get(rl.EARLIER_SIZINGS_FIELD) or ())]
        return {str(r).replace("\\", "/") for r in rels if r and isinstance(r, (str, Path))}

    mine = sizings_of(run_id)
    my_hexes = sizing_hexes(mine)
    chain: set[str] = set()
    cur = run_id
    for _ in range(rl.CARRIED_HOP_CAP):
        try:
            prev = (rl.read_run(record_dir(root, cur)) or {}).get(rl.RESUMED_FROM_FIELD)
        except ValueError:
            break
        if not (isinstance(prev, str) and prev and prev not in chain and prev != run_id):
            break
        chain.add(prev)
        cur = prev
    own_prefix = run_id + "-"
    out: set[str] = set()
    for rid in candidates:
        if rid in chain or rid.startswith(own_prefix):
            out.add(rid)
            continue
        theirs = sizings_of(rid)
        if (mine & theirs) or (not theirs and rid.startswith("ask-") and rid.rsplit("-", 1)[-1] in my_hexes):
            out.add(rid)
    return out


def sizing_hexes(sizing_rels: Iterable[str]) -> set[str]:
    """The trailing 6-hex token of each sizing's stem: the hex an `ask-<stamp>-<hex>` run minted with it carries."""
    out: set[str] = set()
    for rel in sizing_rels:
        tail = PurePosixPath(rel).stem.rsplit("-", 1)[-1]
        if re.fullmatch(r"[0-9a-f]{6}", tail):
            out.add(tail)
    return out


def lineage_kin(root: Path, run_id: str) -> tuple[str, ...]:
    """The earlier runs `chain_kin` reads as `run_id`'s own, discovered from one listing of the record
    homes: the `resumed_from` chain plus each record dir whose stamp precedes `run_id`'s and whose name
    is `warp-<sizing stem>-...` or `ask-<stamp>-<sizing hex>`. Never raises; unreadable lineage is empty."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.landing_integrity import _resumed_chain

    try:
        rec = rl.read_run(record_dir(root, run_id)) or {}
        rels = {str(r).replace("\\", "/") for r in (rec.get("sizing_rel"), *(rec.get(rl.EARLIER_SIZINGS_FIELD) or ())) if r}
        stems = {PurePosixPath(r).stem for r in rels}
        hexes = sizing_hexes(rels)
        mine = (re.findall(r"\d{8}T\d{6}", run_id) or [""])[-1]
        names = [p.name for p in record_dir(root, run_id).parent.iterdir()] if (stems and mine) else []
        found: list[str] = list(_resumed_chain(root, run_id))
        for name in sorted(names):
            stamp = (re.findall(r"\d{8}T\d{6}", name) or [""])[-1]
            if name == run_id or not safe_id(name) or not stamp or stamp >= mine:
                continue
            if any(name.startswith(f"warp-{s}-") for s in stems) or (
                name.startswith("ask-") and name.rsplit("-", 1)[-1] in hexes
            ):
                found.append(name)
        return tuple(sorted(chain_kin(root, run_id, found)))
    except (OSError, ValueError, TypeError):
        return ()

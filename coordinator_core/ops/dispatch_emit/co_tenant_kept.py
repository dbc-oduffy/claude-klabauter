"""Co-tenant kept-blob identity: which foreign holders keep byte-identical work, and which run orders first.

Two runs that keep the same blob for a path hold the same work; attributing it to both is truthful, so the
landing decides it by seniority instead of refusing both. Callers: `identical_holders` then `senior`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def _kept(root: Path, run_id: str) -> Optional[dict]:
    """`{path: kept blob spec}` for the halt of `run_id`, or None when the id is unsafe or the record unreadable."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_hold_release

    if not safe_id(run_id):
        return None
    try:
        plan = halted_hold_release._kept_plan(ask_contract.record_dir(root, run_id))
    except Exception:  # noqa: BLE001 -- unreadable or malformed kept work: the run's paths stay foreign
        return None
    return None if plan is None else {p: plan[1][p] for p in plan[0] if p in plan[1]}


def identical_holders(root: Path, own_run: str, foreign: dict) -> dict:
    """`{path: holder_run}` for each foreign path whose single identical holder keeps the same blob as `own_run`.

    A foreign value may join several holders with ", "; the path maps only when exactly one of them keeps an
    object id equal to `own_run`'s. A holder with no readable kept record (a live run) never matches, so its
    path stays foreign. One `cat-file --batch-check` for every spec; no spawn when nothing can be compared."""
    if not foreign:
        return {}
    own = _kept(root, own_run)
    if not own:
        return {}
    candidates: list = []  # (path, holder, own spec, holder spec)
    cache: dict = {}
    for path, value in foreign.items():
        if path not in own:
            continue
        for holder in dict.fromkeys(h.strip() for h in str(value).split(",") if h.strip()):
            if holder == own_run:
                continue
            if holder not in cache:
                cache[holder] = _kept(root, holder)
            theirs = cache[holder]
            if theirs and path in theirs:
                candidates.append((path, holder, own[path], theirs[path]))
    if not candidates:
        return {}
    from coordinator_core.ops.dispatch_emit.halted_run_superseded import _blob_ids

    specs = list(dict.fromkeys(s for _, _, a, b in candidates for s in (a, b)))
    ids = _blob_ids(root, specs)
    if ids is None:
        return {}
    oid = dict(zip(specs, ids))
    matched: dict = {}
    for path, holder, mine, theirs in candidates:
        if oid[mine] is not None and oid[mine] == oid[theirs]:
            matched.setdefault(path, []).append(holder)
    return {path: holders[0] for path, holders in matched.items() if len(holders) == 1}


def senior(root: Path, own_run: str, own_seniority: Optional[tuple], holder: str) -> bool:
    """True when `own_run` orders before `holder`. Both runs reach the same answer: the `(minted_at, run_id)`
    tuples compare when both resolve, else lexical run-id order, so exactly one of a pair is senior."""
    from coordinator_core.ops.dispatch_emit.land_run import _own_run_seniority

    theirs = _own_run_seniority(root, holder, None)
    if own_seniority is not None and theirs is not None:
        return own_seniority < theirs
    return own_run < holder

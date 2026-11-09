"""Run-end scratch reaper: tombstones the scratch a finished run's own ledger lists.

Inputs are the run ledger and the repo keep index only; nothing here lists a directory or spawns a
process. A path leaves by one `os.rename` into `scratch/.deleted/`, the shape `scratch.delete` uses.
Invariant: the own-run gate reads the halted marker and the retest record directly, never
`_run_referenced`, which holds a just-landed run for its 24h activity window.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import export_lifecycle, run_scratch
from coordinator_core.ops.dispatch_emit.halted_run import (
    HALTED_FILE,
    STATUS_LANDED,
    STATUS_SUPERSEDED,
    read_strict,
)
from coordinator_core.ops.scratch_delete import TOMBSTONE_DIRNAME, _index_refusal

_WARP_PREFIX = "scratch/warp/"


def _own_run_hold(root: Path, run_id: str) -> str:
    """The reason the run's own scratch must stay, or '' when it may be reaped."""
    from coordinator_core.ops.dispatch_emit import ask_contract

    try:
        marker = read_strict(ask_contract.record_dir(root, run_id))
    except ValueError as exc:
        return f"halt marker unreadable: {exc}"
    if marker is not None and marker.get("status") not in (STATUS_LANDED, STATUS_SUPERSEDED):
        return f"{HALTED_FILE} status {marker.get('status')!r} is not landed or superseded"
    if export_lifecycle._retest_live(root, run_id):
        return "landing retest is live"
    return ""


def _ledger_keep_expiry(keeps: list) -> dict[str, Optional[datetime]]:
    out: dict[str, Optional[datetime]] = {}
    for k in keeps:
        stamp = run_scratch._parse(k.get("kept_at"))
        ttl = k.get("ttl_hours")
        ok = stamp is not None and isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0
        out[k.get("path")] = stamp + timedelta(hours=ttl) if ok else None
    return out


def _warp_owner(rel: str) -> Optional[str]:
    if rel.startswith(_WARP_PREFIX):
        return rel[len(_WARP_PREFIX):].split("/", 1)[0] or None
    return None


def _tombstone(root: Path, rel: str, seq: int, result: dict) -> bool:
    """Rename `rel` into the tombstone dir; records the outcome. True when the path is gone."""
    target = root / rel
    if not os.path.lexists(target):
        result["absent"].append(rel)
        return True
    from coordinator_core.ops import prune_claude_temp as _p

    cur = root
    for seg in rel.split("/"):
        cur = cur / seg
        if _p._is_link(str(cur)):
            result["refused"].append({"path": rel, "reason": "path has a link or junction component"})
            return False
    tomb = root / "scratch" / TOMBSTONE_DIRNAME
    try:
        os.makedirs(tomb, exist_ok=True)
        os.rename(target, tomb / f"{time.time_ns()}-{seq}-{target.name}")
    except OSError as exc:
        result["refused"].append({"path": rel, "reason": f"rename failed: {exc}"})
        return False
    result["removed"].append(rel)
    return True


def reap_run(root, run_id: str, now: Optional[datetime] = None) -> dict:
    """Tombstone `run_id`'s ledger paths plus `scratch/warp/<run_id>`, sparing unexpired keeps, a
    live peer's paths and git-tracked paths. Returns `{removed, kept, refused, absent, held}`;
    `held` is the own-run gate's reason ('' when the run was reapable). Never raises."""
    root = Path(root)
    result: dict = {"removed": [], "kept": [], "refused": [], "absent": [], "held": ""}
    moment = now or datetime.now(timezone.utc)
    try:
        _reap(root, run_id, moment, result)
    except Exception as exc:  # never raises: report and reap nothing further
        result["refused"].append({"path": "", "reason": f"reap failed: {type(exc).__name__}: {exc}"})
    return result


def _reap(root: Path, run_id: str, moment: datetime, result: dict) -> None:
    ledger_file = run_scratch.ledger_path(root, run_id)
    if ledger_file.exists():
        try:
            if not isinstance(json.loads(ledger_file.read_text(encoding="utf-8")), dict):
                raise ValueError("ledger is not an object")
        except (OSError, ValueError) as exc:
            result["refused"].append({"path": run_scratch.LEDGER_FILE, "reason": f"ledger unreadable: {exc}"})
            return
    ledger = run_scratch.read_ledger(root, run_id)
    candidates: list[str] = [f"{_WARP_PREFIX}{run_id}"]
    for p in ledger["paths"]:
        try:
            norm = run_scratch.normalise_rel(p)
        except ValueError as exc:
            result["refused"].append({"path": str(p), "reason": str(exc)})
            continue
        if norm not in candidates:
            candidates.append(norm)

    hold = _own_run_hold(root, run_id)
    if hold:
        result["held"] = hold
        result["kept"].extend(candidates)
        return

    index = run_scratch.read_keep_index(root)
    ledger_expiry = _ledger_keep_expiry(ledger["keeps"])
    expired = set(run_scratch.keep_index_expired(root, moment))
    referenced: dict[str, bool] = {}

    def peer_live(owner: Optional[str]) -> bool:
        if not owner or owner == run_id:
            return False
        if owner not in referenced:
            referenced[owner] = export_lifecycle._run_referenced(root, owner)
        return referenced[owner]

    survivors: list[str] = []
    for rel in candidates:
        entry = index.get(rel)
        until = ledger_expiry.get(rel)
        if until is not None and until > moment:
            result["kept"].append(rel)
            continue
        if isinstance(entry, dict) and rel not in expired:
            result["kept"].append(rel)
            continue
        owner = entry.get("run_id") if isinstance(entry, dict) else None
        if peer_live(owner) or peer_live(_warp_owner(rel)):
            result["kept"].append(rel)
            continue
        survivors.append(rel)

    seq = 0
    if survivors:
        tracked = _index_refusal(root, survivors)
        for rel in survivors:
            if rel in tracked:
                result["refused"].append({"path": rel, "reason": tracked[rel]})
                continue
            seq += 1
            _tombstone(root, rel, seq, result)

    gone = set(result["removed"]) | set(result["absent"])
    stale = {}
    for rel in sorted(expired):
        meta = index.get(rel)
        owner = meta.get("run_id") if isinstance(meta, dict) else None
        if rel in candidates:
            if rel in gone:
                stale[rel] = None
            continue
        if peer_live(owner):
            continue
        try:
            norm = run_scratch.normalise_rel(rel)
        except ValueError:
            stale[rel] = None
            continue
        tracked = _index_refusal(root, [norm])
        if norm in tracked:
            result["refused"].append({"path": norm, "reason": tracked[norm]})
            continue
        seq += 1
        if _tombstone(root, norm, seq, result):
            stale[rel] = None
    if stale:
        # Re-read: a peer's declare_keep since the first read must not be overwritten by this write.
        fresh = run_scratch.read_keep_index(root)
        for rel in stale:
            fresh.pop(rel, None)
        run_scratch._write_json(Path(root) / run_scratch.KEEP_INDEX_REL, fresh)

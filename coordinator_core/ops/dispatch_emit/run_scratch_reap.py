"""Run-end scratch reaper: tombstones the scratch a finished run's own ledger lists.

Inputs are the run ledger, the repo keep index and the `.owner.json` marks one level under the
share, session and coordinator-temp roots; no process is spawned and no deeper directory is walked.
A path leaves by one `os.rename`
into `scratch/.deleted/`, the shape `scratch.delete` uses.
Invariant: the own-run gate reads the halted marker and the retest record directly, never
`_run_referenced`, which holds a just-landed run for its 24h activity window.
Invariant: a halted or resumed run keeps exactly `RESUME_INPUTS` under its warp dir; every other
child (the export trees above all) and its ledgered paths are reaped.
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
    STATUS_CLOSED,
    STATUS_LANDED,
    STATUS_RESUMED,
    STATUS_SUPERSEDED,
    read_strict,
)
from coordinator_core.ops.scratch_delete import TOMBSTONE_DIRNAME, _index_refusal

_WARP_PREFIX = "scratch/warp/"
_HALTED = "halted"
_TERMINAL = (STATUS_LANDED, STATUS_SUPERSEDED, STATUS_CLOSED)
_RESUMABLE = (_HALTED, STATUS_RESUMED)

#: Repo-relative writer roots whose direct children carry an owner mark; the only dirs listed for owned paths.
OWNED_REPO_ROOTS = (".coordinator-local/subagent-share", ".coordinator-local/coordinator-sessions")

#: Files under `scratch/warp/<run_id>` a halted run's landing or resume still reads: the recorded
#: base (`recorded_run_base`), the submission stamp (`land_run`, `landing_retest`), the fan-out
#: status files peers poll (`ask_fanout_contract`), the commit request the terminal drain parses
#: and the remainder verdict. Everything else there is rebuildable.
RESUME_INPUTS = frozenset({
    "run-base",
    "submission.json",
    "sibling-status.json",
    "superseded.json",
    "join-waiters.json",
    "parent.json",
    "commit-request.txt",
    "land-remainder-verdict.json",
})


def _own_run_gate(root: Path, run_id: str) -> tuple[str, bool]:
    """`(hold, partial)`: the reason the run's own scratch must stay ('' when it may be reaped),
    and whether only the non-resume-input part of the warp dir may go (a resumable halt)."""
    from coordinator_core.ops.dispatch_emit import ask_contract

    try:
        record = ask_contract.record_dir(root, run_id)
        marker = read_strict(record)
    except ValueError as exc:
        return f"halt marker unreadable: {exc}", False
    partial = False
    if marker is not None and marker.get("status") not in _TERMINAL:
        status = marker.get("status")
        if status not in _RESUMABLE:
            return f"{HALTED_FILE} status {status!r} is not a terminal or resumable status", False
        if record == root / ask_contract.RUN_DIR_ROOT / run_id:
            return f"{HALTED_FILE} of a resumable run sits inside its warp dir", False
        partial = True
    if export_lifecycle._retest_live(root, run_id):
        return "landing retest is live", False
    return "", partial


def _warp_children(root: Path, run_id: str, result: dict) -> list[str]:
    """The `scratch/warp/<run_id>` children outside `RESUME_INPUTS`; the inputs are reported kept."""
    base = f"{_WARP_PREFIX}{run_id}"
    try:
        with os.scandir(root / base) as it:
            names = sorted(e.name for e in it)
    except OSError:
        return []
    out = []
    for name in names:
        (result["kept"] if name in RESUME_INPUTS else out).append(f"{base}/{name}")
    return out


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


def _owned_dirs(parent: Path, root: Path, run_id: str) -> list[Path]:
    """Children of `parent` (one scandir, links skipped) whose `.owner.json` names `run_id` for `root`."""
    from coordinator_core import owner_mark
    from coordinator_core.ops import prune_claude_temp as _p

    out: list[Path] = []
    try:
        with os.scandir(parent) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return out
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False) or _p._is_link(e.path):
                continue
        except OSError:
            continue
        mark = owner_mark.read_mark(e.path)
        if mark is None or mark["owner_kind"] != owner_mark.OWNER_RUN or mark["owner_id"] != run_id:
            continue
        repo = mark.get("repo")
        if repo and not owner_mark._same_dir(str(repo), str(root)):
            continue
        out.append(Path(e.path))
    return out


def _owned_paths(root: Path, run_id: str) -> tuple[list[str], list[Path]]:
    """`(repo-relative, external)` dirs marked as `run_id`'s, from the known writer roots only:
    the repo's share and session roots, and its coordinator temp root (temp indexes, managed dirs)."""
    rels: list[str] = []
    for space in OWNED_REPO_ROOTS:
        base = root.joinpath(*space.split("/"))
        rels.extend(f"{space}/{d.name}" for d in _owned_dirs(base, root, run_id))
    external: list[Path] = []
    try:
        from coordinator_core.temp_layout import coordinator_temp_root

        external = _owned_dirs(coordinator_temp_root(root), root, run_id)
    except (ValueError, OSError):
        pass
    return rels, external


def _tombstone_external(root: Path, target: Path, seq: int, result: dict) -> None:
    """Rename a dir outside the repo into the repo tombstone dir. Another volume (the system temp root
    is not the repo's drive on a split box) cannot take that rename: the dir is renamed beside itself,
    its owner mark dropped, and the temp-root age prune takes it from there."""
    from coordinator_core import owner_mark

    if owner_mark.is_kept(target):
        result["kept"].append(str(target))
        return
    tomb = root / "scratch" / TOMBSTONE_DIRNAME
    stem = f"{time.time_ns()}-{seq}-{target.name}"
    try:
        os.makedirs(tomb, exist_ok=True)
        os.rename(target, tomb / stem)
    except OSError:
        beside = target.with_name(f".deleted-{stem}")
        try:
            os.rename(target, beside)
        except OSError as exc:
            result["refused"].append({"path": str(target), "reason": f"rename failed: {exc}"})
            return
        try:
            os.unlink(beside / owner_mark.MARK_FILE)
        except OSError:
            pass
    result["removed"].append(str(target))


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
    """Tombstone `run_id`'s ledger paths plus `scratch/warp/<run_id>` (all of it, or all but
    `RESUME_INPUTS` for a halted or resumed run), sparing unexpired keeps, a live peer's paths and
    git-tracked paths. Returns `{removed, kept, refused, absent, held}`;
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
    hold, partial = _own_run_gate(root, run_id)
    candidates: list[str] = [] if partial else [f"{_WARP_PREFIX}{run_id}"]
    for p in ledger["paths"]:
        try:
            norm = run_scratch.normalise_rel(p)
        except ValueError as exc:
            result["refused"].append({"path": str(p), "reason": str(exc)})
            continue
        if norm not in candidates:
            candidates.append(norm)

    owned_rels, owned_external = _owned_paths(root, run_id)
    if hold or partial:
        # A held or resumable run may still read its owned dirs.
        result["kept"].extend(
            [r for r in owned_rels if r not in candidates] + [str(p) for p in owned_external]
        )
    else:
        candidates.extend(r for r in owned_rels if r not in candidates)
    if hold:
        result["held"] = hold
        result["kept"].extend(candidates)
        return
    if partial:
        candidates = _warp_children(root, run_id, result) + candidates

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

    if not (hold or partial):
        for target in owned_external:
            seq += 1
            _tombstone_external(root, target, seq, result)

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

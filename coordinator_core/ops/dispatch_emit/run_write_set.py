"""A run's whole write set, read once for review prep, the since-halt check and refire scope.

Invariant: the set is the run's own declared, leg-footprint, credited-chunk and withheld paths,
minus scratch and minus any path `land_run._peer_held` reports held by a live peer (an
unanswerable claim lookup holds nothing). A peer's bare declaration never drops a path this
run's own checkpoint landed; a live touch claim still does. Never the tree. `run_write_set` spawns nothing; `unclaimed_dirty` spawns one git.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from coordinator_core.ops.dispatch_emit import run_credit
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, record_dir
from coordinator_core.ops.dispatch_emit.halted_run import recorded_emitter

_SCRATCH_PREFIX = "scratch/"
_PLAN_LINE = re.compile(r"^plan:\s*[\"']?([^\"'\s#]+)", re.MULTILINE)


def _json_object(path: Path) -> Optional[dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _str_list(value: object) -> list:
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def _norm(path: str) -> str:
    return PurePosixPath(path.strip().replace("\\", "/")).as_posix()


def _sizing_rel(base: Path) -> Optional[str]:
    for name in ("run.json", "halted.json"):
        obj = _json_object(base / name)
        rel = obj.get("sizing_rel") if obj else None
        if isinstance(rel, str) and rel:
            return rel
    return None


def _plan_rel(root: Path, manifest: dict, sizing_rel: Optional[str]) -> Optional[str]:
    for key in ("plan_rel", "plan_path"):
        v = manifest.get(key)
        if isinstance(v, str) and v:
            return v
    if sizing_rel:
        try:
            text = (root / sizing_rel).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        m = _PLAN_LINE.search(text)
        return m.group(1) if m else None
    return None


def _credited_writes(root: Path, base: Path, manifest: dict, sizing_rel: Optional[str]) -> list:
    credits = run_credit.read([base])
    if sizing_rel:
        credits = {**run_credit.credits_for_sizing(root, sizing_rel), **credits}
    plan_rel = _plan_rel(root, manifest, sizing_rel)
    if not credits or not plan_rel:
        return []
    from coordinator_core.frontmatter.body_blocks import LocateStatus
    from coordinator_core.ops.dispatch_emit.spine_read import (
        NON_DISPATCHABLE_DISPOSITIONS,
        load_rows_memo,
        with_canonical_disposition,
    )

    try:
        text = (root / plan_rel).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    loaded = load_rows_memo(text)
    if loaded.status is not LocateStatus.LOCATED:
        return []
    out: list = []
    for raw in loaded.rows:
        row = with_canonical_disposition(raw)
        if not isinstance(row, dict) or row.get("id") not in credits:
            continue
        if row.get("disposition") in NON_DISPATCHABLE_DISPOSITIONS:
            continue
        out += _str_list(row.get("writes"))
    return out


def _without_peer_held(root: Path, ordered: list, own_session: str, run_id: str) -> tuple:
    return _split_peer_held(root, ordered, own_session, run_id)[0]


def _split_peer_held(root: Path, ordered: list, own_session: str, run_id: str) -> tuple:
    """``(kept, held)``: ``kept`` is ``ordered`` minus peer-held paths; ``held`` is ``land_run._peer_held``'s
    ``{path: [peer session ids]}`` over ``ordered``. One claim lookup."""
    # Lazy: land_run imports refire_inherited_scope, which reaches this module.
    from coordinator_core.ops.dispatch_emit import land_run

    plan_rel = run_base = None
    base = record_dir(root, run_id) if run_id else None
    manifest = _json_object(base / "manifest.json") if base else None
    if base is not None and manifest is not None:
        plan_rel = _plan_rel(root, manifest, _sizing_rel(base))
        try:
            from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base
            from coordinator_core.ops.dispatch_emit.halted_run import halted_run_base

            run_base = halted_run_base(base) or recorded_run_base(root, run_id)
        except Exception:  # noqa: BLE001 -- no base: the checkpoint walk stops at the first non-checkpoint commit
            run_base = None
    selves = land_run._firing_sessions(root, run_id, own_session or None) if run_id else set()
    if run_id:
        selves |= land_run._prior_pass_sessions(root, run_id)
    emitter = recorded_emitter(root, run_id) if run_id else None
    if emitter:
        selves.add(emitter)
    held = land_run._peer_held(
        root, ordered, own_session or None, run_id, None, plan_rel=plan_rel, run_base=run_base,
        self_sessions=selves)
    if selves:
        # The run's lead/firing session is self for review: its other runs' declarations never withhold this run's paths.
        held = {p: [s for s in sids if s not in selves] for p, sids in held.items()}
        held = {p: sids for p, sids in held.items() if sids}
    return tuple(p for p in ordered if p not in held), {p: list(held[p]) for p in ordered if p in held}


def run_write_set(root: Path, run_id: str, own_session: str) -> tuple:
    """The run's write set, deduped in first-seen order: manifest `review_declared_paths`, the leg
    footprint `writes` when present, the writes of credited plan rows, then `halted_withheld` and `withhold.jsonl` keys.
    Scratch paths and paths a live peer holds (`land_run._peer_held`) are dropped; a dead or
    abandoned session's claim keeps the path. Empty when the record is unreadable."""
    return run_write_set_split(root, run_id, own_session)[0]


def run_write_set_split(root: Path, run_id: str, own_session: str) -> tuple:
    """`(kept, held)`: `run_write_set`'s tuple plus `{path: [peer session ids]}` for each path the
    filter dropped as peer-held, in write-set order. `((), {})` when the record is unreadable."""
    ordered, held = run_write_set_full(root, run_id, own_session)
    return tuple(p for p in ordered if p not in held), held


def run_write_set_full(root: Path, run_id: str, own_session: str) -> tuple:
    """`(ordered, held)`: the run's whole write set in first-seen order, peer-held paths included,
    plus `{path: [peer session ids]}` for those a live peer holds. A read-only consumer (review prep)
    keeps every path and names the held ones. `((), {})` when the record is unreadable."""
    from coordinator_core.ops.dispatch_emit import run_withhold
    from coordinator_core.ops.dispatch_emit.halted_run import halted_withheld

    root = Path(root)
    base = record_dir(root, run_id)
    manifest = _json_object(base / "manifest.json")
    if manifest is None:
        return (), {}
    footprint = _json_object(base / LEG_FOOTPRINT_FILE)
    sizing_rel = _sizing_rel(base)
    raw = [
        *_str_list(manifest.get("review_declared_paths")),
        *(_str_list(footprint.get("writes")) if footprint else []),
        *_credited_writes(root, base, manifest, sizing_rel),
        *halted_withheld(base),
        *run_withhold.read([base]),
    ]
    seen: dict = {}
    for p in raw:
        n = _norm(p)
        if n and n != "." and not n.startswith(_SCRATCH_PREFIX):
            seen.setdefault(n, None)
    ordered = list(seen)
    return tuple(ordered), _split_peer_held(root, ordered, own_session, run_id)[1]


def unclaimed_dirty(root: Path, paths: Iterable[str], own_session: str, run_id: str = "") -> tuple:
    """Those of `paths` the worktree differs from HEAD on (modified, deleted or untracked) that
    `land_run._peer_held` does not report held by a live peer. One git status spawn; none when
    `paths` is empty. `run_id` lets a senior peer run's declared writes hold a path too."""
    from coordinator_core.git.run import run_git

    wanted = [_norm(p) for p in paths]
    if not wanted:
        return ()
    proc = run_git(
        ["-C", str(root), "-c", "core.quotepath=off", "--literal-pathspecs", "status", "--porcelain", "--untracked-files=all", "--", *wanted],
        timeout=30,
    )
    if proc.returncode != 0:
        return ()
    dirty: dict = {}
    for line in proc.stdout.splitlines():
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip('"')
        if entry:
            dirty.setdefault(_norm(entry), None)
    return _without_peer_held(Path(root), list(dirty), own_session, run_id)

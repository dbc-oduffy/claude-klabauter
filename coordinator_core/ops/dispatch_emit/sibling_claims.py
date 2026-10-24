"""Which admitted landing paths does another unlanded run of this tree claim?

`sibling_run_claims` composes `warp_run_deliverable.live_run_write_claims` and
`halted_run_write_claims` (a live token's declared writes, or a halted unresumed in-window run's
withheld paths) over the admitted set, then adds the sources those readers do not read for a halted
run: its `run.json` `sizing_rel`, its `run-outcome.json` `inline_review.prep.slice_files`, and
its manifest's `review_declared_paths`. `filter_own` drops the claimed paths the committing run
does not itself declare.

Invariants: in-process file reads only (no spawn); an unreadable declaration contributes
nothing and nothing raises; a token past its TTL with no halt marker is not a claim; a released halted run
(`halted_hold_release.release_reason`) claims nothing; a fresh-token run minted at or after
`minted_before` is junior and claims nothing. Stdlib
only at module top (terminal_commit cold-import pin); the readers are imported lazily.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable, Mapping, Optional

SIBLING_WITHHELD_REASON = "sibling-claimed"


def reland_remedy(sizing_rel: str) -> str:
    """One register line naming `warp_fire.fire_command`'s refire Workflow call for the run bound
    to `sizing_rel`: the engine resumes the halted run from its run-outcome.json and lands only
    its own claimed paths. `sizing_rel` is forward-slash normalised; no new op. `dispatch.ask_gate`
    only returns the gate acceptance and fires nothing, so it is never the remedy."""
    from coordinator_core import warp_fire

    path = _norm(sizing_rel) or "<sizing>"
    return (
        f"re-land: {warp_fire.fire_command(path)} "
        "-- re-fires the halted run from its run-outcome.json."
    )


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _unlanded_run_ids(
    root: Path, own_run_id: Optional[str], own_plan: Optional[str] = None, minted_before: Optional[tuple] = None,
) -> list:
    """Run ids other than `own_run_id` that are halted and unresumed, or hold a fresh token
    (unclaimed or claimed), sorted. One record-dir scan; the token is a point read per run, never a
    token-store listing. A stamped run id older than the token TTL is never opened. A halted run
    `halted_hold_release.release_reason` frees (against `own_run_id`/`own_plan`), and a resumed run
    whose successor landed or whose sizing is superseded, is not listed. A listed halted run may
    still claim nothing: `_held_extras` narrows it to what it withheld or left dirty."""
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import halted_hold_release, run_token, stopped_runs
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    now = time.time()
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as entries:
            names = sorted(e.name for e in entries if e.is_dir())
    except OSError:
        return []
    found = []
    for run_id in names:
        if run_id == own_run_id:
            continue
        stamp = stopped_runs._stamp_epoch(run_id)
        if stamp is not None and now - stamp > run_token.TTL_SECONDS:
            continue
        if warp_run_deliverable._halted(root, run_id):
            if stamp is not None and not halted_hold_release.release_reason(
                root, run_id, own_run_id, own_plan,
            ):
                found.append(run_id)
            continue
        for claimed in (False, True):
            record = run_token._read(run_token._record_path(root, run_id, claimed=claimed))
            if record is not None and run_token._fresh(record):
                if minted_before is not None and (record.get("minted_at"), run_id) >= tuple(minted_before):
                    break
                if not warp_run_deliverable.resumed_run_released(root, run_id, own_run_id):
                    found.append(run_id)
                break
    return found


def _extra_declared(root: Path, run_id: str) -> set:
    """Paths a run's own records name that the manifest reader does not: `run.json`
    `sizing_rel`, `run-outcome.json` `inline_review.prep.slice_files`, and the manifest's
    `review_declared_paths`."""
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, RUN_OUTCOME_FILE, record_dir

    out: set = set()
    try:
        rdir = record_dir(root, run_id)
    except ValueError:
        return out
    out.add(warp_run_deliverable.record_sizing_rel(root, run_id))
    prep = (_read_json(rdir / RUN_OUTCOME_FILE).get("inline_review") or {})
    prep = prep.get("prep") if isinstance(prep, dict) else None
    files = prep.get("slice_files") if isinstance(prep, dict) else None
    if isinstance(files, list):
        out.update(_norm(f) for f in files if isinstance(f, str))
    manifest = _read_json(root / RUN_DIR_ROOT / run_id / "manifest.json") or _read_json(rdir / "manifest.json")
    declared = manifest.get("review_declared_paths")
    if isinstance(declared, list):
        out.update(_norm(p) for p in declared if isinstance(p, str))
    out.discard("")
    return out


def _held_extras(root: Path, run_id: str, dirty=None, wanted: Optional[set] = None) -> set:
    """`_extra_declared`, narrowed for a halted run to the paths its halt record withholds plus
    those that are dirty now (`dirty()`, read at most once and only if a candidate in `wanted`
    remains -- an extra outside the candidates must not cost the status spawn)."""
    from coordinator_core.ops import warp_run_deliverable

    extra = _extra_declared(root, run_id)
    if warp_run_deliverable._halted(root, run_id):
        held = extra & warp_run_deliverable.halted_withheld_paths(root, run_id)
        rest = extra - held
        if wanted is not None:
            rest &= wanted
        return held | (rest & dirty()) if dirty is not None and rest else held
    return extra


def _dirty_reader(root: Path, wanted: set):
    """No-arg callable: the `wanted` paths dirty in the tree (tracked-modified or untracked), from
    one `git status --porcelain` through the shared seam, memoised so every sibling shares it."""
    cache: list = []

    def read() -> set:
        if not cache:
            from coordinator_core.git.run import run_git

            found: set = set()
            result = run_git(
                ["-C", str(root), "--literal-pathspecs", "status", "--porcelain", "-z",
                 "--untracked-files=all", "--", *sorted(wanted)],
            )
            if result.ok:
                # -z v1: "XY path"; a rename/copy entry is followed by a bare source-path token
                # with no XY prefix -- both sides are dirty, and slicing [3:] off it is wrong.
                tokens = iter(result.stdout.split("\0"))
                for entry in tokens:
                    if len(entry) > 3:
                        found.add(_norm(entry[3:]))
                        if "R" in entry[:2] or "C" in entry[:2]:
                            found.add(_norm(next(tokens, "")))
                found.discard("")
            cache.append(found)
        return cache[0]

    return read


def _declares(root: Path, run_id: str, path: str) -> bool:
    """Whether `run_id`'s manifest or commit request declares `path` (exactly or under a prefix)."""
    from coordinator_core.ops import warp_run_deliverable

    paths, prefixes, _unreadable = warp_run_deliverable._manifest_declared(run_id, root)
    return path in paths or any(path == p or path.startswith(p + "/") for p in prefixes)


def sibling_run_claims(
    worktree_root: Path, own_run_id: Optional[str], candidates: Iterable[str], *,
    own_plan: Optional[str] = None, minted_before: Optional[tuple] = None,
    own_declared: Optional[Iterable[str]] = None,
) -> dict:
    """`{path: owning run id}` for each of `candidates` another unlanded run claims, paths
    forward-slash normalised. The claim lookup is the one `terminal_commit._live_run_claims`
    makes (one live-token listing, one halted-record scan, both excluding `own_run_id`); this
    adds no token-store listing of its own. A live claim is named by the unlanded run that
    declares it, else by its holding session id. First claimant wins. Never raises.

    A halted run claims what it withholds plus the manifest/spine/extra paths it declares that are
    dirty now (one shared status read, spawned only when such a candidate exists). A halted run
    whose only declaration of a path is its plan spine's `writes` does not claim it dirty when
    `own_declared` holds it: the landing run's own write is not that run's dirt."""
    root = Path(worktree_root)
    wanted = {_norm(p) for p in candidates if p}
    wanted.discard("")
    claims: dict = {}
    if not wanted:
        return claims
    dirty = _dirty_reader(root, wanted)
    own = {_norm(p) for p in own_declared or () if p}
    try:
        from coordinator_core.ops import warp_run_deliverable

        halted = warp_run_deliverable.halted_run_write_claims(
            root, wanted, exclude_run=own_run_id, own_plan=own_plan, dirty=dirty,
        ) or {}
        live = warp_run_deliverable.live_run_write_claims(
            root, wanted, exclude_session="", cwd=str(root), exclude_run=own_run_id, minted_before=minted_before,
        ) or {}
        for path, run_ids in sorted(halted.items()):
            claims.setdefault(path, run_ids[0])
        unlanded = _unlanded_run_ids(root, own_run_id, own_plan, minted_before)
        extras = {run_id: _held_extras(root, run_id, dirty, wanted) for run_id in unlanded}
        for path, sessions in sorted(live.items()):
            owner = next((r for r in unlanded if _declares(root, r, path)), sessions[0])
            claims.setdefault(path, owner)
        for run_id in unlanded:
            for path in sorted(extras[run_id] & wanted):
                claims.setdefault(path, run_id)
            if warp_run_deliverable._halted(root, run_id):
                paths, prefixes, _unreadable = warp_run_deliverable._manifest_declared(run_id, root)
                if not (paths or prefixes):
                    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

                    spine_all = peer_hunk_gate._spine_writes(root, run_id) & wanted
                    spine = spine_all - own
                    for path in sorted(spine & dirty()) if spine else ():
                        claims.setdefault(path, run_id)
                    freed = spine_all & own
                    if freed:
                        withheld = warp_run_deliverable.halted_withheld_paths(root, run_id)
                        for path in sorted((freed - withheld) & dirty()):
                            logging.getLogger(__name__).warning(
                                "spine-only halted sibling %s: dirty declared path %s freed for landing run %s",
                                run_id, path, own_run_id,
                            )
    except Exception:  # noqa: BLE001 -- contest unknown: claim nothing further
        return claims
    return claims


def shared_dir_holds(
    worktree_root: Path,
    own_run_id: Optional[str],
    own_paths: Iterable[str],
    candidates: Iterable[str],
    *,
    own_plan: Optional[str] = None,
) -> list:
    """The `candidates` sitting in a directory that both `own_paths` and an unlanded sibling run's
    declared paths occupy: a sibling's undeclared dirt there cannot be told from this session's own,
    so it is held whole (never hunk-split). Never raises."""
    wanted = [_norm(p) for p in candidates if p]
    if not wanted:
        return []
    own_dirs = {_norm(p).rpartition("/")[0] for p in own_paths if p}
    try:
        from coordinator_core.ops import warp_run_deliverable

        root = Path(worktree_root)
        held_dirs: set = set()
        for run_id in _unlanded_run_ids(root, own_run_id, own_plan):
            if warp_run_deliverable._halted(root, run_id):
                declared = tuple(warp_run_deliverable.halted_withheld_paths(root, run_id))
            else:
                paths, _prefixes, _unreadable = warp_run_deliverable._manifest_declared(run_id, root)
                declared = (*paths, *_extra_declared(root, run_id))
            held_dirs.update(p.rpartition("/")[0] for p in declared if isinstance(p, str) and p)
    except Exception:  # noqa: BLE001 -- contest unknown: hold nothing further
        return []
    shared = own_dirs & held_dirs
    return [p for p in wanted if p.rpartition("/")[0] in shared]


def filter_own(
    paths: Iterable[str], claims: Mapping[str, str], own_declared: Iterable[str],
) -> tuple:
    """`(kept, withheld)`: `paths` minus those a sibling claims, except a path the run itself
    declares. `withheld` maps each dropped path to its claimant run id. Order is preserved."""
    own = {_norm(p) for p in own_declared if p}
    kept: list = []
    withheld: dict = {}
    for path in paths:
        key = _norm(path)
        owner = claims.get(key)
        if owner is not None and key not in own:
            withheld[path] = owner
        else:
            kept.append(path)
    return kept, withheld

"""Which live warp run's write footprint intersects a set of writes? JSON reads only, no spawn.

A live run is a run-token record that `warp_live` reads as not ended. Its footprint is the staged
`manifest.json` in its run dir: every row's `writes`, `review_declared_paths` and `doc_writes`.
A run that has not staged has no manifest: its footprint is unknown (None), and `overlaps` never
reports it, so a submit-time caller does not refuse on a run it cannot read.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Sequence

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT


def _norm(path: str) -> PurePosixPath:
    return PurePosixPath(path.replace("\\", "/").strip("/"))


def _footprint(root: Path, run_id: str) -> Optional[frozenset[str]]:
    manifest = warp_live._read_json(root / RUN_DIR_ROOT / run_id / "manifest.json")
    if manifest is None:
        return None
    paths: set[str] = set()
    for row in manifest.get("rows") or ():
        if isinstance(row, dict):
            paths.update(w for w in row.get("writes") or () if isinstance(w, str))
    for key in ("review_declared_paths", "doc_writes"):
        paths.update(p for p in manifest.get(key) or () if isinstance(p, str))
    return frozenset(paths)


def live_footprints(root: Path, *, exclude_run_id: Optional[str]) -> dict[str, Optional[frozenset[str]]]:
    """One entry per live run (excluding `exclude_run_id`): its staged write paths, or None when
    it has not staged a manifest."""
    root = Path(root)
    dirs = warp_live._launch_dirs(None)
    out: dict[str, Optional[frozenset[str]]] = {}
    for record in warp_live._token_records(root):
        run_id = record["run_id"]
        if run_id == exclude_run_id or run_id in out:
            continue
        if warp_live._record_live(root, record, None, None, dirs):
            out[run_id] = _footprint(root, run_id)
    return out


def overlaps(writes: Sequence[str], footprints: Mapping[str, Optional[frozenset[str]]]) -> dict[str, list[str]]:
    """Run id -> the footprint paths that equal, contain or sit under one of `writes`. A None
    footprint, and a run with no overlap, are absent."""
    mine = {_norm(w) for w in writes if w}
    mine_ancestors = {a for w in mine for a in w.parents}
    cache: dict[str, bool] = {}

    def touches(p: str) -> bool:
        hit = cache.get(p)
        if hit is None:
            n = _norm(p)
            hit = n in mine or n in mine_ancestors or any(a in mine for a in n.parents)
            cache[p] = hit
        return hit

    found: dict[str, list[str]] = {}
    for run_id, footprint in footprints.items():
        if footprint is None:
            continue
        hit = sorted(p for p in footprint if touches(p))
        if hit:
            found[run_id] = hit
    return found

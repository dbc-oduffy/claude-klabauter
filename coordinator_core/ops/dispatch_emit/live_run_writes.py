"""Which live warp run's write footprint intersects a set of writes? JSON reads only, no spawn.

A live run is a run-token record that `warp_live` reads as not ended. Its footprint is the staged
`manifest.json` in its run dir: every row's `writes`, `review_declared_paths` and `doc_writes`.
A run that has not staged has no manifest: its footprint is unknown (None), and `overlaps` never
reports it, so a submit-time caller does not refuse on a run it cannot read.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Optional, Sequence

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT


def _norm(path: str) -> PurePosixPath:
    return PurePosixPath(path.replace("\\", "/").strip("/"))


def _footprint(root: Path, run_id: str) -> Optional[frozenset[str]]:
    manifest = warp_live._read_json(root / RUN_DIR_ROOT / run_id / "manifest.json")
    return None if manifest is None else manifest_footprint(manifest)


def manifest_footprint(manifest: Mapping) -> frozenset[str]:
    """Every row's `writes`, `review_declared_paths` and `doc_writes` of a staged manifest."""
    paths: set[str] = set()
    for row in manifest.get("rows") or ():
        if isinstance(row, dict):
            paths.update(w for w in row.get("writes") or () if isinstance(w, str))
    for key in ("review_declared_paths", "doc_writes"):
        paths.update(p for p in manifest.get(key) or () if isinstance(p, str))
    return frozenset(paths)


_INDEX_LOCK_SECONDS = 2.0


@contextlib.contextmanager
def _index_lock(root: Path) -> Iterator[None]:
    """Cross-process exclusion for an index read-modify-write, so a peer's concurrent add or
    remove is never overwritten. A lock that cannot be taken in time proceeds unlocked: a late
    index beats a lost run."""
    from coordinator_core import locked_write
    from coordinator_core.ops.dispatch_emit import live_run_index

    path = live_run_index.index_path(root)
    stack = contextlib.ExitStack()
    if path is not None:
        try:
            stack.enter_context(locked_write.held_lock(path, timeout=_INDEX_LOCK_SECONDS))
        except (locked_write.LockTimeout, locked_write.LockAnchorError, OSError):
            pass
    with stack:
        yield


def index_mint(root: Path, run_id: str, minted_at: float) -> None:
    """Index a freshly minted run with an unknown footprint, replacing any entry a prior mint of
    the same run id left. Never raises: the index is a read cache of the token records."""
    from coordinator_core.ops.dispatch_emit import live_run_index

    try:
        with _index_lock(root):
            live_run_index.remove(root, run_id)
            live_run_index.add(root, run_id, minted_at)
    except Exception:  # noqa: BLE001
        pass


def index_stage(root: Path, run_id: str, manifest: Mapping, minted_at: Optional[float] = None) -> None:
    """Record a run's staged footprint. Never raises."""
    from coordinator_core.ops.dispatch_emit import live_run_index

    try:
        with _index_lock(root):
            live_run_index.stage(root, run_id, manifest_footprint(manifest), minted_at)
    except Exception:  # noqa: BLE001
        pass


def index_remove(root: Path, run_id: str) -> None:
    """Drop a consumed or discarded run. Never raises."""
    from coordinator_core.ops.dispatch_emit import live_run_index

    try:
        with _index_lock(root):
            live_run_index.remove(root, run_id)
    except Exception:  # noqa: BLE001
        pass


def prune_index(root: Path, *, deep: bool = False, now: Optional[float] = None) -> list[str]:
    """Drop index entries whose run is over; return the dropped run ids. Background use only.

    An entry with no submission record goes once `warp_live.UNLAUNCHED_GRACE_SECONDS` have passed
    since `minted_at`; a launched entry (one with a submission record) stays however old, until
    consume, discard or an ended marker removes it. `deep` also walks the transcript-tail
    liveness (`warp_live._record_live`, which consumes on evidence and so removes the entry)."""
    from coordinator_core.ops.dispatch_emit import live_run_index

    root = Path(root)
    path = live_run_index.index_path(root)
    if path is None:
        return []
    now = time.time() if now is None else now
    dropped: list[str] = []
    runs = live_run_index._load(path)
    records = {r["run_id"]: r for r in warp_live._token_records(root)} if deep else {}
    dirs = warp_live._launch_dirs(None) if deep else []
    for run_id, entry in runs.items():
        minted = entry.get("minted_at") if isinstance(entry, dict) else None
        launched = warp_live._read_json(warp_live._submission_path(root, run_id)) is not None
        over = warp_live._holds_ended_marker(root, run_id)
        if not over and not launched:
            over = isinstance(minted, (int, float)) and now - minted > warp_live.UNLAUNCHED_GRACE_SECONDS
        if not over and deep and launched:
            record = records.get(run_id)
            over = record is None or not warp_live._record_live(root, record, None, None, dirs)
        if over:
            dropped.append(run_id)
    for run_id in dropped:
        index_remove(root, run_id)
    return dropped


def live_footprints(root: Path, *, exclude_run_id: Optional[str]) -> dict[str, Optional[frozenset[str]]]:
    """One entry per live run (excluding `exclude_run_id`): its staged write paths, or None when
    it has not staged a manifest."""
    root = Path(root)
    dirs = warp_live._launch_dirs(None)
    out: dict[str, Optional[frozenset[str]]] = {}
    with warp_live.listing_scope():
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

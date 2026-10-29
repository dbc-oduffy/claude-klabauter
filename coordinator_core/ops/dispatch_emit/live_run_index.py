"""The live-run index: one JSON file under the git common dir mapping run_id -> footprint.

Replaces a per-submit walk of run-token records and launch dirs with a single bounded read. A run
that has not staged has a None footprint, which `overlaps` never reports (as `live_run_writes`).
Reads go through `halt_resume_record.bounded_read`; no git, no directory listing.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops.dispatch_emit import live_run_writes
from coordinator_core.ops.dispatch_emit.halt_resume_record import MISSING, STALLED, bounded_read
from coordinator_core.ops.dispatch_emit.run_token import _common_dir

INDEX_FILE = "coordinator-warp-live-run-index.json"
SCHEMA = "live-run-index/1"
WRITE_DEADLINE_MS = 2000.0

_write_lock = threading.Lock()


def index_path(root: Union[str, Path]) -> Optional[Path]:
    common = _common_dir(Path(root))
    return None if common is None else common / INDEX_FILE


def _load(path: Path) -> dict:
    raw = bounded_read(path, WRITE_DEADLINE_MS)
    if raw is MISSING or raw is STALLED:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    runs = data.get("runs") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    return dict(runs) if isinstance(runs, dict) else {}


def _store(path: Path, runs: dict) -> None:
    payload = {"schema": SCHEMA, "runs": runs}
    atomic_write_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _mutate(root: Union[str, Path], fn) -> bool:
    path = index_path(root)
    if path is None:
        return False
    with _write_lock:
        runs = _load(path)
        fn(runs)
        _store(path, runs)
    return True


def add(root: Union[str, Path], run_id: str, minted_at: float) -> bool:
    """Record a minted run with an unknown (None) footprint. False when there is no common dir."""
    return _mutate(root, lambda runs: runs.setdefault(run_id, {"footprint": None, "minted_at": minted_at}))


def stage(root: Union[str, Path], run_id: str, footprint: Iterable[str], minted_at: Optional[float] = None) -> bool:
    """Set a run's staged footprint (sorted, deduplicated); creates the entry if absent."""

    def apply(runs: dict) -> None:
        entry = runs.get(run_id) or {"minted_at": minted_at}
        entry["footprint"] = sorted(set(footprint))
        runs[run_id] = entry

    return _mutate(root, apply)


def remove(root: Union[str, Path], run_id: str) -> bool:
    """Drop a run (consumed, released or ended)."""
    return _mutate(root, lambda runs: runs.pop(run_id, None))


def read(root: Union[str, Path], deadline_ms: float, *, exclude_run_id: Optional[str] = None):
    """run_id -> frozenset footprint (None when unstaged), or `MISSING` / `STALLED`."""
    path = index_path(root)
    if path is None:
        return MISSING
    raw = bounded_read(path, deadline_ms)
    if raw is MISSING or raw is STALLED:
        return raw
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return MISSING
    runs = data.get("runs") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    if not isinstance(runs, dict):
        return MISSING
    out: dict[str, Optional[frozenset]] = {}
    for run_id, entry in runs.items():
        if run_id == exclude_run_id or not isinstance(entry, dict):
            continue
        fp = entry.get("footprint")
        out[run_id] = frozenset(p for p in fp if isinstance(p, str)) if isinstance(fp, list) else None
    return out


def overlaps(writes: Sequence[str], index: Mapping[str, Optional[frozenset]]) -> dict[str, list[str]]:
    """Run id -> overlapping footprint paths; the same answer as `live_run_writes.overlaps`."""
    return live_run_writes.overlaps(writes, index)

"""The live-run index: one JSON file under the settings-home engine record dir mapping run_id -> footprint.
Reads fall back to the legacy in-`.git` file only while the new one is missing; writes go to the new one.

Replaces a per-submit walk of run-token records and launch dirs with a single bounded read. A run
that has not staged has a None footprint, which `overlaps` never reports (as `live_run_writes`).
Reads go through `halt_resume_record.bounded_read`; no git, no directory listing.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence, Union

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import live_run_writes
from coordinator_core.ops.dispatch_emit.halt_resume_record import (
    MISSING,
    STALLED,
    bounded_read,
    bounded_write,
    write_outstanding,
)
from coordinator_core.ops.dispatch_emit.engine_record_home import engine_record_dir, legacy_record_dir

INDEX_FILE = "coordinator-warp-live-run-index.json"
CLAIMS_DIR = "coordinator-warp-live-claims"
SCHEMA = "live-run-index/1"
WRITE_DEADLINE_MS = 2000.0

_write_lock = threading.Lock()


def index_path(root: Union[str, Path]) -> Optional[Path]:
    record_dir = engine_record_dir(Path(root))
    return None if record_dir is None else record_dir / INDEX_FILE


def _bounded_read_with_fallback(root: Union[str, Path], path: Path, deadline_ms: float):
    """Read the settings-home index; only when it is MISSING, read the legacy in-`.git` file."""
    raw = bounded_read(path, deadline_ms)
    if raw is not MISSING:
        return raw
    legacy = legacy_record_dir(Path(root))
    return raw if legacy is None else bounded_read(legacy / INDEX_FILE, deadline_ms)


def _load(root: Union[str, Path], path: Path) -> dict:
    raw = _bounded_read_with_fallback(root, path, WRITE_DEADLINE_MS)
    if raw is MISSING or raw is STALLED:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    runs = data.get("runs") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    return dict(runs) if isinstance(runs, dict) else {}


def _store(path: Path, runs: dict) -> bool:
    """False when the write outlived `WRITE_DEADLINE_MS` (it may still land later)."""
    payload = {"schema": SCHEMA, "runs": runs}
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return bounded_write(path, data, WRITE_DEADLINE_MS) is not STALLED


_NO_STORE = object()


def _mutate(root: Union[str, Path], fn) -> bool:
    """Run `fn(runs)` under the index lock and store. `fn` returning `_NO_STORE` skips the store (True)."""
    path = index_path(root)
    if path is None:
        return False
    with _write_lock:
        if write_outstanding(path):
            return False
        runs = _load(root, path)
        if fn(runs) is _NO_STORE:
            return True
        return _store(path, runs)


def claim(
    root: Union[str, Path],
    run_id: str,
    writes: Iterable[str],
    ended: Optional[Callable[[str], bool]] = None,
) -> Optional[dict[str, list[str]]]:
    """Atomically claim `writes` as this run's footprint unless another indexed run's footprint overlaps.

    Overlapping runs for which `ended(run_id)` holds (default `warp_live.run_is_over`) are dropped from
    the hits, per hit only: the index is pruned in the background, so an ended run may still be listed.

    Returns `{}` when claimed, the overlap hits (run id -> paths, nothing written) when refused, and
    `None` when the claim could not be made atomically (no common dir, cross-process lock not taken,
    write outstanding, stalled store): the caller falls back to its read-only advisory check.
    Takes the cross-process index lock itself, so never call it inside `live_run_writes._index_lock`
    (held_lock is not re-entrant). A None footprint (minted, unstaged) never blocks."""
    from coordinator_core import locked_write

    path = index_path(root)
    if path is None:
        return None
    mine = sorted({w for w in writes if isinstance(w, str) and w})
    over = ended if ended is not None else lambda rid: run_holds_no_claim(Path(root), rid)
    claims = _claims_dir(path)
    created: list[Path] = []
    try:
        hits = _claim_markers(claims, run_id, mine, over, created)
    except OSError:
        _unlink_all(created)
        return None
    if hits:
        _unlink_all(created)
        return hits

    def apply(runs: dict):
        others = {
            rid: frozenset(p for p in e["footprint"] if isinstance(p, str))
            for rid, e in runs.items()
            if rid != run_id and isinstance(e, dict) and isinstance(e.get("footprint"), list)
        }
        found = overlaps(mine, others)
        if found:
            hits.update({rid: p for rid, p in found.items() if not over(rid)})
        if hits:
            return _NO_STORE
        entry = runs.get(run_id)
        entry = dict(entry) if isinstance(entry, dict) else {"minted_at": None}
        entry["footprint"] = mine
        runs[run_id] = entry
        return None

    try:
        with locked_write.held_lock(path, timeout=live_run_writes._INDEX_LOCK_SECONDS):
            stored = _mutate(root, apply)
    except (locked_write.LockTimeout, locked_write.LockAnchorError, OSError):
        stored = False
    if not stored:
        _unlink_all(created)
        return None
    if hits:
        _unlink_all(created)
    return hits


STALE_RUN_SECONDS = 24 * 3600.0


def _epoch(stamp: object) -> Optional[float]:
    """Epoch seconds of an ISO-8601 `...Z` stamp; None when absent or malformed."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def run_is_stale(root: Union[str, Path], run_id: str, now: Optional[float] = None) -> bool:
    """True when the run's owner session has exited (`warp_live._owner_exited`) and its last activity
    (`run-stage.json` `last_activity_at`, else the token's or index entry's `minted_at`) is more than
    `STALE_RUN_SECONDS` old. An unknown owner or an unknown activity time answers False; never raises."""
    try:
        from coordinator_core.ops.dispatch_emit import run_token

        root = Path(root)
        record = run_token._read_rec(root, run_id) or {}
        submission = warp_live._read_json(warp_live._submission_path(root, run_id)) or {}
        owner = submission.get("session_id") or record.get("session_id")
        if not (isinstance(owner, str) and owner):
            return False
        stage = warp_live._read_json(warp_live._ask_record_dir(root, run_id) / "run-stage.json") or {}
        minted = record.get("minted_at")
        if not isinstance(minted, (int, float)):
            path = index_path(root)
            entry = _load(root, path).get(run_id) if path is not None else None
            minted = entry.get("minted_at") if isinstance(entry, dict) else None
        if not isinstance(minted, (int, float)):
            minted = None
        active = _epoch(stage.get("last_activity_at"))
        if active is None:
            active = minted
        if active is None:
            return False
        now = time.time() if now is None else now
        return now - active > STALE_RUN_SECONDS and warp_live._owner_exited(owner, float(minted or active))
    except Exception:  # noqa: BLE001
        return False


def run_holds_no_claim(root: Union[str, Path], run_id: str) -> bool:
    """`warp_live.run_is_over`, a run whose token is spent or retired (`ask_gate.token_retired`), or a
    stale run (`run_is_stale`): none blocks another run's footprint."""
    if warp_live.run_is_over(Path(root), run_id):
        return True
    from coordinator_core.ops.dispatch_emit import ask_gate

    try:
        if ask_gate.token_retired(Path(root), run_id):
            return True
    except Exception:  # noqa: BLE001
        pass
    return run_is_stale(root, run_id)


def _claims_dir(index_file: Path) -> Path:
    return index_file.parent / CLAIMS_DIR


def _unlink_all(paths: Iterable[Path]) -> None:
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


def _read_marker(p: Path) -> Optional[tuple[str, str]]:
    """(run_id, path) of a claim marker; None when gone or still unwritten after a short wait."""
    for _ in range(10):
        try:
            data = json.loads(p.read_bytes().decode("utf-8"))
            return str(data["run"]), str(data["path"])
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, OSError):
            time.sleep(0.005)
    return None


ABANDONED_MARKER_SECONDS = 30.0


def _marker_is_abandoned(p: Path) -> bool:
    """A marker still unreadable this long after creation belongs to a claimer that died between
    create and write; left alone it would refuse its path forever."""
    try:
        return time.time() - p.stat().st_mtime > ABANDONED_MARKER_SECONDS
    except OSError:
        return False


def _claim_markers(
    claims: Path, run_id: str, mine: Sequence[str], over: Callable[[str], bool], created: list[Path]
) -> dict[str, list[str]]:
    """Create one O_EXCL marker per path of `mine`, then scan every other holder's markers.

    The exclusive create decides an identical path; creating before scanning means two overlapping
    claimers cannot both miss each other (at least one refuses). Markers of ended holders are
    removed. Appends the markers this call created to `created`; the caller unlinks them on a hit."""
    claims.mkdir(parents=True, exist_ok=True)
    wanted = {marker_name(w): w for w in mine}
    for name, rel in wanted.items():
        target = claims / name
        body = json.dumps({"run": run_id, "path": rel}).encode("utf-8")
        for attempt in (0, 1):
            try:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = _read_marker(target)
                dead = _marker_is_abandoned(target) if holder is None else holder[0] != run_id and over(holder[0])
                if attempt == 0 and dead:
                    _unlink_all([target])
                    continue
                break
            try:
                os.write(fd, body)
            finally:
                os.close(fd)
            created.append(target)
            break
    held: dict[str, set[str]] = {}
    markers: dict[Path, str] = {}
    for p in claims.iterdir():
        read_back = _read_marker(p)
        if read_back is None:
            if p.name in wanted and not p.exists():
                continue
            if _marker_is_abandoned(p):
                _unlink_all([p])
            elif p.name in wanted:
                held.setdefault("?", set()).add(wanted[p.name])
            continue
        rid, rel = read_back
        if rid == run_id:
            if p.name not in wanted:
                _unlink_all([p])
            continue
        held.setdefault(rid, set()).add(rel)
        markers[p] = rid
    found = overlaps(mine, {rid: frozenset(paths) for rid, paths in held.items()})
    hits: dict[str, list[str]] = {}
    for rid, paths in found.items():
        if rid != "?" and over(rid):
            _unlink_all(p for p, owner in markers.items() if owner == rid)
        else:
            hits[rid] = paths
    return hits


def marker_name(path: str) -> str:
    """Claim-marker file name for a path: a digest of its normalised form."""
    norm = live_run_writes._norm(path).as_posix()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:32]


def release(root: Union[str, Path], run_id: str) -> int:
    """Unlink every claim marker held by `run_id`; returns how many were held. Never raises."""
    path = index_path(root)
    if path is None:
        return 0
    claims = _claims_dir(path)
    count = 0
    try:
        for p in claims.iterdir():
            held = _read_marker(p)
            if held is not None and held[0] == run_id:
                _unlink_all([p])
                count += 1
    except OSError:
        pass
    return count


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
    """Drop a run (consumed, released or ended) and the claim markers it holds."""
    release(root, run_id)
    return _mutate(root, lambda runs: runs.pop(run_id, None))


def read(root: Union[str, Path], deadline_ms: float, *, exclude_run_id: Optional[str] = None):
    """run_id -> frozenset footprint (None when unstaged), or `MISSING` / `STALLED`."""
    path = index_path(root)
    if path is None:
        return MISSING
    raw = _bounded_read_with_fallback(root, path, deadline_ms)
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

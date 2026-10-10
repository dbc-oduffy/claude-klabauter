"""Heavy-admission lease store: one JSON file per (pid, creation_time) lease under settings_home().

Leases are written atomically, reaped when their holder dies (creation-time liveness, so a reused
pid does not keep one alive) or when they stay unattributed past LEASE_ATTRIBUTION_TTL_S, and
rendered for the deny text. Process liveness comes from a ProcessPrimitives parameter.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from coordinator_core._settings_home import settings_home
from coordinator_core.bash_guards._heavy_admission_contract import (
    CLASS_IMAGES,
    LEASE_ATTRIBUTION_TTL_S,
    LeaseRecord,
    ProcessPrimitives,
)


def leases_dir() -> Path:
    return settings_home() / "heavy-admission" / "leases"


def _is_unattributed(rec: LeaseRecord) -> bool:
    return rec.holder_pid == rec.session_pid and rec.holder_ctime == rec.session_ctime


def _write_atomic(path: Path, rec: LeaseRecord) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(asdict(rec)), encoding="utf-8")
    os.replace(tmp, path)


def write_lease(rec: LeaseRecord, directory: Optional[Path] = None) -> Path:
    """Persist rec as a new lease file and return its path."""
    d = directory or leases_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rec.holder_pid}-{rec.holder_ctime}-{uuid.uuid4().hex[:12]}.json"
    _write_atomic(path, rec)
    return path


def _read(path: Path) -> Optional[LeaseRecord]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LeaseRecord(
            holder_pid=int(raw["holder_pid"]),
            holder_ctime=int(raw["holder_ctime"]),
            session_pid=int(raw["session_pid"]),
            session_ctime=int(raw["session_ctime"]),
            heavy_class=str(raw["heavy_class"]),
            admitted_at=float(raw["admitted_at"]),
            launch_ctime=int(raw.get("launch_ctime", 0)),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _entries(directory: Path) -> List[tuple]:
    try:
        files = sorted(p for p in directory.iterdir() if p.suffix == ".json")
    except OSError:
        return []
    return [(p, _read(p)) for p in files]


def reap(
    primitives: ProcessPrimitives,
    directory: Optional[Path] = None,
    now: Optional[Callable[[], float]] = None,
) -> int:
    """Delete corrupt leases, dead-holder leases, and unattributed leases past the TTL; return the count removed."""
    d = directory or leases_dir()
    clock = (now or time.time)()
    removed = 0
    for path, rec in _entries(d):
        if rec is None:
            drop = True
        elif not primitives.alive(rec.holder_pid, rec.holder_ctime):
            drop = True
        else:
            drop = _is_unattributed(rec) and clock - rec.admitted_at > LEASE_ATTRIBUTION_TTL_S
        if drop:
            _remove(path)
            removed += 1
    return removed


def live_leases(directory: Optional[Path] = None) -> List[LeaseRecord]:
    """Parseable leases as stored; call reap first so only live ones remain."""
    return [rec for _, rec in _entries(directory or leases_dir()) if rec is not None]


def unattributed_count(
    directory: Optional[Path] = None,
    now: Optional[Callable[[], float]] = None,
) -> int:
    """Leases still unattributed and inside the TTL window: the ones the RAM leg reserves for."""
    clock = (now or time.time)()
    return sum(
        1
        for rec in live_leases(directory)
        if _is_unattributed(rec) and clock - rec.admitted_at <= LEASE_ATTRIBUTION_TTL_S
    )


def holders(directory: Optional[Path] = None) -> List[str]:
    """One rendered line per stored lease, for the deny text."""
    return [
        f"pid {r.holder_pid} ({r.heavy_class}, session pid {r.session_pid})"
        + (" unattributed" if _is_unattributed(r) else "")
        for r in live_leases(directory)
    ]


def attribute(
    session_pid: int,
    session_ctime: int,
    holder_pid: int,
    holder_ctime: int,
    directory: Optional[Path] = None,
) -> int:
    """Narrow the session's unattributed leases to the heavy descendant the census found; return the count narrowed."""
    narrowed = 0
    for path, rec in _entries(directory or leases_dir()):
        if rec is None or not _is_unattributed(rec):
            continue
        if (rec.session_pid, rec.session_ctime) != (session_pid, session_ctime):
            continue
        new = LeaseRecord(
            holder_pid=holder_pid,
            holder_ctime=holder_ctime,
            session_pid=rec.session_pid,
            session_ctime=rec.session_ctime,
            heavy_class=rec.heavy_class,
            admitted_at=rec.admitted_at,
            launch_ctime=rec.launch_ctime,
        )
        _write_atomic(path, new)
        narrowed += 1
    return narrowed


def oldest_open_launch(
    directory: Optional[Path] = None,
    now: Optional[Callable[[], float]] = None,
) -> Optional[int]:
    """The earliest launch mark among unattributed in-TTL leases that carry one, or None."""
    clock = (now or time.time)()
    marks = [
        rec.launch_ctime
        for rec in live_leases(directory)
        if _is_unattributed(rec) and rec.launch_ctime > 0 and clock - rec.admitted_at <= LEASE_ATTRIBUTION_TTL_S
    ]
    return min(marks) if marks else None


def claim_orphans(
    trees: Sequence,
    window: int,
    directory: Optional[Path] = None,
    now: Optional[Callable[[], float]] = None,
) -> int:
    """Attribute each open lease to the orphan tree created soonest after its launch mark, within
    window ctime units, holding an image of its class, and not already held; return the count.

    Leases claim in launch order, one tree each, box-wide: an orphan has no session, so the
    launch mark is the only link. TRAP: two leases launched inside the same instant can swap
    trees; each session still counts one.
    """
    clock = (now or time.time)()
    entries = _entries(directory or leases_dir())
    held = {(r.holder_pid, r.holder_ctime) for _, r in entries if r is not None and not _is_unattributed(r)}
    open_ = sorted(
        (
            (path, r) for path, r in entries
            if r is not None and _is_unattributed(r) and r.launch_ctime > 0
            and clock - r.admitted_at <= LEASE_ATTRIBUTION_TTL_S
        ),
        key=lambda pr: pr[1].launch_ctime,
    )
    claimed = 0
    for path, rec in open_:
        family = CLASS_IMAGES.get(rec.heavy_class, frozenset())
        fits = [
            t for t in trees
            if (t.root.pid, t.root.ctime) not in held
            and rec.launch_ctime <= t.root.ctime <= rec.launch_ctime + window
            and family & t.stems
        ]
        if not fits:
            continue
        tree = min(fits, key=lambda t: t.root.ctime)
        held.add((tree.root.pid, tree.root.ctime))
        _write_atomic(path, LeaseRecord(
            holder_pid=tree.root.pid,
            holder_ctime=tree.root.ctime,
            session_pid=rec.session_pid,
            session_ctime=rec.session_ctime,
            heavy_class=rec.heavy_class,
            admitted_at=rec.admitted_at,
            launch_ctime=rec.launch_ctime,
        ))
        claimed += 1
    return claimed

"""Remote roster snapshots: the last-known live sessions of each non-local box, read from files.

One snapshot per box at ``<directory>/remote-roster/<box_id>.json`` (``directory`` is the primary's
``state/group-em``), shaped ``{box_id, fetched_at, ok, sessions: [{session_id, name, cwd, status,
address}]}``. ``read_snapshot`` is the watch-tick reader: one listdir plus one open per snapshot,
never a spawn. A stale, unreadable or ``ok=false`` snapshot reads ``reachable=False``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.group_em import box_registry

SNAPSHOT_DIR = "remote-roster"
SNAPSHOT_TTL_S = 120.0


def snapshot_path(directory: Path, box_id: str) -> Path:
    """Where ``box_id``'s snapshot lives; raises ValueError for an unsafe box id."""
    if not box_registry._valid_id(box_id):
        raise ValueError(f"invalid box id: {box_id!r}")
    return Path(directory) / SNAPSHOT_DIR / (box_id + ".json")


def _load(path: Path, now: float, ttl_s: float) -> dict:
    down = {"reachable": False, "sessions": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return down
    if not isinstance(data, dict) or data.get("ok") is not True:
        return down
    fetched = data.get("fetched_at")
    sessions = data.get("sessions")
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float)):
        return down
    if now - fetched > ttl_s or not isinstance(sessions, list):
        return down
    return {"reachable": True, "sessions": [s for s in sessions if isinstance(s, dict)]}


def read_snapshot(
    directory: Path,
    now: Optional[float] = None,
    ttl_s: float = SNAPSHOT_TTL_S,
    local_id: Optional[str] = None,
) -> dict:
    """``{box_id: {"reachable": bool, "sessions": [...]}}`` for every non-local snapshot file."""
    when = time.time() if now is None else now
    skip = local_id if local_id is not None else box_registry.local_box_id()
    base = Path(directory) / SNAPSHOT_DIR
    try:
        names = sorted(n for n in os.listdir(base) if n.endswith(".json"))
    except OSError:
        return {}
    out: dict = {}
    for name in names:
        box_id = name[: -len(".json")]
        if box_id == skip or not box_registry._valid_id(box_id):
            continue
        out[box_id] = _load(base / name, when, ttl_s)
    return out


REFRESH_INTERVAL_S = SNAPSHOT_TTL_S / 2
_FIELDS = ("session_id", "name", "cwd", "status", "address")


def _trim(rows: object) -> list:
    if not isinstance(rows, list):
        return []
    return [
        {k: row.get(k) for k in _FIELDS}
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("session_id"), str) and row["session_id"]
    ]


def _raw(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_snapshot(directory: Path, box_id: str, ok: bool, sessions: list, now: float) -> bool:
    from coordinator_core.atomic_replace import atomic_write_bytes

    record = {"box_id": box_id, "fetched_at": now, "ok": ok, "sessions": sessions}
    try:
        path = snapshot_path(directory, box_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return True


def _fetch_fs(box: dict, now: float, ttl_s: float) -> Optional[list]:
    """The far box's own published snapshot of itself, or None when absent, stale or not ok."""
    root = box.get("root")
    if not isinstance(root, str):
        return None
    loaded = _load(snapshot_path(Path(root) / "state" / "group-em", box["box_id"]), now, ttl_s)
    if not loaded["reachable"]:
        return None
    return _trim(loaded["sessions"])


def _fetch(directory: Path, box: dict, now: float, runner, ttl_s: float) -> Optional[list]:
    """Sessions of ``box`` via the transport ``node_transport.select`` picks; None on any failure.

    The fs transport is read here with the caller's clock and ttl (the seam call carries neither).
    """
    from coordinator_core.group_em import node_transport

    transport = node_transport.select(box, directory, runner)
    if transport.kind == "fs":
        return _fetch_fs(box, now, ttl_s)
    res = transport.list_sessions(box)
    if not res.get("ok"):
        return None
    return _trim(res.get("sessions"))


def refresh_box(
    directory: Path,
    box: dict,
    now: Optional[float] = None,
    runner=None,
    ttl_s: float = SNAPSHOT_TTL_S,
) -> bool:
    """Fetch ``box`` and write its snapshot under ``directory``; True iff the fetch succeeded.

    The fetch dispatches through ``node_transport.select``, so every node refreshes any registered
    peer the same way. A failure writes ``ok=false`` carrying the prior sessions forward (they
    are never deleted) and never raises. Spawns for ssh boxes: call off the tick.
    """
    when = time.time() if now is None else now
    try:
        box_id = box["box_id"]
        fetched = _fetch(directory, box, when, runner, ttl_s)
        if fetched is not None:
            return _write_snapshot(directory, box_id, True, fetched, when)
        prior = _raw(snapshot_path(directory, box_id))
        kept = _trim(prior.get("sessions")) if prior else []
        _write_snapshot(directory, box_id, False, kept, when)
    except Exception:  # noqa: BLE001 -- a refresh failure must never reach the watch loop
        return False
    return False


def publish_local(
    directory: Path,
    sessions: Optional[list] = None,
    now: Optional[float] = None,
    local_id: Optional[str] = None,
) -> bool:
    """Write this box's own live roster as its snapshot (the fs source other boxes read).

    ``sessions`` defaults to the box-wide harness roster. A box with no identity (``local``)
    publishes nothing. Never raises.
    """
    when = time.time() if now is None else now
    box_id = local_id if local_id is not None else box_registry.local_box_id()
    if box_id == box_registry.LOCAL_BOX_ID:
        return False
    try:
        if sessions is None:
            import dataclasses

            from coordinator_core.session import peer_roster

            sessions = [dataclasses.asdict(r) for r in peer_roster.build_roster(box_wide=True)]
        return _write_snapshot(directory, box_id, True, _trim(sessions), when)
    except Exception:  # noqa: BLE001
        return False


def refresh_due(
    directory: Path,
    now: Optional[float] = None,
    interval_s: float = REFRESH_INTERVAL_S,
    local_id: Optional[str] = None,
) -> bool:
    """True when this box's own snapshot or any registered non-local box's is older than ``interval_s``."""
    when = time.time() if now is None else now
    skip = local_id if local_id is not None else box_registry.local_box_id()
    ids = [b["box_id"] for b in box_registry.read_boxes(directory) if b["box_id"] != skip]
    if skip != box_registry.LOCAL_BOX_ID:
        ids.append(skip)
    for box_id in ids:
        data = _raw(snapshot_path(directory, box_id))
        fetched = data.get("fetched_at") if data else None
        if isinstance(fetched, bool) or not isinstance(fetched, (int, float)):
            return True
        if when - fetched >= interval_s:
            return True
    return False


def refresh(
    directory: Path,
    now: Optional[float] = None,
    runner=None,
    local_sessions: Optional[list] = None,
    interval_s: float = REFRESH_INTERVAL_S,
    ttl_s: float = SNAPSHOT_TTL_S,
    local_id: Optional[str] = None,
) -> dict:
    """Refresh every due snapshot (each non-local box, then this box's own); ``{box_id: ok}``.

    Runs from watch's CLI loop in a background thread, never inside the tick.
    """
    when = time.time() if now is None else now
    skip = local_id if local_id is not None else box_registry.local_box_id()
    out: dict = {}

    def due(box_id: str) -> bool:
        data = _raw(snapshot_path(directory, box_id))
        fetched = data.get("fetched_at") if data else None
        if isinstance(fetched, bool) or not isinstance(fetched, (int, float)):
            return True
        return when - fetched >= interval_s

    for box in box_registry.read_boxes(directory):
        if box["box_id"] != skip and due(box["box_id"]):
            out[box["box_id"]] = refresh_box(directory, box, when, runner, ttl_s)
    if skip != box_registry.LOCAL_BOX_ID and due(skip):
        out[skip] = publish_local(directory, local_sessions, when, skip)
    return out

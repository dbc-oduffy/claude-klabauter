"""Box heartbeat: a whole-file liveness record a box stamps and any peer reads off its root.

``stamp`` writes ``<home>/state/group-em/box/heartbeat.json`` {box_id, ts, holder_session};
``read(box)`` takes a ``box_registry`` row and reads that one path under ``box["root"]``.
Separate from ``watch_heartbeat`` (a pinned cross-plane record, not edited). Leaf module:
never raises on I/O, and an absent, malformed or stale record reads not-live.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em.box_registry import local_box_id

TTL_S = 90.0


def _path(home: Optional[Path] = None) -> Path:
    if home is None:
        from coordinator_core._settings_home import settings_home

        home = settings_home()
    return Path(home) / "state" / "group-em" / "box" / "heartbeat.json"


def stamp(home: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """Whole-file write this box's heartbeat; False on I/O failure, never raises."""
    record = {
        "box_id": local_box_id(home),
        "ts": time.time() if now is None else now,
        "holder_session": os.environ.get("CLAUDE_SESSION_ID") or None,
    }
    path = _path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return True


def read(box: dict, now: Optional[float] = None) -> dict:
    """{box_id, live, age_s} for the registry row ``box``; live iff the record is within ``TTL_S``.

    A missing or unreadable record yields live False and age_s None. A ts in the future
    (clock skew between boxes) clamps age_s to 0.
    """
    box_id = box.get("box_id") if isinstance(box, dict) else None
    out = {"box_id": box_id, "live": False, "age_s": None}
    try:
        root = box["root"]
        with open(_path(root), "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        ts = float(data["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return out
    if isinstance(data.get("box_id"), str):
        out["box_id"] = data["box_id"]
    age = max(0.0, (time.time() if now is None else now) - ts)
    out["age_s"] = age
    out["live"] = age <= TTL_S
    return out

"""Box capacity record: a measured resource sample a box stamps and any peer reads off its root.

``stamp(sample, home)`` writes ``<home>/state/group-em/box/capacity.json``; ``read(box)`` takes a
``box_registry`` row and reads that one path under ``box["root"]``. Same transport as
``box_heartbeat``. Leaf module: never raises on I/O. An absent or malformed record reads
``stale`` with ``age_s`` None, never a zeroed sample.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em.box_registry import local_box_id

SCHEMA_VERSION = 1

# Mirrors watch._POLL_INTERVAL_CEILING_SECONDS (watch is not imported: leaf module); a test pins equality.
WATCH_TICK_CEILING_S = 300.0
# A record survives one missed tick at the slowest cadence before it reads stale.
TTL_S = 2.0 * WATCH_TICK_CEILING_S

_SCALAR_KEYS = (
    "os",
    "arch",
    "cores_physical",
    "cores_logical",
    "load_pct",
    "ram_total_mb",
    "ram_avail_mb",
)
_GPU_KEYS = ("index", "name", "vram_total_mib", "vram_used_mib", "util_pct")
_JOB_KEYS = ("run", "job_type", "session_id", "peak_ram_mb", "peak_vram_mib", "cores")
GPU_NOT_SAMPLED = "not-sampled"


def _path(home: Optional[Path] = None) -> Path:
    if home is None:
        from coordinator_core._settings_home import settings_home

        home = settings_home()
    return Path(home) / "state" / "group-em" / "box" / "capacity.json"


def _pick(row: object, keys: tuple) -> dict:
    return {k: row.get(k) for k in keys} if isinstance(row, dict) else {}


def build(sample: dict, home: Optional[Path] = None, now: Optional[float] = None) -> dict:
    """The record for ``sample``: schema keys only; ``gpus`` or ``gpu_unavailable``, never both."""
    sample = sample if isinstance(sample, dict) else {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "box_id": local_box_id(home),
        "ts": time.time() if now is None else now,
    }
    for key in _SCALAR_KEYS:
        record[key] = sample.get(key)
    gpus = sample.get("gpus")
    if isinstance(gpus, list):
        record["gpus"] = [_pick(g, _GPU_KEYS) for g in gpus]
    else:
        reason = sample.get("gpu_unavailable")
        record["gpu_unavailable"] = reason if isinstance(reason, str) and reason else GPU_NOT_SAMPLED
    jobs = sample.get("jobs")
    record["jobs"] = [_pick(j, _JOB_KEYS) for j in jobs] if isinstance(jobs, list) else []
    return record


def stamp(sample: dict, home: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """Atomically write this box's capacity record; False on I/O failure, never raises."""
    try:
        record = build(sample, home, now)
        path = _path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return True


def read(box: dict, now: Optional[float] = None) -> dict:
    """The record at ``box["root"]`` plus ``age_s`` and ``stale``; never raises.

    A missing or unreadable record yields ``{box_id, stale: True, age_s: None}``. A ts in the
    future (clock skew between boxes) clamps ``age_s`` to 0. ``stale`` is True past ``TTL_S``.
    """
    box_id = box.get("box_id") if isinstance(box, dict) else None
    absent = {"box_id": box_id, "stale": True, "age_s": None}
    try:
        with open(_path(box["root"]), "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        ts = float(data["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return absent
    if not isinstance(data, dict):
        return absent
    age = max(0.0, (time.time() if now is None else now) - ts)
    out = dict(data)
    if not isinstance(out.get("box_id"), str):
        out["box_id"] = box_id
    out["age_s"] = age
    out["stale"] = age > TTL_S
    return out

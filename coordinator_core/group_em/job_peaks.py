"""Job-peak store: measured per-job-type resource peaks, kept as the maximum seen per axis.

``<home>/state/group-em/box/job-peaks.json`` {job_type: {ram_mb, vram_mib, cores, samples,
measured_at, box_id}}. Measured values only: there is no declared field, and a job type never
measured reads ``None`` from ``peak``. Leaf module: never raises on I/O.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em.atomic_record import holder_lock
from coordinator_core.group_em.box_registry import local_box_id

AXES = ("ram_mb", "vram_mib", "cores")
_LOCK_TIMEOUT_S = 2.0


def _path(home: Optional[Path] = None) -> Path:
    if home is None:
        from coordinator_core._settings_home import settings_home

        home = settings_home()
    return Path(home) / "state" / "group-em" / "box" / "job-peaks.json"


def _load(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _measure(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value >= 0 else None


def record(
    job_type: str,
    measured: dict,
    home: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Fold one measured run into ``job_type``: max per axis, ``samples`` + 1. False if nothing recorded.

    ``measured`` carries any of ``ram_mb``, ``vram_mib``, ``cores``; an absent, negative or
    non-numeric axis is skipped, and a call with no usable axis records nothing. Never raises.
    """
    if not isinstance(job_type, str) or not job_type or not isinstance(measured, dict):
        return False
    values = {a: v for a in AXES if (v := _measure(measured.get(a))) is not None}
    if not values:
        return False
    path = _path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with holder_lock(path, timeout=_LOCK_TIMEOUT_S):
            store = _load(path)
            prior = store.get(job_type)
            entry = dict(prior) if isinstance(prior, dict) else {}
            for axis, value in values.items():
                old = _measure(entry.get(axis))
                entry[axis] = value if old is None else max(old, value)
            samples = entry.get("samples")
            entry["samples"] = (samples if isinstance(samples, int) else 0) + 1
            entry["measured_at"] = time.time() if now is None else now
            entry["box_id"] = local_box_id(home)
            store[job_type] = entry
            atomic_write_bytes(path, (json.dumps(store, sort_keys=True) + "\n").encode("utf-8"))
    except (OSError, ValueError, TypeError, RuntimeError):
        return False
    return True


def peak(job_type: str, home: Optional[Path] = None) -> Optional[dict]:
    """The recorded peak for ``job_type``, or None when never measured; never raises."""
    entry = _load(_path(home)).get(job_type) if isinstance(job_type, str) else None
    return dict(entry) if isinstance(entry, dict) else None

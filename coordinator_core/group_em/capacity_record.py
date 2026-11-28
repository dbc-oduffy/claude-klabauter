"""Box capacity record: a measured resource sample a box stamps and any peer reads off its root.

``stamp(sample, home)`` writes ``<home>/state/group-em/box/capacity.json``; ``read(box)`` takes a
``box_registry`` row and reads that one path under ``box["root"]``. Same transport as
``box_heartbeat``. Leaf module: never raises on I/O. An absent or malformed record reads
``stale`` with ``age_s`` None, never a zeroed sample.

The record also carries the box's CPU verdict (``saturated``, ``top_cpu``) and the memory governor's
(``mem_critical``, ``paused``, ``mem_ts``). ``stamp_memory`` merges the governor's verdict into the
record without touching ``ts``; ``stamp`` keeps the governor's fields. ``saturated_boxes`` reads the
remote boxes' verdicts with no ssh: a record under a root readable here, else the fleet live snapshot.
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
    "saturated",
    "ram_total_mb",
    "ram_avail_mb",
)
# Written by the memory governor (``stamp_memory``), never by the watch tick's sample.
_MEM_KEYS = ("mem_critical", "paused", "mem_ts")
TOP_N = 5
_TOP_KEYS = ("pid", "comm", "pct")
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


def _top(rows: object) -> list:
    """``[{pid, comm, pct}]`` from sample rows carrying ``pct`` or ``pcpu``, at most ``TOP_N``."""
    out = []
    for r in rows[:TOP_N] if isinstance(rows, list) else ():
        if isinstance(r, dict):
            row = _pick(r, _TOP_KEYS)
            if row["pct"] is None:
                row["pct"] = r.get("pcpu")
            out.append(row)
    return out


def _load(home: Optional[Path]) -> dict:
    """The raw record under ``home``, ``{}`` when absent or malformed."""
    try:
        with open(_path(home), "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def build(sample: dict, home: Optional[Path] = None, now: Optional[float] = None) -> dict:
    """The record for ``sample``: schema keys only; ``gpus`` or ``gpu_unavailable``, never both."""
    sample = sample if isinstance(sample, dict) else {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "box_id": local_box_id(home),
        "ts": time.time() if now is None else now,
    }
    for key in _SCALAR_KEYS + _MEM_KEYS:
        record[key] = sample.get(key)
    record["top_cpu"] = _top(sample.get("top_cpu"))
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
        if isinstance(sample, dict) and "mem_ts" not in sample:
            prior = _load(home)
            sample = {**sample, **{k: prior.get(k) for k in _MEM_KEYS}}
        return _write(build(sample, home, now), home)
    except (OSError, ValueError, TypeError):
        return False


def _write(record: dict, home: Optional[Path]) -> bool:
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    return True


def stamp_memory(critical: bool, paused: int, home: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """Merge the memory governor's verdict into this box's record, keeping every other field and ``ts``.

    With no record yet, writes a verdict-only one at ``ts`` 0 so capacity readers still read it
    stale. False on I/O failure; never raises.
    """
    try:
        record = _load(home) or build({}, home, 0.0)
        record["mem_critical"] = bool(critical)
        record["paused"] = int(paused)
        record["mem_ts"] = time.time() if now is None else now
        return _write(record, home)
    except (OSError, ValueError, TypeError):
        return False


def read(box: dict, now: Optional[float] = None) -> dict:
    """The record at ``box["root"]`` plus ``age_s`` and ``stale``; never raises.

    A missing or unreadable record yields ``{box_id, stale: True, age_s: None}``. A ts in the
    future (clock skew between boxes) clamps ``age_s`` to 0. ``stale`` is True past ``TTL_S``.
    """
    box_id = box.get("box_id") if isinstance(box, dict) else None
    absent = {"box_id": box_id, "stale": True, "age_s": None}
    try:
        # A far POSIX root is not absolute on Windows (and the reverse): opening it would resolve under
        # this drive and read this box's own stamp as the far box's.
        if not Path(str(box["root"])).is_absolute():
            return absent
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


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def verdicts(rec: dict, now: Optional[float] = None) -> dict:
    """``{saturated, top_cpu, mem_critical, paused}`` from a ``read`` record.

    Each verdict reads from its own clock: CPU from ``ts`` (``stale``), memory from ``mem_ts``, both
    against ``TTL_S``. A stale half reads not saturated / not critical, never its last value."""
    now = time.time() if now is None else now
    cpu_fresh = isinstance(rec, dict) and rec.get("stale") is False
    mem_ts = rec.get("mem_ts") if isinstance(rec, dict) else None
    mem_fresh = isinstance(mem_ts, (int, float)) and not isinstance(mem_ts, bool) and max(0.0, now - mem_ts) <= TTL_S
    return {
        "saturated": bool(cpu_fresh and rec.get("saturated") is True),
        "top_cpu": rec["top_cpu"] if cpu_fresh and isinstance(rec.get("top_cpu"), list) else [],
        "mem_critical": bool(mem_fresh and rec.get("mem_critical") is True),
        "paused": _count(rec.get("paused")) if mem_fresh else 0,
    }


def local_verdicts(home: Optional[Path] = None, now: Optional[float] = None) -> dict:
    """This box's own ``verdicts``, for ``capacity_probe.sample`` to return over ``groupem.capacity_local``."""
    root = home if home is not None else _path(None).parents[3]
    return verdicts(read({"box_id": None, "root": str(root)}, now), now)


def _snapshot_verdicts(row: dict) -> dict:
    """``verdicts`` of a fleet live snapshot row (fresh ``ok`` rows only; else all-clear)."""
    fresh = row.get("state") == "ok" and row.get("stale") is False
    return {
        "saturated": bool(fresh and row.get("saturated") is True),
        "top_cpu": _top(row.get("top_cpu")) if fresh else [],
        "mem_critical": bool(fresh and row.get("mem_critical") is True),
        "paused": _count(row.get("paused")) if fresh else 0,
    }


def saturated_boxes(home: Optional[Path], local_id: str, now: Optional[float] = None) -> list:
    """One row per registered box other than ``local_id``: ``{box_id, alias, stale, saturated, top_cpu,
    mem_critical, paused}``. Never ssh, never raises.

    A box whose root is an absolute path here is read from its stamp; a far box (root not readable on
    this OS) from the fleet live snapshot the watch keeps warm. A stale or missing record counts as not
    saturated and not critical, with ``stale`` True."""
    try:
        from coordinator_core.group_em import fleet_live

        now = time.time() if now is None else now
        snapshot = {r.get("box"): r for r in fleet_live.read_live_snapshot(TTL_S, now)}
        out = []
        for box in fleet_live.registered_boxes(home):
            box_id = box.get("box_id")
            if box_id == local_id:
                continue
            if Path(str(box.get("root"))).is_absolute():
                rec = read(box, now)
                stale, state, alias = rec.get("stale") is not False, verdicts(rec, now), None
            else:
                row = snapshot.get(box_id) or {}
                stale, state, alias = row.get("stale") is not False, _snapshot_verdicts(row), row.get("alias")
            out.append({"box_id": box_id, "alias": alias or fleet_live.box_alias(box), "stale": stale, **state})
        return out
    except Exception:  # noqa: BLE001 -- degrade-never-raise
        return []

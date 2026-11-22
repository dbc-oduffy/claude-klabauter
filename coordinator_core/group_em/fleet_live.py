"""Record contract for one box's row in the live fleet-capacity snapshot.

``FIELDS`` is the per-box key set every row carries, in render order. A row's ``state`` is one of
``STATES``. Maxima fields survive an unreachable box (carried from a last-known record, with
``maxima_source`` naming where they came from); current-use fields are ``None`` there, never stale.
``gpus[]`` rows are the ``gpu_sample`` shape (``index, name, vram_total_mib, vram_used_mib,
util_pct``); ``disks[]`` rows are ``{mount, total_gb, free_gb, floor_gb}``.
"""

from __future__ import annotations

from typing import Any, Optional

STATE_OK = "ok"
STATE_TIMED_OUT = "timed_out"
STATE_UNREACHABLE = "unreachable"
STATE_ERROR = "error"
STATES = (STATE_OK, STATE_TIMED_OUT, STATE_UNREACHABLE, STATE_ERROR)

MAXIMA_FIELDS = ("cores", "cpu_model", "ram_total_mb", "commit_limit_mb", "gpus", "disks")
USE_FIELDS = (
    "load5",
    "ram_free_mb",
    "commit_used_mb",
    "vram_used_mib",
    "sessions",
    "gpu_slot_holder",
    "editor_slot_holder",
    "top_cpu",
    "top_ram",
)
FIELDS = ("box", "state") + MAXIMA_FIELDS + USE_FIELDS + ("maxima_source",)

_GPU_USE_KEYS = ("vram_used_mib", "util_pct")
_DISK_USE_KEYS = ("free_gb",)


def _max_gpus(gpus: Any) -> Optional[list]:
    if not isinstance(gpus, list):
        return None
    return [
        {k: (None if k in _GPU_USE_KEYS else v) for k, v in g.items()} if isinstance(g, dict) else g
        for g in gpus
    ]


def _max_disks(disks: Any) -> Optional[list]:
    if not isinstance(disks, list):
        return None
    return [
        {k: (None if k in _DISK_USE_KEYS else v) for k, v in d.items()} if isinstance(d, dict) else d
        for d in disks
    ]


def empty_row(box: str, state: str = STATE_ERROR) -> dict:
    """A row with every field present and ``None``; ``box`` and ``state`` set."""
    row: dict = {f: None for f in FIELDS}
    row["box"] = box
    row["state"] = state
    return row


def timed_out_row(box: str, last_known: Optional[dict] = None, *, state: str = STATE_TIMED_OUT,
                  maxima_source: Optional[str] = None) -> dict:
    """Row for a box that did not answer: current use ``None``, maxima carried from ``last_known``.

    ``maxima_source`` is the caller's name for ``last_known`` (a record path or label); it is
    ``None`` when no maxima are known. ``state`` may be ``unreachable`` for a box that could not
    be asked at all.
    """
    row = empty_row(box, state if state in STATES else STATE_TIMED_OUT)
    lk = last_known if isinstance(last_known, dict) else {}
    carried = False
    for f in MAXIMA_FIELDS:
        v = lk.get(f)
        if f == "gpus":
            v = _max_gpus(v)
        elif f == "disks":
            v = _max_disks(v)
        if v is not None:
            row[f] = v
            carried = True
    if carried:
        row["maxima_source"] = maxima_source if maxima_source is not None else lk.get("maxima_source")
    return row


def normalize_row(box: str, raw: Optional[dict]) -> dict:
    """Project a probe reply onto ``FIELDS``; missing keys become ``None``, extras are dropped."""
    if not isinstance(raw, dict):
        return empty_row(box)
    row = {f: raw.get(f) for f in FIELDS}
    row["box"] = box
    if row["state"] not in STATES:
        row["state"] = STATE_OK
    return row

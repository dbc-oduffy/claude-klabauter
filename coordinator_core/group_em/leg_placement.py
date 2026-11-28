"""Leg-box chooser over registry and capacity records only: no subprocess, no ssh."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from coordinator_core.group_em import box_registry, capacity_record, fleet_balance, placement


def _load_pct(sample: dict) -> Optional[float]:
    """CPU load percent off a capacity record; None when unmeasured."""
    return fleet_balance.load_pct({"cpu_pct": sample.get("load_pct")})


def _result(box: Optional[dict], box_id: str, load: Optional[float], local_load: Optional[float], reason: str) -> dict:
    return {
        "box": box,
        "box_id": box_id,
        "local": box is None,
        "load_pct": load,
        "local_load_pct": local_load,
        "reason": reason,
    }


def choose_leg_box(leg: str, home: Optional[Path] = None, now: Optional[float] = None) -> dict:
    """Pick the box for ``leg``: a fresh candidate remote strictly below local's load, else local (box None); never raises."""
    local_id = "local"
    try:
        now = time.time() if now is None else now
        directory = box_registry._group_em_dir(home)
        local_id = box_registry.local_box_id(home)
        local_root = Path(home) if home is not None else directory.parent.parent
        local_load = _load_pct(capacity_record.read({"box_id": local_id, "root": str(local_root)}, now))
        if local_load is None:
            return _result(None, local_id, None, None, "local_load_unmeasured")
        registry = {b["box_id"]: b for b in box_registry.read_boxes(directory)}
        best: Optional[tuple] = None
        for row in placement.candidates(leg, directory=directory, home=local_root, now=now):
            bid = row["box_id"]
            box = registry.get(bid)
            if box is None or box_registry.same_box(bid, local_id):
                continue
            load = _load_pct(capacity_record.read(box, now))
            if load is None or load >= local_load:
                continue
            if best is None or load < best[0]:
                best = (load, box)
        if best is None:
            return _result(None, local_id, local_load, local_load, "no_remote_below_local")
        return _result(best[1], best[1]["box_id"], best[0], local_load, "remote_below_local")
    except Exception as exc:
        return _result(None, local_id, None, None, f"error:{type(exc).__name__}")

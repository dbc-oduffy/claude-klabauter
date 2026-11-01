"""Whole-run placement: record that a run belongs on a registered box; the box's own launcher reads it.

A placement is ``<box root>/state/group-em/box/placements/<safe run>.json``. Launching is not here:
``read_intake`` is the seam the target box's launcher polls. Requirement keys: ``min_<k>`` is a
numeric floor on capability ``<k>`` (``min_ram_mb``); any other key is equality on capability
``<k>`` (``os``). Leaf over ``box_registry``; no spawns, no raise.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import box_registry

SCHEMA_VERSION = 1
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _safe(run: str) -> str:
    return _UNSAFE.sub("_", str(run)) or "_"


def _placements_dir(home: Optional[Path] = None) -> Path:
    return box_registry._group_em_dir(home) / "box" / "placements"


def _num(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def satisfies(capability: dict, requirement: dict) -> bool:
    """True when ``capability`` meets every key of ``requirement`` (see module doc for key forms)."""
    for key, want in (requirement or {}).items():
        if key.startswith("min_"):
            have, floor = _num(capability.get(key[4:])), _num(want)
            if have is None or floor is None or have < floor:
                return False
        elif capability.get(key) != want:
            return False
    return True


def place_run(
    run: str,
    requirement: dict,
    box_id: Optional[str] = None,
    brief: str = "",
    directory: Optional[Path] = None,
) -> dict:
    """Place ``run`` on a box; returns ``{ok: True, placement}`` or ``{ok: False, reason}``.

    An explicit ``box_id`` wins (reason ``no_box`` if unregistered, ``unsatisfied`` if its capability
    misses ``requirement``); otherwise the first registered box in id order that satisfies it
    (``unsatisfied`` when none does, ``no_box`` when none is registered). Never raises.
    """
    try:
        requirement = dict(requirement or {})
        boxes = box_registry.read_boxes(directory)
        if box_id is not None:
            chosen = next((b for b in boxes if b["box_id"] == box_id), None)
            if chosen is None:
                return {"ok": False, "reason": "no_box", "box_id": box_id}
            if not satisfies(chosen["capability"], requirement):
                return {"ok": False, "reason": "unsatisfied", "box_id": box_id}
        else:
            if not boxes:
                return {"ok": False, "reason": "no_box"}
            chosen = next((b for b in boxes if satisfies(b["capability"], requirement)), None)
            if chosen is None:
                return {"ok": False, "reason": "unsatisfied"}
        placement = {
            "schema_version": SCHEMA_VERSION,
            "run": str(run),
            "box_id": chosen["box_id"],
            "placed_by": os.environ.get("CLAUDE_SESSION_ID") or "unknown",
            "placed_at": time.time(),
            "requirement": requirement,
            "brief": brief,
        }
        path = _placements_dir(Path(chosen["root"])) / (_safe(run) + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, (json.dumps(placement, indent=2) + "\n").encode("utf-8"))
        return {"ok": True, "placement": placement}
    except Exception as exc:  # contract: refuse, never raise
        return {"ok": False, "reason": "error", "detail": type(exc).__name__}


def read_intake(home: Optional[Path] = None) -> list[dict]:
    """Every well-formed placement under ``<home>/state/group-em/box/placements``, sorted by run file."""
    base = _placements_dir(home)
    try:
        names = sorted(n for n in os.listdir(base) if n.endswith(".json"))
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        try:
            with open(base / name, "r", encoding="utf-8") as fh:
                data = json.loads(fh.read())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("run"), str) and data.get("box_id"):
            out.append(data)
    return out

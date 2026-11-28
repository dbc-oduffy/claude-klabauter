"""Whole-run placement: record that a run belongs on a registered box; the box's own launcher reads it.

A placement is ``<box root>/state/group-em/box/placements/<safe run>.json``. Launching is not here:
``read_intake`` is the seam the target box's launcher polls. Requirement keys: ``min_<k>`` is a
numeric floor on capability ``<k>`` (``min_ram_mb``, or ``min_free_<axis>`` on measured free
headroom from ``sample_capability``); ``os`` matches a name or a list by case-insensitive
membership; any other key is equality. ``candidates`` lists boxes that can run a job type now
from their capacity samples. Leaf over ``box_registry`` and the capacity modules; no spawns, no raise.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import box_registry, capacity_admit, capacity_record, job_peaks

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


def _os_match(have: object, want: object) -> bool:
    wanted = want if isinstance(want, (list, tuple, set, frozenset)) else [want]
    return isinstance(have, str) and have.casefold() in {w.casefold() for w in wanted if isinstance(w, str)}


def satisfies(capability: dict, requirement: dict) -> bool:
    """True when ``capability`` meets every key of ``requirement`` (see module doc for key forms)."""
    for key, want in (requirement or {}).items():
        if key == "os":
            if not _os_match(capability.get("os"), want):
                return False
        elif key.startswith("min_"):
            have, floor = _num(capability.get(key[4:])), _num(want)
            if have is None or floor is None or have < floor:
                return False
        elif capability.get(key) != want:
            return False
    return True


def sample_capability(sample: dict, held: Optional[list] = None, now: Optional[float] = None) -> dict:
    """A capability dict read off a capacity sample: ``os``, ``arch`` and measured free headroom
    ``free_vram_mib``, ``free_ram_mb``, ``free_cores`` (held grants subtracted; None when unmeasured),
    so ``min_free_<axis>`` floors apply to it through ``satisfies``."""
    free = capacity_admit.headroom(sample, capacity_admit.live_held(list(held or []), sample, time.time() if now is None else now), now)
    return {
        "os": sample.get("os"),
        "arch": sample.get("arch"),
        "free_vram_mib": free["vram_mib"],
        "free_ram_mb": free["ram_mb"],
        "free_cores": free["cores"],
    }


def _hardware_groups(boxes: list, home: Optional[Path]) -> dict:
    """``{box_id: group key}`` for boxes the membership's ``same_hardware_as`` ties to one machine.

    One unverified snapshot read, no spawn: placement is advisory, never an authority, so a
    signature check is not worth a process. The relation is symmetric (a one-sided declaration
    pairs both boxes). An absent field or unreadable membership yields no groups. Never raises.
    """
    try:
        from coordinator_core.group_em import member_box

        base = Path(home) if home is not None else box_registry.settings_home()
        snap, _sig = member_box._membership().load_snapshot(base)
        members = [m for m in (snap or {}).get("members", []) if isinstance(m, dict)]
        fold = box_registry.fold_box_id
        parent: dict = {}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for m in members:
            twins = m.get("same_hardware_as")
            for twin in twins if isinstance(twins, list) else []:
                if isinstance(twin, str):
                    parent[find(fold(twin))] = find(fold(m["box"]))
        if not parent:
            return {}
        registry = []
        for b in boxes:
            cap = b.get("capability")
            raw = [b.get("box_id"), cap.get("name") if isinstance(cap, dict) else None]
            raw += b["names"] if isinstance(b.get("names"), list) else []
            registry.append({fold(n) for n in raw if isinstance(n, str) and n})
        out: dict = {}
        for b in boxes:
            member = next((m for m in members if member_box._matches(m, None, b["box_id"], tuple(registry))), None)
            if member is not None and fold(member["box"]) in parent:
                out[b["box_id"]] = find(fold(member["box"]))
        counts: dict = {}
        for g in out.values():
            counts[g] = counts.get(g, 0) + 1
        return {k: g for k, g in out.items() if counts[g] > 1}
    except Exception:  # contract: no twins, never raise
        return {}


def _booted_twins(fresh: list, groups: dict) -> dict:
    """``{group key: box_id}`` of the box with the newest fresh sample in each hardware group.

    ``fresh`` is ``[(box, sample)]`` rows with a fresh sample; a tie on age goes to the lower box id."""
    best: dict = {}
    for box, sample in fresh:
        g = groups.get(box["box_id"])
        if g is None:
            continue
        rank = (sample.get("age_s") or 0.0, box["box_id"])
        if g not in best or rank < best[g][0]:
            best[g] = (rank, box["box_id"])
    return {g: v[1] for g, v in best.items()}


def candidates(
    job_type: str,
    os: object = None,
    directory: Optional[Path] = None,
    home: Optional[Path] = None,
    declared: Optional[dict] = None,
    requirement: Optional[dict] = None,
    now: Optional[float] = None,
) -> list[dict]:
    """Every registered box (plus the ``home`` box) that can run ``job_type`` now, most headroom first.

    A box is capable when its capacity sample is fresh, its platform matches ``os`` (a name or a
    list), ``requirement`` (``min_free_<axis>`` and any other key, via ``satisfies``) holds, and
    ``capacity_admit.decide`` grants the job's measured peak (``declared`` only when none is
    measured) over its measured headroom less grants not yet running. Rows are
    ``{box_id, os, headroom, gpu_index, peak_basis}``; ordered by free vram, ram, then cores,
    descending, ties by box id. A box with no fresh sample is never listed. Never raises.
    """
    try:
        now = time.time() if now is None else now
        peak = job_peaks.peak(job_type, home)
        boxes = [dict(b) for b in box_registry.read_boxes(directory)]
        if home is not None and not any(b["box_id"] == box_registry.local_box_id(home) for b in boxes):
            boxes.append({"box_id": box_registry.local_box_id(home), "root": str(home)})
        want = dict(requirement or {})
        if os is not None:
            want["os"] = os
        out: list[dict] = []
        fresh = []
        for box in boxes:
            sample = capacity_record.read(box, now)
            if sample.get("stale") is False:
                fresh.append((box, sample))
        groups = _hardware_groups(boxes, home)
        booted = _booted_twins(fresh, groups)
        for box, sample in fresh:
            g = groups.get(box["box_id"])
            if g is not None and booted.get(g) != box["box_id"]:
                continue
            held = capacity_admit.read_held(sample, Path(box["root"]), now)
            if not satisfies(sample_capability(sample, held, now), want):
                continue
            verdict = capacity_admit.decide(sample, peak, held, declared, now)
            if verdict["grant"]:
                out.append(
                    {
                        "box_id": box["box_id"],
                        "os": sample.get("os"),
                        "headroom": verdict["headroom"],
                        "gpu_index": verdict["gpu_index"],
                        "peak_basis": verdict["peak_basis"],
                    }
                )

        def _key(row: dict) -> tuple:
            h = row["headroom"]
            return tuple(-(h[a] if h[a] is not None else float("-inf")) for a in ("vram_mib", "ram_mb", "cores")) + (
                row["box_id"],
            )

        return sorted(out, key=_key)
    except Exception:  # contract: refuse, never raise
        return []


def _held_by_other_run(path: Path, run: str) -> bool:
    """True when ``path`` already records a placement for a different run (two runs share a safe filename)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("run"), str) and data["run"] != run


def place_run(
    run: str,
    requirement: dict,
    box_id: Optional[str] = None,
    brief: str = "",
    directory: Optional[Path] = None,
    home: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Place ``run`` on a box; returns ``{ok: True, placement}`` or ``{ok: False, reason}``.

    Boxes the membership marks ``same_hardware_as`` are one machine booted exclusively: a box whose
    twin holds the newest fresh capacity sample is refused (``same_hardware_booted``); an implicit
    choice skips such boxes. ``home`` locates the membership (default: the settings home).

    An explicit ``box_id`` wins (reason ``no_box`` if unregistered, ``unsatisfied`` if its capability
    misses ``requirement``); otherwise the first registered box in id order that satisfies it
    (``unsatisfied`` when none does, ``no_box`` when none is registered). A different run already
    holding the same safe filename is refused (``run_collision``) and keeps its placement. Never raises.
    """
    try:
        requirement = dict(requirement or {})
        boxes = box_registry.read_boxes(directory)
        now = time.time() if now is None else now
        if home is None and directory is not None and Path(directory).name == "group-em":
            home = Path(directory).parent.parent
        groups = _hardware_groups(boxes, home)
        booted = _booted_twins(
            [(b, s) for b in boxes if b["box_id"] in groups for s in [capacity_record.read(b, now)] if s.get("stale") is False],
            groups,
        )

        def _twin_booted(b: dict) -> bool:
            g = groups.get(b["box_id"])
            return g is not None and g in booted and booted[g] != b["box_id"]

        if box_id is not None:
            chosen = next((b for b in boxes if b["box_id"] == box_id), None)
            if chosen is None:
                return {"ok": False, "reason": "no_box", "box_id": box_id}
            if not satisfies(chosen["capability"], requirement):
                return {"ok": False, "reason": "unsatisfied", "box_id": box_id}
            if _twin_booted(chosen):
                return {"ok": False, "reason": "same_hardware_booted", "box_id": box_id}
        else:
            if not boxes:
                return {"ok": False, "reason": "no_box"}
            fits = [b for b in boxes if satisfies(b["capability"], requirement)]
            if not fits:
                return {"ok": False, "reason": "unsatisfied"}
            chosen = next((b for b in fits if not _twin_booted(b)), None)
            if chosen is None:
                return {"ok": False, "reason": "same_hardware_booted", "box_id": fits[0]["box_id"]}
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
        if _held_by_other_run(path, str(run)):
            return {"ok": False, "reason": "run_collision", "box_id": chosen["box_id"]}
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

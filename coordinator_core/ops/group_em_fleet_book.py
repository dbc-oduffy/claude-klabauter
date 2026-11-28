"""
coordinator_core.ops.group_em_fleet_book -- JSON-RPC "groupem.fleet_book".

Purpose: one fleet-wide booking verb. It records a booking on the chosen box and answers with the
headroom of every registered box, so a slot request is routed with the whole fleet in view.

Negative-spec:
    - Never raises: an invalid param or I/O failure is `{"error": ...}`.
    - Spawns 0 processes and makes no network call: it reads the persisted fleet live snapshot
      (``fleet_live.read_live_snapshot``) and this box's stamped capacity record, never ssh. The one
      write is a grant row in THIS box's ``capacity_admit`` grants ledger, ``box_id`` naming the target.
    - No priority and no measured peak are required, and a booking is never refused for want of
      headroom (``capacity_admit.decide`` / ``admit`` and their ``no_measured_peak`` refusal are untouched).
    - A stale, unreachable or never-sampled box is listed with its state, carries no free-capacity
      field, is never idle and is never picked.
    - No reply carries a slot or job count.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import box_registry, capacity_admit, capacity_record, fleet_balance, fleet_live, job_peaks
from coordinator_core.group_em.atomic_record import holder_lock
from coordinator_core.ipc import register_op

_AXES = ("ram_mb", "vram_mib", "cores")
BASIS_UNMEASURED = "unmeasured"
_STATE_OK = "ok"
_STATE_STALE = "stale"
_LOCK_TIMEOUT_S = 2.0


def _num(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _declared(p: dict) -> tuple:
    """``(axes or None, core_class or None, error)`` from ``declared``: ram_mb, vram_mib, cores, core_class."""
    v = p.get("declared")
    if v is None:
        return None, None, None
    if not isinstance(v, dict):
        return None, None, "declared must be an object"
    axes = {}
    for axis in _AXES:
        if axis in v:
            n = v[axis]
            if isinstance(n, bool) or not isinstance(n, (int, float)) or n < 0:
                return None, None, f"declared.{axis} must be a non-negative number"
            axes[axis] = n
    core_class = v.get("core_class")
    if core_class is not None and not (isinstance(core_class, str) and core_class):
        return None, None, "declared.core_class must be a non-empty str"
    return (axes or None), core_class, None


def _peak_axes(peak: Optional[dict]) -> dict:
    if not isinstance(peak, dict):
        return {}
    return {a: peak[a] for a in _AXES if _num(peak.get(a)) is not None and peak[a] >= 0}


def _local_row(rec: dict) -> Optional[dict]:
    """A fleet row from this box's stamped capacity record; None when it was never stamped."""
    sample = capacity_record.read(rec)
    age = sample.get("age_s")
    if age is None:
        return None
    gpus = sample.get("gpus")
    return {
        "box": rec["box_id"],
        "state": _STATE_OK,
        "cores": sample.get("cores_logical") or sample.get("cores_physical"),
        "cpu_pct": sample.get("load_pct"),
        "ram_free_mb": sample.get("ram_avail_mb"),
        "gpus": gpus if isinstance(gpus, list) else None,
        "age_s": age,
    }


def _by_class(row: dict) -> tuple:
    """``(heterogeneous, free_cores_by_class or None)``: a box is heterogeneous when it declares classes."""
    hetero = isinstance(row.get("cores_by_class"), dict) or isinstance(row.get("core_classes"), dict)
    free = row.get("free_cores_by_class")
    return hetero, (dict(free) if isinstance(free, dict) else None)


def _gpu_util(row: dict) -> Optional[float]:
    util = _num(row.get("gpu_util_pct"))
    if util is not None:
        return util
    utils = [u for g in (row.get("gpus") or []) if isinstance(g, dict) and (u := _num(g.get("util_pct"))) is not None]
    return max(utils) if utils else None


def _view(rec: dict, row: Optional[dict], held: list, now: float) -> dict:
    """One box's reply entry. Only a fresh ``ok`` row carries free-capacity fields."""
    box_id = rec["box_id"]
    alias = (row or {}).get("alias") or fleet_live.box_alias(rec)
    age = _num((row or {}).get("age_s"))
    state = (row or {}).get("state") or _STATE_STALE
    if state == _STATE_OK and (age is None or age > fleet_balance.FRESH_S):
        state = _STATE_STALE
    view = {
        "box_id": box_id, "alias": alias, "state": state, "ram_free_mb": None, "cores": None,
        "free_cores_by_class": None, "free_cores": None, "load": None, "gpu_util_pct": None,
        "vram_free_mib": None, "idle": False, "age_s": age,
    }
    if state != _STATE_OK or row is None:
        return view
    load = fleet_balance.load_pct(row)
    sample = {
        "box_id": box_id, "gpus": row.get("gpus"), "ram_avail_mb": row.get("ram_free_mb"),
        "cores_logical": row.get("cores"), "load_pct": load,
    }
    free = capacity_admit.headroom(sample, held)
    hetero, by_class = _by_class(row)
    view.update(
        ram_free_mb=free["ram_mb"], cores=row.get("cores"), load=load, gpu_util_pct=_gpu_util(row),
        vram_free_mib=free["vram_mib"], idle=fleet_balance.is_idle(row, now),
    )
    if hetero:
        view["free_cores_by_class"] = by_class
    elif free["cores"] is not None:
        view["free_cores"] = round(max(0.0, free["cores"]), 1)
    return view


def _score(view: dict, core_class: Optional[str]) -> Optional[tuple]:
    """Ranking key (bigger is freer) of a usable box; None when it cannot take this booking."""
    if view["state"] != _STATE_OK:
        return None
    by_class = view["free_cores_by_class"]
    if core_class is not None:
        free = _num(by_class.get(core_class)) if isinstance(by_class, dict) else None
        if free is None:
            return None
    elif isinstance(by_class, dict):
        free = _num(by_class.get("p"))
    elif view["free_cores"] is None:
        return None
    else:
        free = _num(view["free_cores"])
    return (free or 0.0, _num(view["vram_free_mib"]) or 0.0)


def _pick(views: list, core_class: Optional[str]) -> tuple:
    """``(box_id or None, class_fallback)``: the freest usable box, falling back to any usable box
    when none carries ``core_class``."""
    for cls in ((core_class, None) if core_class is not None else (None,)):
        scored = [(s, v["box_id"]) for v in views if (s := _score(v, cls)) is not None]
        if scored:
            return max(scored, key=lambda t: (t[0], t[1]))[1], cls is None and core_class is not None
    return None, False


def _resolve(records: list, wanted: str) -> Optional[dict]:
    key = wanted.strip().lower()
    for rec in records:
        names = {rec.get("box_id"), rec.get("registered_as"), rec.get("alias"), *(rec.get("aliases") or [])}
        if key in {n.lower() for n in names if isinstance(n, str)}:
            return rec
    return None


def _append_grant(home: Path, row: dict, now: float) -> bool:
    """Add ``row`` to this box's grants ledger under the ledger lock; False on a lock or I/O failure."""
    path = capacity_admit.grants_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with holder_lock(path, timeout=_LOCK_TIMEOUT_S):
            rows = [r for r in capacity_admit.read_held({}, home, now) if r.get("run") != row["run"]]
            rows.append(row)
            body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
            atomic_write_bytes(path, body.encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 -- the booking reply still names the headroom
        return False


@register_op("groupem.fleet_book")
def _groupem_fleet_book(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.fleet_book" handler: record a booking and list every box's headroom.

    Params:
        job_type (str) -- required; the kind of work booked.
        box_id (str, optional) -- book on this box (registry id or alias); absent, the box with the
            most free cores (P-cores on a heterogeneous box), then most free VRAM, among fresh ``ok`` boxes.
        run (str, optional) -- the run label; minted when absent. requester_sid (str, optional).
        declared (dict, optional) -- ram_mb / vram_mib / cores held against the box, and ``core_class``
            ("p" / "e") to pick by that class's free cores.

    Returns:
        {booked, run, job_type, box_id, alias, basis, recorded, boxes: [{box_id, alias, state,
        ram_free_mb, cores, free_cores_by_class (heterogeneous box; else None), free_cores (homogeneous
        box; else None), load, gpu_util_pct, vram_free_mib, idle, age_s}, ...every registered box]}
        or {"error": str}. ``basis`` is ``measured`` (a recorded job peak), ``declared`` or
        ``unmeasured``. ``reason: "no_fresh_box"`` marks a booking placed on this box with no fresh
        sample anywhere; ``class_fallback: true`` marks a ``core_class`` no box carries.
    """
    p = params if isinstance(params, dict) else {}
    job_type = p.get("job_type")
    if not (isinstance(job_type, str) and job_type):
        return {"error": "job_type (non-empty str) is required"}
    for name in ("run", "box_id", "requester_sid"):
        if p.get(name) is not None and not isinstance(p[name], str):
            return {"error": f"{name} must be str"}
    declared, core_class, err = _declared(p)
    if err:
        return {"error": err}
    try:
        home = settings_home()
        now = time.time()
        local = box_registry.local_box_id(home)
        records = fleet_live.registered_boxes(home)
        snap = {r.get("box"): r for r in fleet_live.read_live_snapshot(fleet_balance.FRESH_S, now)}
        rows = {}
        for rec in records:
            row = snap.get(rec["box_id"])
            if rec["box_id"] == local:
                fallback = _local_row(rec)
                if fallback is not None and (row is None or row.get("stale") or fallback["age_s"] < (row.get("age_s") or 0)):
                    # The stamped record has no per-core split: keep the declared classes so a
                    # heterogeneous box stays unknown (never idle by whole-box load).
                    classes = {k: row[k] for k in ("core_classes", "cores_by_class") if row and row.get(k)}
                    row = {**fallback, **classes}
            rows[rec["box_id"]] = row
        held = capacity_admit.read_held({}, home, now)
        views = [_view(rec, rows[rec["box_id"]], held, now) for rec in records]

        reply: dict = {}
        if p.get("box_id"):
            target_rec = _resolve(records, p["box_id"])
            if target_rec is None:
                return {"error": f"box_id {p['box_id']!r} is not registered"}
            target = target_rec["box_id"]
        else:
            target, class_fallback = _pick(views, core_class)
            if class_fallback:
                reply["class_fallback"] = True
            if target is None:
                target, reply["reason"] = local, "no_fresh_box"
        run = p.get("run") or f"{job_type}-{uuid.uuid4().hex[:8]}"
        axes = declared
        basis = capacity_admit.PEAK_DECLARED if axes else None
        if not axes:
            axes = _peak_axes(job_peaks.peak(job_type, home))
            basis = capacity_admit.PEAK_MEASURED if axes else BASIS_UNMEASURED
        row = {
            "run": run, "job_type": job_type, "box_id": target, "gpu_index": None, "granted_at": now,
            "ttl_s": capacity_admit.GRANT_TTL_S, "basis": basis, "requester_sid": p.get("requester_sid"),
            **{a: axes.get(a) for a in _AXES},
        }
        if core_class is not None:
            row["core_class"] = core_class
        recorded = _append_grant(home, row, now)
        if recorded:
            held = capacity_admit.read_held({}, home, now)
            views = [_view(rec, rows[rec["box_id"]], held, now) for rec in records]
        alias = next((v["alias"] for v in views if v["box_id"] == target), None)
        reply.update(
            booked=recorded, run=run, job_type=job_type, box_id=target, alias=alias, basis=basis,
            recorded=recorded, boxes=views,
        )
        return reply
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}

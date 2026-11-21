"""Idle-capacity watchdog: is a priority job waiting while a capable box idles?

``scan(waits, samples, peaks, holder)`` is a pure decision over records already read: wait rows
(``capacity-waits.jsonl``), per-box capacity samples (``capacity_record.read``), measured job
peaks (``job_peaks``) and the box holder (the caller passes ``nomination.box_holder``). It sends
nothing: the alerts are returned to the caller (``groupem.idle_capacity_watch``), and the watch
tick prints them. A job type with no measured peak never alerts (no declared guess stands in), and
a stale sample is not an idle box. Never raises.
"""

from __future__ import annotations

import time
from typing import Mapping, Optional

from coordinator_core.group_em import capacity_admit

# Is this box's GPU (or, for a job needing no VRAM, its CPU) idle enough to take more work?
IDLE_UTIL_PCT = 10.0
# Has the requester waited long enough that an idle box is worth interrupting the G-EM?
MIN_WAIT_S = 60.0
_PRIORITY_NAMES = frozenset({"priority", "high", "urgent"})


def _num(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def is_priority(value: object) -> bool:
    """True for a wait row's priority marker: ``True``, a positive number, or a priority name."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PRIORITY_NAMES
    number = _num(value)
    return number is not None and number > 0


def _idle_headroom(sample: dict, peak: dict, held: Optional[list] = None) -> Optional[dict]:
    """The box's measured free amounts when it is fresh, idle and covers ``peak`` on every axis; else None.

    ``held`` is the box's granted-but-not-started grants; their declared amounts count as taken.
    """
    if sample.get("stale") is not False:
        return None
    need_vram = _num(peak.get("vram_mib")) or 0.0
    need_ram = _num(peak.get("ram_mb")) or 0.0
    need_cores = _num(peak.get("cores")) or 0.0
    out: dict = {}
    if need_vram > 0:
        gpus = sample.get("gpus")
        fits = []
        for gpu in gpus if isinstance(gpus, list) else []:
            total = _num(gpu.get("vram_total_mib"))
            used = _num(gpu.get("vram_used_mib"))
            util = _num(gpu.get("util_pct"))
            if total is None or used is None or util is None:
                continue
            if util < IDLE_UTIL_PCT and total - used >= need_vram:
                fits.append((util, total - used))
        if not fits:
            return None
        out["gpu_util_pct"], out["vram_free_mib"] = min(fits)
    else:
        load = _num(sample.get("load_pct"))
        if load is None or load >= IDLE_UTIL_PCT:
            return None
        out["load_pct"] = load
    if need_ram > 0:
        avail = _num(sample.get("ram_avail_mb"))
        if avail is None or avail < need_ram:
            return None
        out["ram_avail_mb"] = avail
    if need_cores > 0:
        cores = _num(sample.get("cores_logical")) or _num(sample.get("cores_physical"))
        load = _num(sample.get("load_pct"))
        if cores is None or load is None or cores * (1 - load / 100.0) < need_cores:
            return None
    if held:
        free = capacity_admit.headroom(sample, held)
        for axis, need in (("vram_mib", need_vram), ("ram_mb", need_ram), ("cores", need_cores)):
            if need > 0 and (free[axis] is None or free[axis] < need):
                return None
    return out


def scan(
    waits: list,
    samples: list,
    peaks: Mapping[str, dict],
    holder: Optional[dict],
    now: Optional[float] = None,
    held: Optional[Mapping[str, list]] = None,
) -> list[dict]:
    """One alert per priority wait that a different idle box could run now.

    An alert is ``{run, job_type, requester_sid, holder_sid, idle_box_id, waited_s, headroom}``.
    A wait row may carry ``box_id`` (the box that refused it): that box never counts as the idle
    capable one. ``held`` maps ``box_id`` to that box's unstarted grants (``capacity_admit.read_held``);
    they are subtracted before a box counts as idle. Among capable boxes the one with the lowest
    utilisation is named. Waits younger than ``MIN_WAIT_S``, non-priority waits, unmeasured job types and stale samples yield nothing.
    """
    try:
        clock = time.time() if now is None else now
        holder_sid = holder.get("session_id") if isinstance(holder, dict) else None
        alerts: list[dict] = []
        seen: set = set()
        for wait in waits or []:
            if not isinstance(wait, dict) or not is_priority(wait.get("priority")):
                continue
            run, since = wait.get("run"), _num(wait.get("since"))
            if run in seen or since is None or clock - since < MIN_WAIT_S:
                continue
            peak = peaks.get(wait.get("job_type")) if isinstance(peaks, Mapping) else None
            if not isinstance(peak, dict):
                continue
            best = None
            for sample in samples or []:
                if not isinstance(sample, dict) or sample.get("box_id") == wait.get("box_id"):
                    continue
                headroom = _idle_headroom(
                    sample, peak, held.get(sample.get("box_id")) if isinstance(held, Mapping) else None
                )
                if headroom is None:
                    continue
                load = headroom.get("gpu_util_pct", headroom.get("load_pct", 0.0))
                if best is None or load < best[0]:
                    best = (load, sample.get("box_id"), headroom)
            if best is None:
                continue
            seen.add(run)
            alerts.append({
                "run": run,
                "job_type": wait.get("job_type"),
                "requester_sid": wait.get("requester_sid"),
                "holder_sid": holder_sid,
                "idle_box_id": best[1],
                "waited_s": clock - since,
                "headroom": best[2],
            })
        return alerts
    except Exception:  # contract: scan never raises
        return []

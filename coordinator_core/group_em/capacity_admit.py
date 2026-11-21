"""Measured-headroom admission: grant when measured free headroom covers the job's measured peak.

``decide`` is pure over a ``capacity_record`` sample, a ``job_peaks`` peak and the grants already
made on that box but not yet visible in the sample. Headroom per axis is measured free minus the
held peaks; a refusal names each short axis with its need and free amount, never a slot count.
``admit`` wraps ``decide`` with the grant ledger (``capacity-grants.jsonl``) and the refusal
ledger (``capacity-waits.jsonl``) under one lock, so concurrent grants sum their peaks. Axes:
``vram_mib``, ``ram_mb``, ``cores``. Never raises.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import capacity_record
from coordinator_core.group_em.atomic_record import holder_lock

# A grant is held until its run shows in a sample's jobs[]; a stamp can lag one watch tick, so a
# grant outlives the record TTL before it is dropped as never started.
GRANT_TTL_S = capacity_record.TTL_S
# A wait row is live while its run keeps re-asking: each refusal restamps ``seen_at``.
WAIT_TTL_S = 1800.0
_LOCK_TIMEOUT_S = 2.0
_AXES = ("vram_mib", "ram_mb", "cores")
PEAK_MEASURED = "measured"
PEAK_DECLARED = "declared"
# GPU rows are present but report no used VRAM (Apple unified memory): free VRAM cannot be derived.
GPU_USAGE_UNMEASURED = "gpu-usage-unmeasured"


def _box_dir(home: Optional[Path] = None) -> Path:
    if home is None:
        from coordinator_core._settings_home import settings_home

        home = settings_home()
    return Path(home) / "state" / "group-em" / "box"


def grants_path(home: Optional[Path] = None) -> Path:
    return _box_dir(home) / "capacity-grants.jsonl"


def waits_path(home: Optional[Path] = None) -> Path:
    return _box_dir(home) / "capacity-waits.jsonl"


def _num(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _rows(path: Path) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _write_rows(path: Path, rows: list[dict]) -> None:
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    atomic_write_bytes(path, body.encode("utf-8"))


def live_held(rows: list[dict], sample: dict, now: float) -> list[dict]:
    """``rows`` whose grant is unexpired and whose run is not yet in ``sample["jobs"]``."""
    started = {j.get("run") for j in (sample.get("jobs") or []) if isinstance(j, dict)}
    return [
        r
        for r in rows
        if r.get("run") not in started
        and (_num(r.get("granted_at")) or 0.0) + (_num(r.get("ttl_s")) or GRANT_TTL_S) > now
    ]


def read_held(sample: Optional[dict] = None, home: Optional[Path] = None, now: Optional[float] = None) -> list[dict]:
    """Unexpired grants under ``home`` whose run is not yet in ``sample["jobs"]``; never raises."""
    now = time.time() if now is None else now
    return live_held(_rows(grants_path(home)), sample if isinstance(sample, dict) else {}, now)


def _live_waits(rows: list[dict], now: float) -> list[dict]:
    return [w for w in rows if (_num(w.get("seen_at")) or _num(w.get("since")) or 0.0) + WAIT_TTL_S > now]


def read_waits(home: Optional[Path] = None, now: Optional[float] = None) -> list[dict]:
    """Unexpired refusal wait rows ``{run, job_type, requester_sid, priority, since, seen_at}``; never raises."""
    return _live_waits(_rows(waits_path(home)), time.time() if now is None else now)


def _peak_axes(job_peak: Optional[dict]) -> dict:
    if not isinstance(job_peak, dict):
        return {}
    return {a: v for a in _AXES if (v := _num(job_peak.get(a))) is not None}


def headroom(sample: dict, held: Optional[list] = None, now: Optional[float] = None) -> dict:
    """Free amount per axis after the held grants of ``sample``'s box.

    ``{vram_mib, ram_mb, cores, gpu_index, vram_reason}``: an axis the sample did not measure is
    None. ``vram_mib`` is the free VRAM of the freest GPU (a job runs on one GPU) and ``gpu_index``
    names it; ``vram_reason`` carries the sample's ``gpu_unavailable`` text when there is no GPU row.
    """
    sample = sample if isinstance(sample, dict) else {}
    box_id = sample.get("box_id")
    mine = [
        h
        for h in (held or [])
        if isinstance(h, dict) and (h.get("box_id") in (None, box_id))
    ]
    out: dict = {"vram_mib": None, "ram_mb": None, "cores": None, "gpu_index": None, "vram_reason": None}

    per_gpu: list[tuple[float, object]] = []
    for g in sample.get("gpus") or []:
        total, used = _num(g.get("vram_total_mib")), _num(g.get("vram_used_mib"))
        if total is None or used is None:
            continue
        taken = sum(_num(h.get("vram_mib")) or 0.0 for h in mine if h.get("gpu_index") == g.get("index"))
        per_gpu.append((total - used - taken, g.get("index")))
    if per_gpu:
        unassigned = sum(
            _num(h.get("vram_mib")) or 0.0
            for h in mine
            if h.get("gpu_index") is None or h.get("gpu_index") not in [i for _, i in per_gpu]
        )
        best = max(per_gpu, key=lambda p: p[0])
        out["vram_mib"], out["gpu_index"] = best[0] - unassigned, best[1]
    else:
        reason = sample.get("gpu_unavailable")
        if isinstance(reason, str):
            out["vram_reason"] = reason
        else:
            out["vram_reason"] = GPU_USAGE_UNMEASURED if sample.get("gpus") else capacity_record.GPU_NOT_SAMPLED

    ram = _num(sample.get("ram_avail_mb"))
    if ram is not None:
        out["ram_mb"] = ram - sum(_num(h.get("ram_mb")) or 0.0 for h in mine)

    cores = _num(sample.get("cores_logical")) or _num(sample.get("cores_physical"))
    load = _num(sample.get("load_pct"))
    if cores is not None and load is not None:
        out["cores"] = cores - cores * min(load, 100.0) / 100.0 - sum(_num(h.get("cores")) or 0.0 for h in mine)
    return out


def decide(
    box_sample: dict,
    job_peak: Optional[dict],
    held: Optional[list] = None,
    declared: Optional[dict] = None,
    now: Optional[float] = None,
) -> dict:
    """``{grant, box_id, headroom, short, peak_basis, gpu_index}`` for one job on one box.

    ``job_peak`` is a ``job_peaks.peak`` entry (measured). With none, ``declared`` (same axes) is
    used and ``peak_basis`` reads ``declared``; with neither, ``short`` is
    ``[{resource: "measurement", reason: "no_measured_peak"}]``. A missing or stale sample is
    ``reason: "no_fresh_sample"``. Otherwise ``short`` holds one ``{resource, need, free}`` per axis
    whose headroom is below the peak, in vram, ram, cores order; ``reason`` is set when the axis
    was never measured. Held grants for this box that already show in the sample's jobs are ignored.
    """
    sample = box_sample if isinstance(box_sample, dict) else {}
    now = time.time() if now is None else now
    box_id = sample.get("box_id")
    reply: dict = {"grant": False, "box_id": box_id, "headroom": {}, "short": [], "peak_basis": None, "gpu_index": None}

    need, basis = _peak_axes(job_peak), PEAK_MEASURED
    if not need:
        need, basis = _peak_axes(declared), PEAK_DECLARED
    if not need:
        reply["short"] = [{"resource": "measurement", "reason": "no_measured_peak"}]
        return reply
    reply["peak_basis"] = basis

    if sample.get("stale") is True:
        reply["short"] = [{"resource": "measurement", "reason": "no_fresh_sample"}]
        return reply

    free = headroom(sample, live_held([h for h in (held or []) if isinstance(h, dict)], sample, now), now)
    reply["headroom"] = {a: free[a] for a in _AXES}
    short = []
    for axis in _AXES:
        if axis not in need:
            continue
        have = free[axis]
        if have is None:
            entry = {"resource": axis.split("_")[0], "need": need[axis], "free": None}
            entry["reason"] = free["vram_reason"] if axis == "vram_mib" else "not_measured"
            short.append(entry)
        elif have < need[axis]:
            short.append({"resource": axis.split("_")[0], "need": need[axis], "free": have})
    reply["short"] = short
    if not short:
        reply["grant"] = True
        reply["gpu_index"] = free["gpu_index"] if "vram_mib" in need else None
    return reply


def admit(
    run: str,
    job_type: str,
    box_sample: dict,
    job_peak: Optional[dict],
    home: Optional[Path] = None,
    declared: Optional[dict] = None,
    requester_sid: Optional[str] = None,
    priority: Optional[str] = None,
    now: Optional[float] = None,
) -> dict:
    """Decide under the ledger lock, then record: a grant row on success, a wait row on refusal.

    Grants and waits live under ``home``'s ``state/group-em/box``. A repeat ``run`` is one row:
    a re-grant replaces its grant, a repeat refusal keeps the first ``since`` and restamps
    ``seen_at``; a wait unseen for ``WAIT_TTL_S`` is dropped. A grant clears the
    run's wait. A lock or I/O failure returns the decision unrecorded with ``recorded: False``.
    """
    now = time.time() if now is None else now
    gpath, wpath = grants_path(home), waits_path(home)
    try:
        gpath.parent.mkdir(parents=True, exist_ok=True)
        with holder_lock(gpath, timeout=_LOCK_TIMEOUT_S):
            rows = [r for r in _rows(gpath) if r.get("run") != run]
            sample = box_sample if isinstance(box_sample, dict) else {}
            live = live_held(rows, sample, now)
            reply = decide(sample, job_peak, live, declared, now)
            all_waits = _rows(wpath)
            waits = [w for w in _live_waits(all_waits, now) if w.get("run") != run]
            if reply["grant"]:
                axes = _peak_axes(job_peak) or _peak_axes(declared)
                live.append(
                    {
                        "run": run,
                        "job_type": job_type,
                        "box_id": reply["box_id"],
                        "gpu_index": reply["gpu_index"],
                        "granted_at": now,
                        "ttl_s": GRANT_TTL_S,
                        **{a: axes.get(a) for a in _AXES},
                    }
                )
                _write_rows(gpath, live)
                if len(waits) != len(all_waits):
                    _write_rows(wpath, waits)
            else:
                since = next((w.get("since") for w in _live_waits(all_waits, now) if w.get("run") == run), now)
                waits.append(
                    {
                        "run": run,
                        "job_type": job_type,
                        "box_id": reply["box_id"],
                        "requester_sid": requester_sid,
                        "priority": priority,
                        "since": since,
                        "seen_at": now,
                    }
                )
                _write_rows(wpath, waits)
                _write_rows(gpath, live)
        reply["recorded"] = True
    except Exception:  # contract: decide, never raise
        reply = decide(box_sample, job_peak, [], declared, now)
        reply.update(grant=False, short=[{"resource": "measurement", "reason": "ledger_unavailable"}], recorded=False)
    return reply

"""
coordinator_core.ops.group_em_fleet_capacity -- JSON-RPC "groupem.fleet_capacity",
"groupem.capacity_admit", "groupem.capacity_place", "groupem.idle_capacity_watch".

Purpose: the engine surface of measured-headroom scheduling. Report every box's stamped capacity
sample, admit a job when measured free headroom covers its measured peak, list the boxes that can
run a job now, and alert when a priority job waits while a capable box idles.

Negative-spec:
    - Never raises: an invalid param or I/O failure is `{"error": ...}`.
    - `groupem.fleet_capacity` fans out fleet-wide by default: one `groupem.capacity_local` invoke per
      far box through `box_transport.invoke_remote` (one ssh each, in parallel, per-box timeout
      `_PER_BOX_TIMEOUT_S` = 8s, clamped under the `ipc._timeout_for` backstop); `local_only` samples
      this box in-process and makes no network call. Its only extra write is the per-box last-known
      cache. The other three ops spawn nothing and make no network call: every read is a stamped
      record under a box root; `groupem.capacity_admit` writes its grant and wait ledgers under the
      target box's root.
    - No reply carries a slot or job count; a refusal names a measured axis or the missing
      measurement (`no_measured_peak`, `no_fresh_sample`).
    - Registration in `op_scopes.py`, `authz/classification.py` and the spawn audit is owned
      outside this module.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import box_registry, capacity_admit, capacity_record, idle_capacity, job_peaks, placement
from coordinator_core.ipc import register_op

_PEAK_AXES = ("ram_mb", "vram_mib", "cores")
_PER_BOX_TIMEOUT_S = 8.0


def _timeout_ceiling(ipc_module) -> float:
    """Largest per-box timeout whose fan-out (timeout x gather grace) still ends inside the op backstop."""
    backstop = ipc_module._timeout_for("groupem.fleet_capacity")
    return max(1.0, (backstop - 2.0) / 1.1)


def _str(p: dict, key: str) -> Optional[str]:
    v = p.get(key)
    return v if isinstance(v, str) and v else None


def _os_param(p: dict) -> tuple:
    """``(value, error)``: a platform name or a list of names, or None when absent."""
    v = p.get("os")
    if v is None:
        return None, None
    if isinstance(v, str) and v:
        return v, None
    if isinstance(v, list) and v and all(isinstance(x, str) and x for x in v):
        return v, None
    return None, "os must be a non-empty str or list of str"


def _declared(p: dict) -> tuple:
    """``(declared axes or None, error)``: only ram_mb, vram_mib, cores, each a non-negative number."""
    v = p.get("declared")
    if v is None:
        return None, None
    if not isinstance(v, dict):
        return None, "declared must be an object"
    out = {}
    for axis in _PEAK_AXES:
        if axis in v:
            n = v[axis]
            if isinstance(n, bool) or not isinstance(n, (int, float)) or n < 0:
                return None, f"declared.{axis} must be a non-negative number"
            out[axis] = n
    return (out or None), None


def _requirement(p: dict) -> tuple:
    v = p.get("requirement")
    if v is None:
        return None, None
    return (v, None) if isinstance(v, dict) else (None, "requirement must be an object")


def _boxes(home: Path) -> list[dict]:
    """Registered boxes plus this box, one row per physical box (``fleet_live.registered_boxes``)."""
    from coordinator_core.group_em import fleet_live

    return fleet_live.registered_boxes(home)


def _by_local_id(rows: list[dict], alias: dict) -> list[dict]:
    """``rows`` with a registry id that names this box rewritten to the local id, one row per box."""
    out, seen = [], set()
    for r in rows:
        bid = alias.get(r["box_id"], r["box_id"])
        if bid not in seen:
            seen.add(bid)
            out.append({**r, "box_id": bid})
    return out


def _refresh_local() -> bool:
    """Stamp this box's sample in-process (no spawn); False when it could not."""
    from coordinator_core.group_em import watch

    return bool(watch.capacity_tick())


def _fleet_row(box: dict, local_id: str) -> dict:
    # A far POSIX root is not absolute here; opening it would resolve under this drive and read this box's stamp.
    readable = Path(str(box.get("root"))).is_absolute()
    row = capacity_record.read(box) if readable else {"box_id": box.get("box_id"), "stale": True, "age_s": None}
    refreshed = False
    if row.get("stale") is not False and box.get("box_id") == local_id and _refresh_local():
        row, refreshed = capacity_record.read(box), True
    row["box_id"] = box.get("box_id")
    if row.get("stale") is False:
        row["cores"] = row.get("cores_logical") or row.get("cores_physical")
        if "gpus" not in row and "gpu_unavailable" not in row:
            row["gpu_unavailable"] = capacity_record.GPU_NOT_SAMPLED
    if refreshed:
        row["refreshed"] = True
    return row


@register_op("groupem.fleet_capacity")
def _groupem_fleet_capacity(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.fleet_capacity" handler: every box's measured capacity, maxima and live use.

    Params:
        local_only (bool, optional) -- sample this box only (no ssh); default fans out to every box.
        timeout (number > 0, optional) -- per-box seconds; default 8, clamped under the ipc backstop.

    Returns:
        {"local_box_id", "boxes": [{box_id, os, arch, gpus | gpu_unavailable, ram_total_mb,
        ram_avail_mb, cores, cores_physical, load_pct, jobs, age_s, stale}, ...],
        "live": [fleet_live row per box: alias, os, state, maxima, current use, sampled_at, last_seen],
        "table": str (``fleet_live.render``), "power_on": [str per UNREACHABLE box]} or {"error": str}.
        A box with no fresh sample reads ``stale: true`` with its ``age_s`` (None when never
        stamped) and no measured fields. This box's stale sample is re-stamped in-process first.
        A box that times out, is unreachable or lacks the op keeps its last-known maxima in ``live``.
    """
    p = params if isinstance(params, dict) else {}
    local_only = p.get("local_only", False)
    if not isinstance(local_only, bool):
        return {"error": "local_only must be bool"}
    timeout = p.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
        return {"error": "timeout must be a positive number"}
    try:
        from coordinator_core import ipc
        from coordinator_core.group_em import fleet_live

        home = settings_home()
        local = box_registry.local_box_id(home)
        boxes = fleet_live.known_boxes(home)
        per_box = min(float(timeout) if timeout is not None else _PER_BOX_TIMEOUT_S, _timeout_ceiling(ipc))
        live = fleet_live.gather([] if local_only else boxes, timeout=per_box, local_id=local)
        return {
            "local_box_id": local,
            "boxes": [_fleet_row(b, local) for b in boxes],
            "live": live,
            "table": fleet_live.render(live),
            "power_on": fleet_live.power_on_items(live),
        }
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.capacity_admit")
def _groupem_capacity_admit(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.capacity_admit" handler: grant when measured headroom covers the measured peak.

    Params:
        job_type (str) -- the job type whose recorded peak is admitted.
        run (str, optional) -- the run label the grant is held under; minted when absent.
        box_id (str, optional) -- admit on this box only; absent, the capable box with most headroom.
        os (str | list, optional), requirement (dict, optional) -- restrict the candidate boxes.
        declared (dict, optional) -- ram_mb / vram_mib / cores used only when no peak is measured;
            the reply labels the basis ``declared``.
        requester_sid (str, optional), priority (str | number | bool, optional) -- recorded on a wait row.

    Returns:
        {grant, run, job_type, box_id, headroom, short, peak_basis, gpu_index, recorded} plus, on
        a refusal, ``capable_elsewhere`` (``placement.candidates`` minus the refused box); or
        {"error": str}. ``short`` holds {resource, need, free} per axis, or
        {resource: "measurement", reason}.
    """
    p = params if isinstance(params, dict) else {}
    job_type = _str(p, "job_type")
    if job_type is None:
        return {"error": "job_type (non-empty str) is required"}
    run, box_id = p.get("run"), p.get("box_id")
    for name, v in (("run", run), ("box_id", box_id), ("requester_sid", p.get("requester_sid"))):
        if v is not None and not isinstance(v, str):
            return {"error": f"{name} must be str"}
    priority = p.get("priority")
    if priority is not None and not isinstance(priority, (str, int, float, bool)):
        return {"error": "priority must be str, number or bool"}
    os_value, err = _os_param(p)
    declared, err2 = _declared(p)
    requirement, err3 = _requirement(p)
    if err or err2 or err3:
        return {"error": err or err2 or err3}
    try:
        home = settings_home()
        run = run or f"{job_type}-{uuid.uuid4().hex[:8]}"
        peak = job_peaks.peak(job_type, home)
        cands = placement.candidates(
            job_type, os=os_value, home=home, declared=declared, requirement=requirement
        )
        box_rows = _boxes(home)
        boxes = {b["box_id"]: b for b in box_rows}
        alias = {b["registered_as"]: b["box_id"] for b in box_rows if b.get("registered_as")}
        cands = _by_local_id(cands, alias)
        target = box_id or (cands[0]["box_id"] if cands else box_registry.local_box_id(home))
        target = alias.get(target, target)
        box = boxes.get(target)
        if box is None:
            return {"error": f"box_id {target!r} is not registered"}
        sample = capacity_record.read(box)
        if sample.get("box_id") is None:
            sample["box_id"] = target
        reply = capacity_admit.admit(
            run,
            job_type,
            sample,
            peak,
            home=Path(box["root"]),
            declared=declared,
            requester_sid=p.get("requester_sid"),
            priority=priority,
        )
        reply.update(run=run, job_type=job_type, box_id=target)
        if not reply["grant"]:
            reply["capable_elsewhere"] = [c for c in cands if c["box_id"] != target]
        return reply
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.capacity_place")
def _groupem_capacity_place(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.capacity_place" handler: every box that can run the job now.

    Params:
        job_type (str), os (str | list, optional), requirement (dict, optional),
        declared (dict, optional) -- as ``groupem.capacity_admit``.

    Returns:
        {"job_type", "boxes": [{box_id, os, headroom, gpu_index, peak_basis}, ...]} most headroom
        first; ``reason: "no_measured_peak"`` is added when nothing is measured or declared;
        or {"error": str}.
    """
    p = params if isinstance(params, dict) else {}
    job_type = _str(p, "job_type")
    if job_type is None:
        return {"error": "job_type (non-empty str) is required"}
    os_value, err = _os_param(p)
    declared, err2 = _declared(p)
    requirement, err3 = _requirement(p)
    if err or err2 or err3:
        return {"error": err or err2 or err3}
    try:
        home = settings_home()
        boxes = placement.candidates(
            job_type, os=os_value, home=home, declared=declared, requirement=requirement
        )
        out: dict = {"job_type": job_type, "boxes": boxes}
        if not boxes and declared is None and job_peaks.peak(job_type, home) is None:
            out["reason"] = "no_measured_peak"
        return out
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.idle_capacity_watch")
def _groupem_idle_capacity_watch(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.idle_capacity_watch" handler: priority waits a capable idle box could run now.

    Returns:
        {"alerts": [{run, job_type, requester_sid, holder_sid, idle_box_id, waited_s, headroom}, ...],
        "waits": <wait rows read>, "boxes": <samples read>} or {"error": str}. Sends nothing.
    """
    try:
        from coordinator_core.group_em import nomination

        home = settings_home()
        waits = capacity_admit.read_waits(home)
        boxes = _boxes(home)
        samples = [capacity_record.read(b) for b in boxes]
        held = {
            s.get("box_id"): capacity_admit.read_held(s, Path(b["root"]))
            for b, s in zip(boxes, samples)
            if isinstance(s, dict) and isinstance(s.get("box_id"), str)
        }
        peaks = {}
        for w in waits:
            jt = w.get("job_type")
            if isinstance(jt, str) and jt not in peaks:
                found = job_peaks.peak(jt, home)
                if found is not None:
                    peaks[jt] = found
        # box_holder() snapshots the external session registry (hundreds of ms on a busy box); with no
        # waiting run there is no alert to attribute, so the common idle poll never pays it.
        holder = nomination.box_holder() if waits else None
        alerts = idle_capacity.scan(waits, samples, peaks, holder, now=time.time(), held=held)
        return {"alerts": alerts, "waits": len(waits), "boxes": len(samples)}
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}

"""
coordinator_core.ops.group_em_job_ledger -- JSON-RPC "groupem.job_ledger" and "groupem.job_ledger_local".

Purpose: the fleet job ledger. Per box, every running heavy process joined to the GO grant booked for
it, with owner, GO id, pid, command, window, deadline, rate and CPU / RAM / VRAM / IO, and flags for
an unbooked heavy process, a job past its window and a job behind its expected rate.

Negative-spec:
    - Read-only: both ops write nothing. ``groupem.job_ledger_local`` joins ``proc_census.read()`` to
      ``capacity_admit.read_booked`` (every pid-booked grant, started or expired), never ``read_held``.
    - ``groupem.job_ledger`` fans out fleet-wide by default: one ``groupem.job_ledger_local`` invoke per
      far box through ``box_transport.invoke_remote`` on its own thread, per-box timeout clamped by
      ``group_em_fleet_capacity._timeout_ceiling``; ``local_only`` makes no network call. A far box that
      fails keeps its row with a ``reason`` and no jobs; it is never dropped.
    - Never raises: an invalid param or I/O failure is ``{"error": ...}``.
    - Registration in ``op_scopes.py`` and ``authz/classification.py`` is owned outside this module.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import box_registry, capacity_admit, job_ledger, proc_census
from coordinator_core.ipc import register_op

_PER_BOX_TIMEOUT_S = 8.0
_GATHER_GRACE = 1.1
_REASONS = ("timed_out", "op_missing")
_UNREACHABLE = "unreachable"


def _classify_input(row: dict) -> dict:
    """A ``proc_census`` row in the shape ``job_ledger.classify`` reads (rates are lifetime averages)."""
    age = row.get("age_s")
    out = {
        "pid": row.get("pid"),
        "ppid": row.get("ppid"),
        "started_at": row.get("start"),
        "cmdline": row.get("cmdline") or row.get("comm"),
        "cpu_pct": row.get("pcpu"),
        "rss_mb": row.get("rss_mb"),
        "vram_mib": row.get("vram_mb"),
        "io_read_bps": None,
        "io_write_bps": None,
    }
    if isinstance(age, (int, float)) and age > 0:
        for key, src in (("io_read_bps", "io_read_bytes"), ("io_write_bps", "io_write_bytes")):
            v = row.get(src)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[key] = v / age
    return out


def _threshold_settings() -> dict:
    """The machine-local ``group_em.job_ledger.heavy_*`` keys as numbers; unreadable or non-numeric keys are omitted."""
    from coordinator_core import machine_resolver

    out: dict = {}
    try:
        flat = machine_resolver.merged_flat_registry()
    except Exception:  # noqa: BLE001 -- thresholds fall back to defaults
        return out
    for key, raw in (flat or {}).items():
        if isinstance(key, str) and key.startswith(job_ledger.SETTINGS_PREFIX):
            try:
                out[key] = float(raw)
            except (TypeError, ValueError):
                continue
    return out


def _local_ledger(home: Optional[Path] = None, now: Optional[float] = None) -> dict[str, Any]:
    home = settings_home() if home is None else home
    now = time.time() if now is None else now
    census = proc_census.read(now=now)
    procs = [_classify_input(r) for r in census.get("processes") or []]
    jobs = job_ledger.classify(procs, capacity_admit.read_booked(home), now,
                          job_ledger.resolve_thresholds(_threshold_settings()))
    return {
        "box_id": box_registry.local_box_id(home),
        "state": "ok",
        "sampled_at": now,
        "jobs": jobs,
        "flagged": sum(1 for j in jobs if j.get("flags")),
        "unavailable": census.get("unavailable") or {},
    }


def _far_row(record: dict, timeout: float, invoke) -> dict:
    """One far box's ledger row; a failure keeps the row with ``state`` and ``reason``."""
    from coordinator_core.group_em import fleet_live

    base = {"box_id": record["box_id"], "alias": fleet_live.box_alias(record), "jobs": [], "flagged": 0}
    if record.get("roster_only"):
        return {**base, "state": _UNREACHABLE, "reason": _UNREACHABLE, "detail": "no_route"}
    res = invoke(record, "groupem.job_ledger_local", {}, timeout=timeout)
    if not isinstance(res, dict) or not res.get("ok"):
        state, detail = fleet_live._state_for_failure(res if isinstance(res, dict) else {})
        state = state if state in _REASONS else _UNREACHABLE
        return {**base, "state": state, "reason": state, "detail": detail}
    reply = res.get("reply")
    if not isinstance(reply, dict) or not isinstance(reply.get("jobs"), list):
        err = reply.get("error") if isinstance(reply, dict) else None
        return {**base, "state": _UNREACHABLE, "reason": _UNREACHABLE, "detail": str(err or "bad_reply")}
    return {**base, **{k: reply[k] for k in ("sampled_at", "flagged", "unavailable") if k in reply},
            "state": "ok", "jobs": reply["jobs"]}


@register_op("groupem.job_ledger_local")
def _groupem_job_ledger_local(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.job_ledger_local" handler: this box's running heavy jobs and their GO match.

    Returns:
        {"box_id", "state": "ok", "sampled_at", "jobs": [{owner_sid, go_id, pid, cmdline, started_at,
        window_s, deadline, expected_rate, progress, rate_so_far, cpu_pct, rss_mb, vram_mib,
        io_read_bps, io_write_bps, flags}, ...], "flagged", "unavailable"} or {"error": str}.
        ``flags`` holds ``unbooked``, ``over_window``, ``behind_rate`` or ``exited``; ``[]`` is clean.
    """
    try:
        return _local_ledger()
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.job_ledger")
def _groupem_job_ledger(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.job_ledger" handler: every box's running heavy jobs, flagged.

    Params:
        local_only (bool, optional) -- this box only (no ssh); default fans out to every box.
        timeout (number > 0, optional) -- per-box seconds; default 8, clamped under the ipc backstop.

    Returns:
        {"local_box_id", "boxes": [{box_id, alias, state, jobs, flagged, ...}, ...], "jobs": total,
        "flagged": total} or {"error": str}. A far box that times out, lacks the op or cannot be
        reached keeps its row: ``state`` and ``reason`` are ``timed_out``, ``op_missing`` or
        ``unreachable`` (``detail`` the transport reason) and ``jobs`` is empty.
    """
    p = params if isinstance(params, dict) else {}
    local_only = p.get("local_only", False)
    if not isinstance(local_only, bool):
        return {"error": "local_only must be bool"}
    timeout = p.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
        return {"error": "timeout must be a positive number"}
    try:
        from concurrent.futures import ThreadPoolExecutor, wait

        from coordinator_core import ipc
        from coordinator_core.group_em import box_transport, fleet_live
        from coordinator_core.ops.group_em_fleet_capacity import _timeout_ceiling

        home = settings_home()
        local = box_registry.local_box_id(home)
        per_box = min(float(timeout) if timeout is not None else _PER_BOX_TIMEOUT_S, _timeout_ceiling(ipc))
        uniq: dict = {}
        for r in fleet_live.known_boxes(home):
            if isinstance(r, dict) and isinstance(r.get("box_id"), str):
                uniq.setdefault(r["box_id"], r)
        uniq.setdefault(local, {"box_id": local})
        far = [] if local_only else [r for bid, r in uniq.items() if bid != local]
        pool = ThreadPoolExecutor(max_workers=max(1, len(far)))
        futures = {r["box_id"]: pool.submit(_far_row, r, per_box, box_transport.invoke_remote) for r in far}
        mine = _local_ledger(home)
        mine["alias"] = fleet_live.box_alias(uniq[local])
        wait(list(futures.values()), timeout=per_box * _GATHER_GRACE)
        pool.shutdown(wait=False, cancel_futures=True)
        rows: list[dict[str, Any]] = [mine]
        for r in far:
            fut = futures[r["box_id"]]
            if fut.done() and fut.exception() is None:
                rows.append(fut.result())
                continue
            detail = "timeout" if not fut.done() else f"{type(fut.exception()).__name__}: {fut.exception()}"
            state = "timed_out" if not fut.done() else _UNREACHABLE
            rows.append({"box_id": r["box_id"], "alias": fleet_live.box_alias(r), "jobs": [], "flagged": 0,
                         "state": state, "reason": state, "detail": detail})
        return {
            "local_box_id": local,
            "boxes": rows,
            "jobs": sum(len(b["jobs"]) for b in rows),
            "flagged": sum(b.get("flagged") or 0 for b in rows),
        }
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}

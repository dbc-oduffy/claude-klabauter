"""Fleet idle-balance decision: does a capable box idle while another box has work?

``scan(rows, waits_by_box, grants_by_box, dwell, now)`` is a pure decision over records already
read; it spawns nothing, reads no file and never raises. ``rows`` are ``fleet_live.FIELDS`` rows (the
persisted fleet live snapshot): ``box``, ``state``, ``sampled_at``, optional ``age_s``, ``cores``,
``cpu_pct``, ``load5``, ``gpu_util_pct`` and ``gpus[]`` (``util_pct``). The caller carries ``dwell``
(``{box: idle-since epoch}``) across single-shot ticks and persists the returned one.

Snapshot constants: ``IDLE_UTIL_PCT`` (GPU util below it is idle; the value ``idle_capacity``
uses), ``IDLE_LOAD_PCT`` (half the cores free), ``BUSY_LOAD_PCT`` (heavily loaded),
``IDLE_DWELL_S`` (900 s of unbroken idleness before an alert), ``FRESH_S`` (a sample older than
this is stale).

Two idle definitions coexist on purpose. ``idle_capacity.scan`` (``groupem.idle_capacity_watch``)
calls a box idle at CPU load < 10 and alerts only on a priority wait with a measured job peak. This
module (the watch tick's ``IDLE-BOX`` line) calls a box idle at a GPU under 10 percent or half the
cores free, needs no priority and no peak. ``IDLE-BOX`` is advisory, never a refusal path. A stale
or unreachable box is never idle and its load never counts as work.
"""

from __future__ import annotations

import time
from typing import Mapping, Optional

IDLE_UTIL_PCT = 10.0
IDLE_LOAD_PCT = 50.0
BUSY_LOAD_PCT = 80.0
IDLE_DWELL_S = 900.0
FRESH_S = 300.0
_STATE_OK = "ok"


def _num(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fresh(row: dict, now: float) -> bool:
    age = _num(row.get("age_s"))
    if age is None:
        sampled = _num(row.get("sampled_at"))
        if sampled is None:
            return False
        age = now - sampled
    return age <= FRESH_S


def load_pct(row: dict) -> Optional[float]:
    """The row's CPU load percent: ``cpu_pct``, else ``load5 / cores * 100``; None when unmeasured."""
    cpu = _num(row.get("cpu_pct"))
    if cpu is not None:
        return cpu
    load5, cores = _num(row.get("load5")), _num(row.get("cores"))
    if load5 is None or cores is None or cores <= 0:
        return None
    return load5 / cores * 100.0


def _gpu_idle(row: dict) -> bool:
    gpus = row.get("gpus")
    for gpu in gpus if isinstance(gpus, list) else []:
        util = _num(gpu.get("util_pct")) if isinstance(gpu, dict) else None
        if util is not None and util < IDLE_UTIL_PCT:
            return True
    util = _num(row.get("gpu_util_pct"))
    return util is not None and util < IDLE_UTIL_PCT


def is_idle(row: object, now: Optional[float] = None) -> bool:
    """True for a fresh ``ok`` row with a GPU under ``IDLE_UTIL_PCT`` or load at most ``IDLE_LOAD_PCT``."""
    try:
        if not isinstance(row, dict) or row.get("state") != _STATE_OK:
            return False
        if not _fresh(row, time.time() if now is None else now):
            return False
        load = load_pct(row)
        return _gpu_idle(row) or (load is not None and load <= IDLE_LOAD_PCT)
    except Exception:  # contract: never raises
        return False


def has_work(row: object, waits: object, grants: object, now: Optional[float] = None) -> bool:
    """True when the box has a wait or grant row, or a fresh ``ok`` sample at ``BUSY_LOAD_PCT`` or more."""
    try:
        if _rows(waits) or _rows(grants):
            return True
        if not isinstance(row, dict) or row.get("state") != _STATE_OK:
            return False
        if not _fresh(row, time.time() if now is None else now):
            return False
        load = load_pct(row)
        return load is not None and load >= BUSY_LOAD_PCT
    except Exception:  # contract: never raises
        return False


def _rows(value: object) -> bool:
    return isinstance(value, (list, tuple)) and any(isinstance(r, dict) for r in value)


def scan(
    rows: object,
    waits_by_box: Optional[Mapping[str, list]],
    grants_by_box: Optional[Mapping[str, list]],
    dwell: Optional[Mapping[str, float]],
    now: Optional[float] = None,
) -> tuple[list[dict], dict]:
    """``(alerts, dwell)``: one alert per box idle for ``IDLE_DWELL_S`` while another box has work.

    An alert is ``{idle_box, idle_s, busy_boxes}``. The returned dwell keeps the idle-since epoch of
    each box that is still idle and drops every other box, so a box that stops idling restarts its
    clock. Two rows naming one box count once. On any failure it returns ``([], dwell as given)``.
    """
    prior = dict(dwell) if isinstance(dwell, Mapping) else {}
    try:
        clock = time.time() if now is None else now
        waits = waits_by_box if isinstance(waits_by_box, Mapping) else {}
        grants = grants_by_box if isinstance(grants_by_box, Mapping) else {}
        boxes: dict = {}
        for row in rows if isinstance(rows, (list, tuple)) else []:
            box = row.get("box") if isinstance(row, dict) else None
            if isinstance(box, str) and box and box not in boxes:
                boxes[box] = row
        busy = [b for b, r in boxes.items() if has_work(r, waits.get(b), grants.get(b), clock)]
        carried: dict = {}
        alerts: list[dict] = []
        for box, row in boxes.items():
            if not is_idle(row, clock):
                continue
            since = _num(prior.get(box))
            carried[box] = clock if since is None else since
            idle_s = clock - carried[box]
            others = [b for b in busy if b != box]
            if idle_s >= IDLE_DWELL_S and others:
                alerts.append({"idle_box": box, "idle_s": idle_s, "busy_boxes": others})
        return alerts, carried
    except Exception:  # contract: scan never raises
        return [], prior

"""Record contract for one box's row in the live fleet-capacity snapshot.

``FIELDS`` is the per-box key set every row carries, in render order. A row's ``state`` is one of
``STATES``. Maxima fields survive an unreachable box (carried from a last-known record, with
``maxima_source`` naming where they came from); current-use fields are ``None`` there, never stale.
The per-box last-known cache (``read_last_known`` / ``write_last_known``) lives at
``LAST_KNOWN_PATH`` (resolved lazily; ``last_known_path()``).
``gpus[]`` rows are the ``gpu_sample`` shape (``index, name, vram_total_mib, vram_used_mib,
util_pct``); ``disks[]`` rows are ``{mount, total_gb, free_gb, floor_gb}``.
A box whose cores differ in class declares ``core_classes`` (``{"p": [core idx], "e": [...]}``) in its
``box.json``; its row then carries ``core_classes`` and ``cores_by_class`` (maxima) and
``free_cores_by_class`` (use). A box with no declaration carries none of the three: a count only.
The persisted live snapshot (``live_snapshot_path`` / ``write_live_snapshot`` / ``read_live_snapshot``)
is what the idle-balance tick reads.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes

STATE_OK = "ok"
STATE_TIMED_OUT = "timed_out"
STATE_UNREACHABLE = "unreachable"
STATE_ERROR = "error"
STATE_OP_MISSING = "op_missing"
STATE_BUSY = "busy"
STATES = (STATE_OK, STATE_TIMED_OUT, STATE_UNREACHABLE, STATE_ERROR, STATE_OP_MISSING, STATE_BUSY)

IDENTITY_FIELDS = ("alias", "os")
MAXIMA_FIELDS = ("cores", "cpu_model", "ram_total_mb", "commit_limit_mb", "gpus", "disks",
                 "core_classes", "cores_by_class")
USE_FIELDS = (
    "cpu_pct",
    "load5",
    "ram_free_mb",
    "commit_used_mb",
    "vram_used_mib",
    "gpu_util_pct",
    "free_cores_by_class",
    "sessions",
    "jobs",
    "gpu_slot_holder",
    "editor_slot_holder",
    "top_cpu",
    "top_ram",
)
FIELDS = (
    ("box",) + IDENTITY_FIELDS + ("state",) + MAXIMA_FIELDS + USE_FIELDS
    + ("maxima_source", "sampled_at", "last_seen")
)

LAST_KNOWN_RELPATH = Path("group-em") / "fleet-capacity-cache.json"


def last_known_path(home: Optional[Path] = None) -> Path:
    """Stable per-box last-known cache path under settings_home(); DoE's render reads it."""
    return (Path(home) if home is not None else settings_home()) / LAST_KNOWN_RELPATH


def __getattr__(name: str) -> Path:
    if name == "LAST_KNOWN_PATH":
        return last_known_path()
    raise AttributeError(name)

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
    for f in IDENTITY_FIELDS:
        row[f] = lk.get(f)
    row["last_seen"] = lk.get("last_seen") if lk.get("last_seen") is not None else lk.get("sampled_at")
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
    if row["state"] == STATE_OK and row["last_seen"] is None:
        row["last_seen"] = row["sampled_at"]
    return row


def _label(host: object) -> Optional[str]:
    """First DNS label of an ssh host (``user@`` stripped, lowercased); None for an IP literal or empty."""
    if not isinstance(host, str):
        return None
    h = host.rsplit("@", 1)[-1].strip().lower()
    if not h or h.startswith("[") or re.fullmatch(r"[\d.]+", h):
        return None
    return h.split(".", 1)[0] or None


def box_alias(record: Optional[dict], local_hostname: Optional[str] = None) -> str:
    """Friendly name for a registry ``record``: its ``alias``, else the transport host's first DNS
    label, else the local hostname's (a record with no ssh transport is this box)."""
    rec = record if isinstance(record, dict) else {}
    alias = rec.get("alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip().lower()
    t = rec.get("transport")
    if isinstance(t, dict) and t.get("kind") == "ssh":
        name = _label(t.get("host"))
        if name:
            return name
    return _label(local_hostname if local_hostname is not None else socket.gethostname()) or "local"


def maxima_record(row: dict) -> dict:
    """The persistable last-known record of an ``ok`` ``row``: identity, maxima and ``last_seen``."""
    rec = {f: row.get(f) for f in IDENTITY_FIELDS + MAXIMA_FIELDS}
    rec["gpus"] = _max_gpus(rec["gpus"])
    rec["disks"] = _max_disks(rec["disks"])
    rec["last_seen"] = row.get("last_seen") if row.get("last_seen") is not None else row.get("sampled_at")
    return rec


def read_last_known(path: Optional[Path] = None) -> dict:
    """``{box: maxima record}`` from the cache; a missing, unreadable or malformed file reads as ``{}``."""
    try:
        data = json.loads(Path(path if path is not None else last_known_path()).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def write_last_known(boxes: dict, path: Optional[Path] = None) -> None:
    """Atomically replace the cache with ``boxes`` (``{box: maxima record}``)."""
    target = Path(path if path is not None else last_known_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, (json.dumps(boxes, indent=2, sort_keys=True) + "\n").encode("utf-8"))


LIVE_SNAPSHOT_RELPATH = Path("group-em") / "fleet-live-snapshot.json"


def live_snapshot_path(home: Optional[Path] = None) -> Path:
    """Where the persisted fleet live snapshot lives under settings_home()."""
    return (Path(home) if home is not None else settings_home()) / LIVE_SNAPSHOT_RELPATH


def write_live_snapshot(rows: list, now: float, path: Optional[Path] = None) -> None:
    """Merge ``rows`` (one per box, keyed by ``box``) into the snapshot, each stamped ``observed_at=now``.

    A box this call did not sample keeps its prior entry and its older ``observed_at``, so a
    ``local_only`` call never erases the far boxes. Atomic; raises OSError on an unwritable target.
    """
    from coordinator_core.group_em.atomic_record import LockTimeout, holder_lock

    target = Path(path if path is not None else live_snapshot_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with holder_lock(target, timeout=2.0):
            try:
                prior = json.loads(target.read_text("utf-8")).get("boxes", {})
            except (OSError, ValueError, AttributeError):
                prior = {}
            boxes = {k: v for k, v in prior.items() if isinstance(k, str) and isinstance(v, dict)} \
                if isinstance(prior, dict) else {}
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("box"), str):
                    boxes[row["box"]] = {"observed_at": now, "row": row}
            atomic_write_bytes(target, (json.dumps({"boxes": boxes}, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    except LockTimeout as exc:
        raise OSError(str(exc)) from exc


def read_live_snapshot(max_age_s: float, now: Optional[float] = None, path: Optional[Path] = None) -> list:
    """Every persisted row, each with ``age_s`` (seconds since its sample, else since observed) and
    ``stale`` (``age_s > max_age_s``); a stale row keeps its state and is never free capacity. A
    missing or malformed file reads as ``[]``. Never raises.
    """
    stamp = time.time() if now is None else now
    try:
        data = json.loads(Path(path if path is not None else live_snapshot_path()).read_text("utf-8"))
        entries = data.get("boxes", {}).values()
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for entry in entries:
        row = entry.get("row") if isinstance(entry, dict) else None
        if not isinstance(row, dict):
            continue
        sampled, seen = row.get("sampled_at"), entry.get("observed_at")
        base = sampled if isinstance(sampled, (int, float)) and not isinstance(sampled, bool) else seen
        age = max(0.0, stamp - base) if isinstance(base, (int, float)) and not isinstance(base, bool) else None
        out.append({**row, "age_s": age, "stale": age is None or age > max_age_s})
    return sorted(out, key=lambda r: (str(r.get("alias")), str(r.get("box"))))


def _host_label(record: dict) -> Optional[str]:
    t = record.get("transport")
    return _label(t.get("host")) if isinstance(t, dict) and t.get("kind") == "ssh" else None


def _root_is_this_home(root: object, home_root: str) -> bool:
    """A registry root names this box only when it is an absolute path HERE and resolves to ``home``;
    a far POSIX root resolves under the current drive on Windows and must not match."""
    try:
        path = Path(str(root))
        return path.is_absolute() and str(path.resolve()) == home_root
    except (OSError, ValueError):
        return False


def registered_boxes(home: Optional[Path] = None) -> list:
    """Registered boxes plus this box, one record per physical box, never a name list.

    A record whose id is the local id or whose root is this home is rewritten to the local id
    (``registered_as`` keeps the registry id); records sharing a transport host or a folded id
    collapse to the freshest ``registered_at``, the losers' ids kept under ``aliases``.
    """
    from coordinator_core.group_em import box_registry

    base = Path(home) if home is not None else settings_home()
    local = box_registry.local_box_id(base)
    home_root = str(base.resolve())
    records = [dict(b) for b in box_registry.read_boxes(base / "state" / "group-em")]
    merged: dict = {}
    for rec in records:
        if rec.get("box_id") == local or _root_is_this_home(rec.get("root"), home_root):
            if rec.get("box_id") != local:
                rec["registered_as"] = rec.get("box_id")
                rec["box_id"] = local
            key = ("local",)
        else:
            key = ("host", _host_label(rec)) if _host_label(rec) else ("id", box_registry.fold_box_id(rec["box_id"]))
        kept = merged.get(key)
        if kept is None:
            merged[key] = rec
            continue
        newer, older = (rec, kept) if rec.get("registered_at", 0) > kept.get("registered_at", 0) else (kept, rec)
        newer["aliases"] = sorted({*newer.get("aliases", []), *older.get("aliases", []), older["box_id"]}
                                  - {newer["box_id"]})
        merged[key] = newer
    out = list(merged.values())
    if ("local",) not in merged:
        out.append({"box_id": local, "root": str(base)})
    return out


BOX_SPECS_RELPATH = Path("state") / "box-specs.json"


def box_specs_roster(home: Optional[Path] = None) -> list:
    """Peer names SessionStart BOX SPECS reads: ``peers`` of ``<home>/state/box-specs.json``, else the
    keys of its ``boxes``. A file read; a missing or malformed file reads as ``[]``."""
    try:
        data = json.loads(((Path(home) if home is not None else settings_home()) / BOX_SPECS_RELPATH).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    names = data.get("peers")
    if not isinstance(names, list):
        names = list(data.get("boxes") or []) if isinstance(data.get("boxes"), dict) else []
    return [n for n in names if isinstance(n, str) and n.strip()]


def _names_of(rec: dict) -> set:
    from coordinator_core.group_em import box_registry

    raw = [rec.get("box_id"), rec.get("registered_as"), rec.get("alias"), _host_label(rec), *(rec.get("aliases") or [])]
    return {box_registry.fold_box_id(n) for n in raw if isinstance(n, str) and n}


def known_boxes(home: Optional[Path] = None) -> list:
    """``registered_boxes`` plus a ``roster_only`` record (no transport, ``alias`` = the roster name) for
    every BOX SPECS peer no registry record or this host names; ``gather`` renders those UNREACHABLE."""
    from coordinator_core.group_em import box_registry

    out = registered_boxes(home)
    known = set().union(*(_names_of(r) for r in out))
    host = _label(socket.gethostname())
    if host:
        known.add(host)
    for name in box_specs_roster(home):
        key = box_registry.fold_box_id(name.strip())
        if key not in known:
            known.add(key)
            out.append({"box_id": name.strip(), "alias": key, "roster_only": True})
    return out


FREE_CORE_BUSY_PCT = 25.0
_CORE_GAP_S = 0.1
_CORE_REUSE_S = (0.2, 120.0)
_core_prev: Optional[tuple] = None


def valid_core_classes(value: object) -> Optional[dict]:
    """``{"p": [idx...], "e": [idx...]}`` when ``value`` names two or more disjoint non-empty classes of
    non-negative core indexes; None otherwise (a homogeneous or undeclared box)."""
    if not isinstance(value, dict):
        return None
    out, seen = {}, set()
    for name, idx in value.items():
        if not isinstance(name, str) or not isinstance(idx, list) or not idx:
            return None
        if any(isinstance(i, bool) or not isinstance(i, int) or i < 0 or i in seen for i in idx):
            return None
        seen.update(idx)
        out[name] = list(idx)
    return out if len(out) >= 2 else None


def read_core_classes(home: Optional[Path] = None) -> Optional[dict]:
    """This box's declared ``core_classes`` from ``<home>/state/group-em/box.json`` (top level or under
    ``capability``); None when undeclared. Never guesses a split."""
    from coordinator_core.group_em import box_registry

    try:
        data = json.loads(box_registry._identity_path(home).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cap = data.get("capability")
    return valid_core_classes(data.get("core_classes") or (cap.get("core_classes") if isinstance(cap, dict) else None))


def _core_times() -> Optional[list]:
    """Per logical core ``(idle, total)`` cumulative ticks; None on a host with no reader. No spawn."""
    if os.name == "nt":
        import ctypes

        class _Perf(ctypes.Structure):
            _fields_ = [("idle", ctypes.c_int64), ("kernel", ctypes.c_int64), ("user", ctypes.c_int64),
                        ("dpc", ctypes.c_int64), ("interrupt", ctypes.c_int64), ("count", ctypes.c_ulong)]

        buf = (_Perf * (os.cpu_count() or 1))()
        got = ctypes.c_ulong()
        if ctypes.WinDLL("ntdll").NtQuerySystemInformation(8, buf, ctypes.sizeof(buf), ctypes.byref(got)) != 0:
            return None
        return [(p.idle, p.kernel + p.user) for p in buf[: got.value // ctypes.sizeof(_Perf)]]
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            lines = [ln.split() for ln in fh if ln.startswith("cpu") and ln[3:4].isdigit()]
    except OSError:
        return None
    return [(int(f[4]) + int(f[5]), sum(int(x) for x in f[1:9])) for f in lines] or None


def read_core_busy_pct() -> Optional[list]:
    """Busy percent per logical core since the previous call (kept in-process while the engine is warm);
    a cold call takes a second reading ``_CORE_GAP_S`` later. None when the host has no reader."""
    global _core_prev
    cur, at = _core_times(), time.monotonic()
    if cur is None:
        return None
    prev = _core_prev
    if prev is None or not _CORE_REUSE_S[0] <= at - prev[0] <= _CORE_REUSE_S[1] or len(prev[1]) != len(cur):
        time.sleep(_CORE_GAP_S)
        prev, cur, at = (at, cur), _core_times(), time.monotonic()
        if cur is None or len(prev[1]) != len(cur):
            return None
    _core_prev = (at, cur)
    out = []
    for (i0, t0), (i1, t1) in zip(prev[1], cur):
        total = t1 - t0
        out.append(0.0 if total <= 0 else round(max(0.0, min(100.0, (1 - (i1 - i0) / total) * 100.0)), 1))
    return out


def class_use(core_classes: dict, busy: Optional[list]) -> tuple:
    """``(cores_by_class, free_cores_by_class)``; a core is free under ``FREE_CORE_BUSY_PCT`` busy.
    ``free`` is None when ``busy`` does not cover every declared core."""
    total = {name: len(idx) for name, idx in core_classes.items()}
    if not isinstance(busy, list) or any(i >= len(busy) for idx in core_classes.values() for i in idx):
        return total, None
    return total, {name: sum(1 for i in idx if busy[i] < FREE_CORE_BUSY_PCT) for name, idx in core_classes.items()}


def with_core_classes(raw: dict, classes: Optional[dict] = None, busy_reader: Any = None) -> dict:
    """``raw`` plus ``core_classes``, ``cores_by_class`` and ``free_cores_by_class`` when this box
    declares a split; the same dict untouched otherwise. Never raises."""
    try:
        declared = valid_core_classes(classes) if classes is not None else read_core_classes()
        if declared is None:
            return raw
        busy = (busy_reader or read_core_busy_pct)()
        total, free = class_use(declared, busy)
        return {**raw, "core_classes": declared, "cores_by_class": total, "free_cores_by_class": free}
    except Exception:  # noqa: BLE001 - the plain sample still stands
        return raw


def sample_local() -> dict:
    """This box's ``capacity_probe.sample()`` plus its per-class cores (``with_core_classes``)."""
    from coordinator_core.group_em import capacity_probe

    return with_core_classes(capacity_probe.sample())


DEFAULT_TIMEOUT_S = 8.0
CAPACITY_OP = "groupem.capacity_local"
_BUSY_REASONS = ("box_busy", "rate_limited", "box_lock_unavailable")
_TIMEOUT_GRACE = 1.1


def _state_for_failure(res: dict) -> tuple:
    """``(state, reason)`` for a non-ok ``invoke_remote`` reply."""
    reason = str(res.get("reason") or "unknown")
    if reason == "ssh_timeout":
        return STATE_TIMED_OUT, reason
    if reason in _BUSY_REASONS:
        return STATE_BUSY, reason
    stderr = str(res.get("stderr") or "").lower()
    if reason == "remote_error" or (
        reason.startswith("ssh_exit_") and ("method not found" in stderr or "method-not-found" in stderr)
    ):
        return STATE_OP_MISSING, reason
    return STATE_UNREACHABLE, reason


def _far_row(record: dict, timeout: float, invoke: Any) -> dict:
    """One far box's raw outcome: ``{"_row": raw}`` or ``{"_fail": state, "reason": str}``."""
    if record.get("roster_only"):
        return {"_fail": STATE_UNREACHABLE, "reason": "no_route"}
    res = invoke(record, CAPACITY_OP, {}, timeout=timeout)
    if not isinstance(res, dict) or not res.get("ok"):
        state, reason = _state_for_failure(res if isinstance(res, dict) else {})
        return {"_fail": state, "reason": reason}
    reply = res.get("reply")
    raw = reply.get("row") if isinstance(reply, dict) else None
    if not isinstance(raw, dict):
        err = reply.get("error") if isinstance(reply, dict) else None
        return {"_fail": STATE_ERROR, "reason": str(err or "bad_reply")}
    return {"_row": raw}


def _local_row(local_sample: Any) -> dict:
    raw = (local_sample or sample_local)()
    if not isinstance(raw, dict):
        return {"_fail": STATE_ERROR, "reason": "bad_sample"}
    return {"_row": raw}


def gather(boxes: Optional[list] = None, timeout: float = DEFAULT_TIMEOUT_S, invoke: Any = None,
           local_sample: Any = None, cache_path: Optional[Path] = None, local_id: Optional[str] = None,
           now: Optional[float] = None, snapshot_path: Optional[Path] = None) -> list:
    """One live row per registered box plus this box, fetched in parallel; never drops a box, never raises.

    ``boxes=None`` reads the registry plus the BOX SPECS roster through ``known_boxes`` (one record per
    physical box); a ``roster_only`` record has no route and is ``unreachable`` without an invoke.

    The local box samples in-process; each far box is asked ``groupem.capacity_local`` through
    ``box_transport.invoke_remote`` (``invoke`` overrides it) on its own thread, so the call returns
    within about one ``timeout``. A box that does not answer in time is ``timed_out``; a refusal by a
    concurrent call is ``busy``; a stamp without the op is ``op_missing``; any other failure is
    ``unreachable``. Non-ok rows carry the box's last-known maxima and ``last_seen`` from the cache plus
    a ``reason`` key; ok rows refresh the cache (first write); every call merges its rows into the live
    snapshot (second write, ``write_live_snapshot``; default beside ``cache_path`` when given).
    """
    from concurrent.futures import ThreadPoolExecutor, wait

    from coordinator_core.group_em import box_registry

    if invoke is None:
        from coordinator_core.group_em import box_transport

        invoke = box_transport.invoke_remote
    try:
        records = list(boxes) if boxes is not None else known_boxes()
        local = local_id if local_id is not None else box_registry.local_box_id()
    except Exception:  # noqa: BLE001 - degrade to the local box alone
        records, local = [], local_id or "local"
    uniq: dict = {}
    for r in records:
        if isinstance(r, dict) and isinstance(r.get("box_id"), str):
            uniq.setdefault(r["box_id"], r)
    records = list(uniq.values())
    if not any(r["box_id"] == local for r in records):
        records.append({"box_id": local})
    cache = read_last_known(cache_path)
    stamp = time.time() if now is None else now
    pool = ThreadPoolExecutor(max_workers=max(1, len(records)))
    futures = {}
    for rec in records:
        if rec["box_id"] == local:
            futures[rec["box_id"]] = pool.submit(_local_row, local_sample)
        else:
            futures[rec["box_id"]] = pool.submit(_far_row, rec, timeout, invoke)
    wait(list(futures.values()), timeout=timeout * _TIMEOUT_GRACE)
    pool.shutdown(wait=False, cancel_futures=True)
    rows, fresh = [], dict(cache)
    for rec in records:
        box_id, fut = rec["box_id"], futures[rec["box_id"]]
        if not fut.done():
            out: dict = {"_fail": STATE_TIMED_OUT, "reason": "timeout"}
        else:
            try:
                out = fut.result()
            except Exception as exc:  # noqa: BLE001 - one box never sinks the call
                out = {"_fail": STATE_ERROR, "reason": f"{type(exc).__name__}: {exc}"}
        raw = out.get("_row")
        if isinstance(raw, dict):
            row = normalize_row(box_id, {**raw, "state": STATE_OK})
            if row["sampled_at"] is None:
                row["sampled_at"] = stamp
            row["last_seen"] = row["sampled_at"]
            if not row["alias"]:
                row["alias"] = box_alias(rec)
            declared = valid_core_classes((rec.get("capability") or {}).get("core_classes")
                                          if isinstance(rec.get("capability"), dict) else None)
            if row["core_classes"] is None and declared is not None:
                row["core_classes"], row["cores_by_class"] = declared, {k: len(v) for k, v in declared.items()}
            fresh[box_id] = maxima_record(row)
        else:
            lk = cache.get(box_id)
            row = timed_out_row(box_id, lk, state=out["_fail"],
                                maxima_source="last_known" if lk else None)
            row["alias"] = row["alias"] or box_alias(rec)
            row["reason"] = out["reason"]
        row["box_id"] = box_id
        rows.append(row)
    if fresh != cache:
        try:
            write_last_known(fresh, cache_path)
        except OSError:
            pass  # best-effort: the rows are still returned; only last-known persistence is lost
    rows = sorted(rows, key=lambda r: (str(r["alias"]), r["box"]))
    try:
        write_live_snapshot(rows, stamp, snapshot_path if snapshot_path is not None else (
            Path(cache_path).parent / LIVE_SNAPSHOT_RELPATH.name if cache_path is not None else None))
    except OSError:
        pass  # best-effort: the live snapshot is a convenience for the idle-balance tick
    return rows


_DOWN = (STATE_TIMED_OUT, STATE_UNREACHABLE)
_LABEL = {STATE_TIMED_OUT: "UNREACHABLE (timed out)", STATE_UNREACHABLE: "UNREACHABLE",
          STATE_OP_MISSING: "OP-MISSING", STATE_BUSY: "BUSY", STATE_ERROR: "ERROR"}


def _gb(mb: Any) -> str:
    return "?" if not isinstance(mb, (int, float)) else f"{mb / 1024:.0f}"


def _age(then: Any, now: float) -> str:
    if then is None:
        return "never"
    if not isinstance(then, (int, float)):
        return str(then)
    s = max(0, int(now - then))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def _max_text(row: dict) -> str:
    gpus = [g for g in (row.get("gpus") or []) if isinstance(g, dict)]
    gpu = ", ".join(f"{g.get('name') or '?'} {_gb(g.get('vram_total_mib'))}G" for g in gpus) or "no gpu"
    cores = row.get("cores")
    split = row.get("cores_by_class")
    classes = f" ({'+'.join(f'{n}{k}' for k, n in split.items())})" if isinstance(split, dict) and split else ""
    return f"{cores if cores is not None else '?'}c{classes} {_gb(row.get('ram_total_mb'))}G ram {gpu}"


def _num(v: Any, suffix: str = "") -> str:
    if v is None:
        return "?"
    return f"{v:.0f}{suffix}" if isinstance(v, (int, float)) else str(v)


def _now_text(row: dict) -> str:
    vram = row.get("vram_used_mib")
    commit = f"{_gb(row.get('commit_used_mb'))}/{_gb(row.get('commit_limit_mb'))}G"
    jobs = row.get("jobs")
    free = row.get("free_cores_by_class")
    free_text = f" freecores {'/'.join(f'{n}{k}' for k, n in free.items())}" if isinstance(free, dict) and free else ""
    return (f"cpu {_num(row.get('cpu_pct'), '%')}{free_text} free {_gb(row.get('ram_free_mb'))}G commit {commit} "
            f"vram {'?' if vram is None else _gb(vram)}G gpu {_num(row.get('gpu_util_pct'), '%')} "
            f"sess {_num(row.get('sessions'))} jobs {len(jobs) if isinstance(jobs, list) else '?'}")


def render(snapshot: list, now: Optional[float] = None) -> str:
    """The compact table: ``FLEET CAPACITY`` then one line per row (never dropped); a non-ok box prints
    ``unknown (<reason>)`` for NOW and its last-seen age; a ``power it on?`` line follows per down box."""
    stamp = time.time() if now is None else now
    lines = ["FLEET CAPACITY"]
    for row in snapshot:
        head = f"{row.get('alias') or row.get('box')} {row.get('os') or '?'}"
        if row.get("state") == STATE_OK:
            tail = f"NOW {_now_text(row)} | {_age(row.get('sampled_at'), stamp)} ago"
        else:
            label = _LABEL.get(row.get("state"), str(row.get("state")).upper())
            tail = (f"{label} NOW unknown ({row.get('reason') or row.get('state')}) | "
                    f"last seen {_age(row.get('last_seen'), stamp)} ago")
        known = row.get("cores") is not None or row.get("ram_total_mb") is not None
        lines.append(f"{head} | MAX {_max_text(row) if known else 'unknown'} | {tail}")
    lines.extend(power_on_items(snapshot))
    return "\n".join(lines)


def power_on_items(snapshot: list) -> list:
    """One ``power it on?`` line per UNREACHABLE (unreachable or timed-out) box, for the G-EM to raise
    with the PM; busy and op-missing boxes are up and get none."""
    return [f"power it on? {r.get('alias') or r.get('box')} is UNREACHABLE (last seen "
            f"{r.get('last_seen') if r.get('last_seen') is not None else 'never'})"
            for r in snapshot if r.get("state") in _DOWN]

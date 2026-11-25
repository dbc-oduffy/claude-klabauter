"""Record contract for one box's row in the live fleet-capacity snapshot.

``FIELDS`` is the per-box key set every row carries, in render order. A row's ``state`` is one of
``STATES``. Maxima fields survive an unreachable box (carried from a last-known record, with
``maxima_source`` naming where they came from); current-use fields are ``None`` there, never stale.
The per-box last-known cache (``read_last_known`` / ``write_last_known``) lives at
``LAST_KNOWN_PATH`` (resolved lazily; ``last_known_path()``).
``gpus[]`` rows are the ``gpu_sample`` shape (``index, name, vram_total_mib, vram_used_mib,
util_pct``); ``disks[]`` rows are ``{mount, total_gb, free_gb, floor_gb}``.
"""

from __future__ import annotations

import json
import re
import socket
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
MAXIMA_FIELDS = ("cores", "cpu_model", "ram_total_mb", "commit_limit_mb", "gpus", "disks")
USE_FIELDS = (
    "cpu_pct",
    "load5",
    "ram_free_mb",
    "commit_used_mb",
    "vram_used_mib",
    "gpu_util_pct",
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
    if local_sample is None:
        from coordinator_core.group_em import capacity_probe

        local_sample = capacity_probe.sample
    raw = local_sample()
    if not isinstance(raw, dict):
        return {"_fail": STATE_ERROR, "reason": "bad_sample"}
    return {"_row": raw}


def gather(boxes: Optional[list] = None, timeout: float = DEFAULT_TIMEOUT_S, invoke: Any = None,
           local_sample: Any = None, cache_path: Optional[Path] = None, local_id: Optional[str] = None,
           now: Optional[float] = None) -> list:
    """One live row per registered box plus this box, fetched in parallel; never drops a box, never raises.

    ``boxes=None`` reads the registry plus the BOX SPECS roster through ``known_boxes`` (one record per
    physical box); a ``roster_only`` record has no route and is ``unreachable`` without an invoke.

    The local box samples in-process; each far box is asked ``groupem.capacity_local`` through
    ``box_transport.invoke_remote`` (``invoke`` overrides it) on its own thread, so the call returns
    within about one ``timeout``. A box that does not answer in time is ``timed_out``; a refusal by a
    concurrent call is ``busy``; a stamp without the op is ``op_missing``; any other failure is
    ``unreachable``. Non-ok rows carry the box's last-known maxima and ``last_seen`` from the cache plus
    a ``reason`` key; ok rows refresh the cache (the only write).
    """
    import time
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
    return sorted(rows, key=lambda r: (str(r["alias"]), r["box"]))


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
    return f"{cores if cores is not None else '?'}c {_gb(row.get('ram_total_mb'))}G ram {gpu}"


def _num(v: Any, suffix: str = "") -> str:
    if v is None:
        return "?"
    return f"{v:.0f}{suffix}" if isinstance(v, (int, float)) else str(v)


def _now_text(row: dict) -> str:
    vram = row.get("vram_used_mib")
    commit = f"{_gb(row.get('commit_used_mb'))}/{_gb(row.get('commit_limit_mb'))}G"
    jobs = row.get("jobs")
    return (f"cpu {_num(row.get('cpu_pct'), '%')} free {_gb(row.get('ram_free_mb'))}G commit {commit} "
            f"vram {'?' if vram is None else _gb(vram)}G gpu {_num(row.get('gpu_util_pct'), '%')} "
            f"sess {_num(row.get('sessions'))} jobs {len(jobs) if isinstance(jobs, list) else '?'}")


def render(snapshot: list, now: Optional[float] = None) -> str:
    """The compact table: ``FLEET CAPACITY`` then one line per row (never dropped); a non-ok box prints
    ``unknown (<reason>)`` for NOW and its last-seen age; a ``power it on?`` line follows per down box."""
    import time

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

"""Session landing hold: a per-session record that defers engine landings, never drops them.

State: ``<root>/.coordinator-local/landing-hold/<session_id>.json`` holding
``{"session_id", "set_at", "deferred": [{"kind", "params"}]}``, written by atomic replace.
``is_held`` is one stat and fails open (False) for a None or path-unsafe session id.
``clear_hold`` removes the record and returns the deferred landings for the caller to replay.
Leaf module: advisory only, it denies nothing; defer/pending/clear are one session's
read-modify-write and take no lock.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.session.machinery_paths import machinery_root, safe_session_id

KINDS = ("halted_record", "checkpoint_stamp", "coded_stamp")

_HOLD_DIR = "landing-hold"


def _path(root: str, sid: str) -> str:
    return os.path.join(machinery_root(str(root)), _HOLD_DIR, sid + ".json")


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: str, record: dict) -> None:
    atomic_write_bytes(path, json.dumps(record, indent=2).encode("utf-8"))


def is_held(root: str, sid: Optional[str]) -> bool:
    """True when ``sid`` has a hold record. One stat; False for a None or unsafe sid."""
    if not safe_session_id(sid):
        return False
    return os.path.isfile(_path(root, sid))


def set_hold(root: str, sid: str) -> bool:
    """Set the hold, keeping any landings already deferred. False for an unsafe sid."""
    if not safe_session_id(sid):
        return False
    path = _path(root, sid)
    prior = _read(path) or {}
    deferred = prior.get("deferred")
    record = {
        "session_id": sid,
        "set_at": prior.get("set_at") if isinstance(prior.get("set_at"), (int, float)) else time.time(),
        "deferred": deferred if isinstance(deferred, list) else [],
    }
    _write(path, record)
    return True


def defer(root: str, sid: str, kind: str, params: dict[str, Any]) -> bool:
    """Append one landing to the held session's deferred list. False when not held or kind unknown."""
    if kind not in KINDS or not is_held(root, sid):
        return False
    path = _path(root, sid)
    record = _read(path) or {"session_id": sid, "set_at": time.time(), "deferred": []}
    deferred = record.get("deferred")
    if not isinstance(deferred, list):
        deferred = []
    deferred.append({"kind": kind, "params": params})
    record["deferred"] = deferred
    _write(path, record)
    return True


def pending(root: str, sid: Optional[str]) -> list[dict]:
    """The deferred landings in arrival order; empty when not held or the record is unreadable."""
    if not safe_session_id(sid):
        return []
    record = _read(_path(root, sid))
    deferred = record.get("deferred") if record else None
    return [d for d in deferred if isinstance(d, dict)] if isinstance(deferred, list) else []


def clear_hold(root: str, sid: Optional[str]) -> list[dict]:
    """Remove the hold and return its deferred landings for replay; empty when none."""
    if not safe_session_id(sid):
        return []
    items = pending(root, sid)
    try:
        os.unlink(_path(root, sid))
    except FileNotFoundError:
        pass
    return items

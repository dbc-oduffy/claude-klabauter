"""Queued remote retry: ``launch`` records a pending handle; the ask-spool drain runs it.

The door never blocks on the ssh round trip and never spawns a process. ``launch`` writes a
pending handle plus a params side file under ``requests_dir`` and returns the key; the engine's
ask-spool drain calls ``drain_pending``, which claims each pending entry and runs
``fallback.try_remote`` (the only ssh path: ``node_transport.select`` -> ``box_transport``),
replacing the handle atomically with the outcome. Callers read it with ``read_handle``.

The handle file holds only status, op and the outcome, never request bodies; the params file is
consumed (claimed, read, unlinked) before ``try_remote`` runs. ``sweep`` drops every file under
``requests_dir`` older than the TTL.

Negative-spec:
    - Never raises from ``launch``, ``read_handle`` or ``drain_pending``; a launch failure returns None.
    - Never spawns a process; at most ``DRAIN_BATCH`` remote round trips per ``drain_pending`` call.
    - An entry is claimed by an atomic rename, so two drainers never run the same op.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Callable, Optional

from coordinator_core._settings_home import settings_home

Popen = Callable[..., Any]

HANDLE_TTL_S = 3600.0
DRAIN_BATCH = 2


def requests_dir():
    return settings_home() / "state" / "remote-engine" / "requests"


def _handle_path(key: str):
    return requests_dir() / f"{key}.json"


def _params_path(key: str):
    return requests_dir() / f"{key}.params"


def _write_atomic(key: str, payload: dict, *, path=None) -> None:
    path = path or _handle_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload))
    os.replace(tmp, path)


def sweep(ttl_s: float = HANDLE_TTL_S, *, now: Optional[float] = None) -> int:
    """Delete handle, params and tmp files older than `ttl_s`; returns the count. Never raises."""
    removed = 0
    try:
        cutoff = (time.time() if now is None else now) - ttl_s
        for entry in os.scandir(requests_dir()):
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def read_handle(key: str) -> Optional[dict]:
    """The handle file for `key` as a dict, or None when absent, unreadable or `key` is malformed."""
    try:
        if not isinstance(key, str) or not key or os.sep in key or "/" in key or ".." in key:
            return None
        data = json.loads(_handle_path(key).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def launch(op: str, params: dict, *, popen: Optional[Popen] = None) -> Optional[str]:
    """Queue the retry of `op` for the ask-spool drain; returns the handle key, or None.

    The pending handle is written before the key is returned, so `read_handle(key)` is never None
    for a returned key. `popen` is accepted for existing callers and ignored: nothing is spawned.
    """
    key = None
    try:
        sweep()
        key = uuid.uuid4().hex[:16]
        _write_atomic(key, params, path=_params_path(key))
        _write_atomic(key, {"status": "pending", "op": op})
        return key
    except Exception:  # noqa: BLE001 - contract: never raises
        if key is not None:
            for path in (_handle_path(key), _params_path(key)):
                try:
                    path.unlink()
                except OSError:
                    pass
        return None


def _outcome(op: str, served: Optional[dict]) -> dict:
    if served is None:
        return {"status": "no_reply", "op": op}
    if served.get("indeterminate"):
        box = served.get("served_by")
        return {
            "status": "indeterminate",
            "op": op,
            "served_by": box,
            "request_key": served.get("request_key"),
            "reason": served.get("reason"),
            "instruction": (
                f"The far run of {op} may have happened on box {box}; do not re-run it locally. "
                "Reconcile with warm.request_status on that box"
                + (f" using key {served['request_key']}." if served.get("request_key") else ".")
            ),
        }
    return {
        "status": "done",
        "op": op,
        "served_by": served.get("served_by"),
        "latency_ms": served.get("latency_ms"),
        "reply": served.get("reply"),
    }


def run_child(key: str, *, try_remote: Optional[Callable[..., Optional[dict]]] = None) -> int:
    """Run one queued entry: claim its params, run `try_remote` once, write the outcome. Never raises.

    Returns 0 on an outcome written, 2 when the run errored, 1 when the entry is not pending or
    another drainer claimed it first (nothing ran).
    """
    op = "?"
    try:
        pending = read_handle(key)
        if not pending or pending.get("status") != "pending":
            return 1
        op = pending["op"]
        claim = _params_path(key).with_suffix(".claimed")
        try:
            os.replace(_params_path(key), claim)
        except OSError:
            return 1
        params = None
        try:
            params = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            claim.unlink()
        except OSError:
            pass
        if try_remote is None:
            from coordinator_core.remote_engine import fallback

            try_remote = fallback.try_remote
        if not isinstance(params, dict):
            raise ValueError("request params file missing or unreadable; refusing to run the op with empty params")
        served = try_remote(op, params)
        _write_atomic(key, _outcome(op, served))
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            _write_atomic(key, {"status": "error", "op": op, "reason": str(exc)})
        except Exception:  # noqa: BLE001
            pass
        return 2


def drain_pending(
    limit: int = DRAIN_BATCH, *, try_remote: Optional[Callable[..., Optional[dict]]] = None
) -> int:
    """Run up to `limit` queued entries (oldest first) and sweep expired files; returns the count run."""
    ran = 0
    try:
        sweep()
        entries = sorted(
            (e for e in os.scandir(requests_dir()) if e.name.endswith(".json") and e.is_file()),
            key=lambda e: e.stat().st_mtime,
        )
        for entry in entries:
            if ran >= limit:
                break
            key = entry.name[: -len(".json")]
            if run_child(key, try_remote=try_remote) != 1:
                ran += 1
    except Exception:  # noqa: BLE001 - contract: never raises
        pass
    return ran

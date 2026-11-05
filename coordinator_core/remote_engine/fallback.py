"""One-shot remote retry of a coordinator-invoke op on a registered box.

The door calls ``try_remote`` after the local engine missed its reach budget or health probe.
A non-reply (refused op, hook context, no box, transport failure) returns None so the caller
keeps its existing local loud-pass behaviour.

Negative-spec:
    - Never raises; never retries more than once; never routes a non-remote-safe op.
    - Never runs in hook context (``COORDINATOR_HOOK_CONTEXT=1``, or the legacy
      ``COORDINATOR_WARM_BOOT_WAIT_SECS`` <= 0): hooks fail open locally.
    - Never returns None for a phase-2 mutating op once the send was attempted: the caller gets an
      ``indeterminate`` envelope and must not re-run the op locally.
    - Never forwards params carrying a caller-local path (refused before a box is picked).
    - A far box is reached only through ``node_transport.select`` (ssh kind) -> ``box_transport``.
    - The reply is returned to the caller only; nothing is written to caller run-state.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import box_registry, box_transport, node_transport
from coordinator_core.remote_engine import policy

HOOK_ENV = "COORDINATOR_WARM_BOOT_WAIT_SECS"
HOOK_MARKER_ENV = "COORDINATOR_HOOK_CONTEXT"
DEFAULT_REMOTE_TIMEOUT_S = 8.0


def in_hook_context() -> bool:
    """True when the explicit hook marker is ``1`` or the warm-boot wait env is non-positive."""
    if os.environ.get(HOOK_MARKER_ENV) == "1":
        return True
    raw = os.environ.get(HOOK_ENV)
    if raw is None or not raw.strip():
        return False
    try:
        return float(raw) <= 0
    except ValueError:
        return False


def pick_box(boxes: list[dict], local_id: str) -> Optional[dict]:
    """First registered box other than ``local_id`` with an ssh transport."""
    for box in boxes:
        if box.get("box_id") == local_id:
            continue
        if box_transport.ssh_host(box) is not None:
            return box
    return None


def telemetry_path() -> Path:
    return settings_home() / "state" / "remote-engine" / "telemetry.jsonl"


def _record(entry: dict) -> None:
    try:
        path = telemetry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 - telemetry is best-effort
        pass


def _far_request_key(res: Any) -> Optional[str]:
    """The dispatch key a far -32004 carried (``error.data.dispatch_key``), else None; never invented."""
    if not isinstance(res, dict):
        return None
    err = res.get("error")
    data = err.get("data") if isinstance(err, dict) else None
    key = data.get("dispatch_key") if isinstance(data, dict) else None
    key = key if key is not None else res.get("request_key")
    return key if isinstance(key, str) and key else None


def _indeterminate(op: str, served_by: Optional[str], reason: Any, request_key: Optional[str]) -> dict[str, Any]:
    """Envelope for a phase-2 op whose far outcome is unknown: reconcile on the far box, never rerun locally."""
    where = served_by or "the far box"
    how = (
        f"reconcile with warm.request_status key {request_key} on {where}"
        if request_key
        else f"no dispatch key; reconcile by inspecting {where} for op {op}"
    )
    return {
        "indeterminate": True,
        "served_by": served_by,
        "reason": f"{reason}: {how}; do not re-run locally",
        "request_key": request_key,
    }


def try_remote(
    op: str,
    params: dict,
    *,
    timeout: float = DEFAULT_REMOTE_TIMEOUT_S,
    runner: Optional[box_transport.Runner] = None,
    boxes: Optional[list[dict]] = None,
    local_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Retry ``op`` once on one remote box; returns ``{reply, served_by, latency_ms, remote}``, an
    ``{indeterminate, served_by, reason, request_key}`` envelope, or None.

    None means nothing was sent (op not remote-safe, hook context, local-path params, no ssh box) or
    a non-mutating op got no reply; the caller may run locally. The indeterminate envelope is returned
    only for a phase-2 mutating op whose far outcome is unknown: the caller must not re-run it.
    Latency of every attempted round trip is appended to the telemetry jsonl.
    """
    sent = False
    served_by: Optional[str] = None
    try:
        if in_hook_context() or not policy.remote_safe(op):
            return None
        if policy.carries_local_path(params):
            return None
        lid = local_id if local_id is not None else box_registry.local_box_id()
        candidates = boxes if boxes is not None else box_registry.read_boxes()
        box = pick_box(candidates, lid)
        if box is None:
            return None
        if node_transport.select(box, runner=runner).kind != "ssh":
            return None
        served_by = box.get("box_id")
        t0 = time.monotonic()
        sent = True
        res = box_transport.invoke_remote(box, op, params, runner, timeout=timeout)
        latency_ms = (time.monotonic() - t0) * 1000.0
        ok = bool(res.get("ok")) and isinstance(res.get("reply"), dict)
        _record(
            {
                "ts": time.time(),
                "op": op,
                "served_by": served_by,
                "latency_ms": latency_ms,
                "ok": ok,
                "reason": None if ok else res.get("reason"),
            }
        )
        if not ok:
            if policy.far_result_indeterminate(op, res):
                return _indeterminate(op, served_by, res.get("reason"), _far_request_key(res))
            return None
        return {"reply": res["reply"], **policy.reply_envelope(served_by, latency_ms)}
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        if sent and policy.is_phase2_mutating(op):
            return _indeterminate(op, served_by, f"exception:{type(exc).__name__}", None)
        return None

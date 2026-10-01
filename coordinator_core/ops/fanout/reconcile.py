"""
coordinator_core.ops.fanout.reconcile — census rows to next actions.

Purpose: turn a `fanout.census` result into the ordered actions the verb performs next
(spawn, archive, flag, await_checkin, broadcast). It names an idempotency key for a spawn and
never re-emits create_session args; compose owns those.

Negative spec: pure function — no file, process or network access, no op registration.
"""

from __future__ import annotations

from typing import Any, Optional

from coordinator_core.ops.fanout import transport
from coordinator_core.ops.fanout.contract import (
    DEFAULT_MAX_CONCURRENT,
    ReconcileAction,
    ReconcileResult,
    idempotency_key,
)

# States whose sessions are not known to be finished, so each holds a concurrency slot.
_SLOT_HOLDING_STATES = frozenset({"running", "duplicate", "unknown"})


def _slots_held(rows: list[dict]) -> int:
    held = 0
    for row in rows:
        if row.get("state") in _SLOT_HOLDING_STATES:
            held += max(len(row.get("session_ids") or ()), 1)
    return held


def reconcile(manifest: dict, census: dict, pause: Optional[dict[str, Any]] = None) -> ReconcileResult:
    """Next actions for a job, in census row order, then strays.

    `pause` is `{message}`; when given, every checked-in row with a broadcast target yields one
    `broadcast` action (the verb reads the target and message from the census row and `pause`).
    """
    job_id = census.get("job_id") or manifest["job_id"]
    rows = list(census.get("rows") or ())
    transport_ = transport.for_channel(manifest["channel"])
    cap = manifest.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
    free_slots = max(cap - _slots_held(rows), 0)

    actions: list[ReconcileAction] = []
    for row in rows:
        worker_id = row["worker_id"]
        state = row.get("state")
        session_ids = row.get("session_ids") or []
        if state == "missing":
            if free_slots > 0:
                free_slots -= 1
                actions.append(
                    {
                        "kind": "spawn",
                        "worker_id": worker_id,
                        "idempotency_key": idempotency_key(job_id, worker_id),
                        "reason": "missing",
                    }
                )
            else:
                actions.append({"kind": "flag", "worker_id": worker_id, "reason": "concurrency-cap"})
        elif state == "done":
            action: ReconcileAction = {"kind": "archive", "worker_id": worker_id, "reason": "done"}
            if session_ids:
                action["session_id"] = session_ids[0]
            actions.append(action)
        elif state in ("failed", "duplicate"):
            actions.append({"kind": "flag", "worker_id": worker_id, "reason": state})
        elif state == "running":
            if not row.get("checked_in"):
                action = {"kind": "await_checkin", "worker_id": worker_id, "reason": "running-not-checked-in"}
                if session_ids:
                    action["session_id"] = session_ids[0]
                actions.append(action)
        else:
            actions.append({"kind": "flag", "worker_id": worker_id, "reason": "unknown"})

        if pause is not None and transport_.broadcast_target(row) is not None:
            action = {"kind": "broadcast", "worker_id": worker_id, "reason": "pause"}
            if session_ids:
                action["session_id"] = session_ids[0]
            actions.append(action)

    for stray in census.get("strays") or ():
        actions.append({"kind": "flag", "session_id": stray, "reason": "stray"})

    return {"actions": actions}

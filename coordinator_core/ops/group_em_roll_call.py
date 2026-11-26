"""
coordinator_core.ops.group_em_roll_call -- JSON-RPC "groupem.roll_call",
"groupem.roll_call_reply" and "groupem.roll_call_status".

Purpose: thin veneers over `coordinator_core.group_em.roll_call` (addressee resolution, the
templated ask, the per-id record under the group-em home) and `roll_call_deliver.launch` (one
detached far-delivery worker). Mirrors `group_em_idle_report`'s registration convention.

Self-registration: importing this module registers the three ops as a side-effect. Registered in
`coordinator_core/ops/__init__.py`, `_registry_map.py`'s `OP_MODULE_MAP`, `op_scopes.py` (scope
"none" -- same resolution story as `groupem.idle_report`) and `authz/classification.py`.

Negative-spec:
    - Never delivers to a far addressee inside the op: the worker does, detached and not awaited.
    - Never SendMessages: the engine cannot, so local addressees are returned to the caller.
    - `dry_run` writes the record and launches nothing.
    - `groupem.roll_call_status` only reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.group_em import roll_call
from coordinator_core.ipc import CallerFacingValidationError, register_op


def _str(op: str, p: dict, key: str, *, required: bool = False) -> Optional[str]:
    value = p.get(key)
    if value is None:
        if required:
            raise CallerFacingValidationError(f"{op}: {key} (string) is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise CallerFacingValidationError(f"{op}: {key} must be a non-empty string")
    return value


def _required(op: str, p: dict, key: str) -> str:
    value = _str(op, p, key, required=True)
    assert value is not None
    return value


def _checked_id(op: str, roll_call_id: str) -> str:
    try:
        roll_call.record_path(roll_call_id)
    except ValueError as exc:
        raise CallerFacingValidationError(f"{op}: {exc}") from exc
    return roll_call_id


@register_op("groupem.roll_call")
def _groupem_roll_call(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.roll_call" handler.

    Params (all optional):
        caller_session_id (str) -- excluded from the addressees.
        roll_call_id (str) -- defaults to a fresh `rc-<stamp>-<hex>`; an id already on disk
            returns its record's addressees and launches nothing.
        note (str) -- appended to the templated ask.
        deadline_minutes (number > 0) -- reply deadline; default 60.
        dry_run (bool) -- write the record, launch no worker, deliver nothing.
        repo_root (str) -- repo whose peer roster is read; defaults to the process cwd.

    Returns: {roll_call_id, text, local: [{session_id, name}], far: [{key, box_id, name}],
    excluded: [{key, reason}], record_path, deadline, dry_run, delivery} where `delivery` is the
    worker launch result (`{launched, pid | reason}`). The caller SendMessages `local` by name.
    """
    p = params if isinstance(params, dict) else {}
    op = "groupem.roll_call"
    minutes = p.get("deadline_minutes")
    if minutes is not None and (
        isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0
    ):
        raise CallerFacingValidationError(f"{op}: deadline_minutes must be a positive number")
    dry_run = bool(p.get("dry_run"))
    caller = _str(op, p, "caller_session_id")
    note = _str(op, p, "note")
    supplied = _str(op, p, "roll_call_id")
    rc_id = _checked_id(op, supplied) if supplied else roll_call.new_roll_call_id()

    existing = roll_call.load_record(rc_id)
    if existing is not None:
        record, launched = existing, {"launched": False, "reason": "existing_roll_call"}
    else:
        override = p.get("repo_root")
        target_root = override if isinstance(override, str) and override else str(Path.cwd())
        addressees = roll_call.resolve_addressees(target_root, caller)
        record, created = roll_call.open_roll_call_once(
            rc_id, addressees, roll_call.render_ask(rc_id, note), deadline_minutes=minutes
        )
        if not created:
            launched = {"launched": False, "reason": "existing_roll_call"}
        elif dry_run:
            launched = {"launched": False, "reason": "dry_run"}
        elif not addressees["far"]:
            launched = {"launched": False, "reason": "no_far_addressees"}
        else:
            from coordinator_core.group_em import roll_call_deliver

            launched = roll_call_deliver.launch(roll_call.record_path(rc_id))
    addressed = record.get("addressees", {})
    return {
        "roll_call_id": rc_id,
        "text": record.get("text"),
        "local": addressed.get("local", []),
        "far": addressed.get("far", []),
        "excluded": addressed.get("excluded", []),
        "record_path": str(roll_call.record_path(rc_id)),
        "deadline": record.get("deadline"),
        "dry_run": dry_run,
        "delivery": launched,
    }


@register_op("groupem.roll_call_reply")
def _groupem_roll_call_reply(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.roll_call_reply" handler.

    Params: roll_call_id (str, required), session_key (str, required), goals (list, required):
    one `{goal_id, target, measured_now, gap, next_lever, expected_gain, owner, eta (ISO date),
    blocked_on?: {what, owner, date}}` per goal; priority_goal_id (str, required; one of the
    reply's goal_ids, else refused); activity (any JSON, optional; log only).

    Returns the `upsert_reply` dict: `{ok, result: stored | unchanged, late}` or
    `{ok: False, reason: "unknown_roll_call"}`. A reply failing the per-goal check (missing field,
    TBD, no ISO ETA, blocked_on without owner and date) stores nothing and returns the typed
    refusal `{ok: False, error: "RollCallReplyRefused", roll_call_id, session_key, refusals:
    [{goal_id, field, reason}]}` to the replying EM. A repeat of the same payload is a no-op; a
    reply past the record's deadline is stored `late: true`.
    """
    p = params if isinstance(params, dict) else {}
    op = "groupem.roll_call_reply"
    rc_id = _checked_id(op, _required(op, p, "roll_call_id"))
    session_key = _required(op, p, "session_key")
    try:
        return roll_call.upsert_reply(
            rc_id, session_key, p.get("goals"), p.get("activity"), p.get("priority_goal_id")
        )
    except roll_call.RollCallReplyRefused as refused:
        return {"ok": False, "error": "RollCallReplyRefused", **refused.as_dict()}


@register_op("groupem.roll_call_status")
def _groupem_roll_call_status(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.roll_call_status" handler.

    Params: roll_call_id (str, required).

    Returns the progress-to-goals brief `{ok, roll_call_id, deadline, goals, replied, missing,
    deliveries}` (one `goals` row per goal; never per-EM activity) or
    `{ok: False, reason: "unknown_roll_call"}`. Reads one file; writes nothing.
    """
    p = params if isinstance(params, dict) else {}
    op = "groupem.roll_call_status"
    rc_id = _checked_id(op, _required(op, p, "roll_call_id"))
    return roll_call.status(rc_id)

"""
coordinator_core.ops.group_em_roll_call -- JSON-RPC "groupem.roll_call",
"groupem.roll_call_reply" and "groupem.roll_call_status".

Purpose: thin veneers over `coordinator_core.group_em.roll_call` (addressee resolution, the
templated ask, the per-id record under the group-em home) and `roll_call_deliver.launch` (one
detached local and far delivery worker). Mirrors `group_em_idle_report`'s registration convention.

Self-registration: importing this module registers the three ops as a side-effect. Registered in
`coordinator_core/ops/__init__.py`, `_registry_map.py`'s `OP_MODULE_MAP`, `op_scopes.py` (scope
"none" -- same resolution story as `groupem.idle_report`) and `authz/classification.py`.

Negative-spec:
    - Never delivers inside the op: the worker does, to local and far addressees, detached and not awaited.
    - Never SendMessages: local delivery is a mailbox append or a bridge route in the worker.
    - `dry_run` writes the record and launches nothing.
    - `groupem.roll_call_status` only reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.group_em import roll_call
from coordinator_core.ipc import CallerFacingValidationError, register_op


FORWARD_TIMEOUT_S = 15.0


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
    worker launch result (`{launched, pid | reason}`); the worker delivers to `local` and `far` and
    records one `deliveries` row per addressee key.
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
        from coordinator_core.group_em import box_registry

        addressees = roll_call.resolve_addressees(target_root, caller)
        record, created = roll_call.open_roll_call_once(
            rc_id,
            addressees,
            roll_call.render_ask(rc_id, note, origin_box=box_registry.local_box_id()),
            deadline_minutes=minutes,
        )
        if not created:
            launched = {"launched": False, "reason": "existing_roll_call"}
        elif dry_run:
            launched = {"launched": False, "reason": "dry_run"}
        elif not (addressees["local"] or addressees["far"]):
            launched = {"launched": False, "reason": "no_addressees"}
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


def _forward_reply(origin: str, p: dict) -> dict:
    """Run `groupem.roll_call_reply` on the issuing box `origin` and return its reply; never raises."""
    from coordinator_core.group_em import box_registry

    box = next(
        (b for b in box_registry.read_boxes() if box_registry.same_box(b.get("box_id"), origin)), None
    )
    if box is None:
        return {"ok": False, "reason": "unknown_origin_box"}
    return _send_reply(box, p)


def _probe_minting_box(p: dict) -> Optional[dict]:
    """Forward to each other registered box; the first reply that is not a miss, else None."""
    from coordinator_core.group_em import box_registry

    from concurrent.futures import ThreadPoolExecutor

    local = box_registry.local_box_id()
    others = [b for b in box_registry.read_boxes() if not box_registry.same_box(b.get("box_id"), local)]
    if not others:
        return None
    pool = ThreadPoolExecutor(max_workers=len(others))
    try:
        for fut in [pool.submit(_send_reply, box, p) for box in others]:
            res = fut.result()
            if res.get("ok") or res.get("error") or res.get("reason") == "not_an_addressee":
                return res
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _send_reply(box: dict, p: dict) -> dict:
    from coordinator_core.group_em import box_registry, box_transport

    params = {k: v for k, v in p.items() if k not in ("origin_box", "forwarded_by")}
    params["forwarded_by"] = box_registry.local_box_id()
    res = box_transport.invoke_remote(
        box, "groupem.roll_call_reply", params, timeout=FORWARD_TIMEOUT_S
    )
    if res.get("ok"):
        return res["reply"]
    reason = res.get("reason")
    if reason == "not_ssh_transport":
        return {"ok": False, "reason": "forward_unsupported_transport"}
    return {"ok": False, "reason": f"forward_failed:{reason}"}


@register_op("groupem.roll_call_reply")
def _groupem_roll_call_reply(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.roll_call_reply" handler.

    Params: roll_call_id (str, required), session_key (str, optional; the caller's own session
    when absent), goals (list, optional): one `{goal_id, target, measured_now, gap, next_lever,
    expected_gain, owner, eta (ISO date), blocked_on?: {what, owner, date}}` per goal;
    priority_goal_id (str, required with goals; one of the reply's goal_ids, else refused); jobs
    (list: id, pid, cores, hard stop), queued (list) and eta (str): the minimal reply, at least one
    of goals, jobs, queued, eta; activity (any JSON, optional; log only); origin_box (str,
    optional): the issuing box named in the ask; without it and without a local record, each other
    registered box is tried in turn and the first non-miss reply is returned. When it is a
    registered box other than this one and no local record exists, the call runs there through
    `box_transport.invoke_remote` and its reply is returned (`unknown_origin_box` when
    unregistered); a forward with no path (`forward_unsupported_transport`,
    `forward_failed:<reason>`) stores the reply on this box instead, `origin_box` kept on it, and
    returns the stored dict plus `stored_locally: true` and `forward_reason`. `forwarded_by` (str)
    marks the issuing-side leg of a forward: its `session_key` must be a far addressee of the
    record, else `not_an_addressee`.

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
    origin = _str(op, p, "origin_box")
    forwarded_by = _str(op, p, "forwarded_by")
    record = roll_call.load_record(rc_id)
    session_key = _str(op, p, "session_key")
    if session_key is None:
        session_key = _own_session_key(op, record is None)
        p = {**p, "session_key": session_key}
    if origin is not None and forwarded_by is None and record is None:
        from coordinator_core.group_em import box_registry

        if not box_registry.same_box(origin, box_registry.local_box_id()):
            forwarded = _forward_reply(origin, p)
            reason = forwarded.get("reason")
            if forwarded.get("ok") is not False or not (
                isinstance(reason, str) and reason.startswith("forward_")
            ):
                return forwarded
            return _store_reply(rc_id, session_key, p, forwarded_by, create=True, origin_box=origin) | {
                "stored_locally": True,
                "forward_reason": reason,
            }
    if forwarded_by is not None:
        if record is not None:
            far_keys = {row.get("key") for row in record.get("addressees", {}).get("far", [])}
            if session_key not in far_keys:
                return {"ok": False, "reason": "not_an_addressee"}
    elif record is None and origin is None:
        found = _probe_minting_box(p)
        if found is not None:
            return found
    return _store_reply(rc_id, session_key, p, forwarded_by)


def _own_session_key(op: str, forwards: bool) -> str:
    """The caller's own session id; box-qualified when the reply may travel to another box."""
    from coordinator_core.group_em import box_registry
    from coordinator_core.ops.session_context import resolve_current_session_id

    sid = resolve_current_session_id()
    if not sid:
        raise CallerFacingValidationError(
            f"{op}: session_key (string) is required; no current session id resolved"
        )
    return f"{box_registry.local_box_id()}:{sid}" if forwards else sid


def _store_reply(
    rc_id: str,
    session_key: str,
    p: dict,
    forwarded_by: Optional[str],
    create: bool = False,
    origin_box: Optional[str] = None,
) -> dict:
    from coordinator_core.group_em import box_registry

    try:
        return roll_call.upsert_reply(
            rc_id,
            session_key,
            p.get("goals"),
            p.get("activity"),
            p.get("priority_goal_id"),
            origin_box=origin_box or box_registry.local_box_id(),
            forwarded_by=forwarded_by,
            jobs=p.get("jobs"),
            queued=p.get("queued"),
            eta=p.get("eta"),
            create_if_missing=create,
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

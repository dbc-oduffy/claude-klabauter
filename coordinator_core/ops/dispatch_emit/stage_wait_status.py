"""
coordinator_core.ops.dispatch_emit.stage_wait_status -- ``dispatch.stage_wait_status``, the read-only
view of one parked run's typed blockers and how its wait ended or stands.

Params: ``{run_id}``. Reply: ``{exit_code, applied: false, run_id, status: waiting|resumed|attention|
expired, blockers: [{kind, ref, state}], resumed_by, session, action, sha, waited_s, flag, resume_action,
on_blocker_dead}``. ``resumed_by`` is ``engine`` for a wake or sweep resume (``session`` null, no session
involved) and ``pm`` for a resume after the PM answered an attention flag; ``sha`` is the landed-marker sha
of a ``close_landed`` resume. ``attention`` is a park held for the PM: a dead blocker under
``on_blocker_dead: surface``, never proceeded past. ``expired`` is ``stage_wait.expired`` for an unstarted
wait that is not held for the PM.

Negative-spec:
  - Writes no file: no wake, no sweep, no flag, no spool entry.
  - Unknown or unsafe run id, or a run with no wait record, is a refusal, never a guess.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import stage_wait

STAGE_WAIT_STATUS_OP = "dispatch.stage_wait_status"

_STATES = ("live", "cleared", "dead", "pending")


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


def _verdict(root: Path, blocker: dict) -> tuple[str, Any]:
    """`blocker_check.evaluate`'s `(state, payload)`; an absent evaluator or a failing check reads `pending`."""
    try:
        from coordinator_core.ops.dispatch_emit import blocker_check

        verdict = blocker_check.evaluate(root, blocker)
    except Exception:  # noqa: BLE001
        return "pending", None
    if isinstance(verdict, tuple) and len(verdict) == 2:
        state, payload = verdict
    elif isinstance(verdict, dict):
        state, payload = verdict.get("state"), verdict.get("payload")
    else:
        state, payload = getattr(verdict, "state", None), getattr(verdict, "payload", None)
    return (state if state in _STATES else "pending"), payload


def _payload_sha(payload: Any) -> Optional[str]:
    sha = payload.get("sha") if isinstance(payload, dict) else payload
    return sha if isinstance(sha, str) and len(sha) == 40 else None


def _flag_of(root: Path, record: dict) -> Optional[str]:
    flag = record.get("flag")
    if isinstance(flag, str) and flag:
        return flag
    try:
        from coordinator_core.ops.dispatch_emit import pending_oracle

        directory = pending_oracle.flag_path(root, record["run_id"], "x").parent
        found = sorted(directory.glob(f"{record['run_id']}.*.json"))
    except Exception:  # noqa: BLE001
        return None
    return str(found[0]) if found else None


def stage_wait_status(params: dict, root: Path, now: Optional[float] = None) -> dict:
    """Return the parked run's status, its blockers' states and the resume the engine recorded."""
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id or not safe_id(run_id):
        return _err(f"dispatch.stage_wait_status: run_id {run_id!r} is not a single safe path segment")
    base = Path(root)
    record = stage_wait.read(base, run_id)
    if record is None:
        return _err(f"dispatch.stage_wait_status: no stage wait record for run {run_id}")
    moment = time.time() if now is None else now
    started = bool(record.get("started"))
    blockers: list[dict[str, Any]] = []
    sha = record.get("sha") if isinstance(record.get("sha"), str) else None
    dead = False
    for blocker in record["typed_blockers"]:
        state, payload = _verdict(base, blocker)
        blockers.append({"kind": blocker["kind"], "ref": blocker["ref"], "state": state})
        dead = dead or state == "dead"
        if sha is None and not started and state == "cleared" and blocker["kind"] == "run_landed":
            sha = _payload_sha(payload)
    held = record.get("on_blocker_dead", stage_wait.ON_BLOCKER_DEAD[0]) == stage_wait.ON_BLOCKER_DEAD[0]
    if started:
        status = "resumed"
    elif record.get("status") == "attention" or (dead and held):
        status = "attention"
    elif stage_wait.expired(base, run_id, now=moment):
        status = "expired"
    else:
        status = "waiting"
    parked_at = float(record.get("parked_at") or moment)
    ended_at = record.get("resumed_at") or record.get("started_at")
    end = float(ended_at) if started and isinstance(ended_at, (int, float)) else moment
    resumed_by = (record.get("resumed_by") or "engine") if started else None
    return {
        "exit_code": 0,
        "applied": False,
        "run_id": run_id,
        "status": status,
        "blockers": blockers,
        "resumed_by": resumed_by,
        "session": None,
        "action": (record.get("action") or record.get("resume_action")) if started else None,
        "sha": sha if started or status == "attention" else None,
        "waited_s": round(max(0.0, end - parked_at), 1),
        "flag": _flag_of(base, record) if status == "attention" or record.get("flag") else None,
        "resume_action": record.get("resume_action"),
        "on_blocker_dead": record.get("on_blocker_dead"),
    }


@register_op(STAGE_WAIT_STATUS_OP)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.stage_wait_status" handler: `stage_wait_status` over the repo root."""
    if repo_root is None:
        return _err("dispatch.stage_wait_status: repo_root is required")
    base = Path(repo_root)
    # trap: the op's `common_dir` scope hands the handler `<repo>/.git`; blocker_check reads run markers under the worktree root.
    return stage_wait_status(params, base.parent if base.name == ".git" else base)

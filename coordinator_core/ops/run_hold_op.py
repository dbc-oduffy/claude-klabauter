"""coordinator_core.ops.run_hold_op -- op "run.hold": the holding session sets or clears a run's hold.

Writes or removes `held.json` in the run's record dir (`ops/dispatch_emit/run_hold.py`). A clear by
a session other than the holder is answered with the held reply unless `continue_held` names the run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op

OP_KEY = "run.hold"

_SCHEMA = {
    "type": "object",
    "properties": {
        "run_id": {"type": "string", "minLength": 1, "description": "The warp run to hold or release."},
        "pm_turn_ref": {"type": "string", "description": "The PM turn that holds the run; required to set."},
        "release_condition": {"type": "string", "description": "What releases the hold; required to set."},
        "clear": {"type": "boolean", "description": "Remove the hold instead of setting it."},
        "continue_held": {"type": "string",
                          "description": "The run id, to clear a hold another session owns."},
        "repo_root": {"type": "string", "description": "Repo holding the run; default the caller's worktree."},
        "session_id": {"type": "string", "minLength": 1, "description": "The calling session; the holder when setting."},
    },
    "required": ["run_id", "session_id"],
    "additionalProperties": False,
}


def _text(params: dict, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CallerFacingValidationError(f"run.hold: {key} must be a non-empty string")
    return value.strip()


@register_op(OP_KEY, params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "run.hold": params `run_id`, `session_id`, `pm_turn_ref`/`release_condition` (set),
    `clear`, `continue_held`, `repo_root` (optional).

    Returns {"run_id", "state", ...}; state is "held", "cleared", "absent" (clear with no hold),
    or "held_by_other" with the held reply in `reply` when a non-holder cleared without the continue.
    """
    from coordinator_core.ops.dispatch_emit import run_hold
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    run_id = _text(params, "run_id")
    session = _text(params, "session_id")
    root = params.get("repo_root") or repo_root
    if not root:
        raise CallerFacingValidationError("run.hold: no repo root")
    try:
        rdir = record_dir(Path(root), run_id)
    except ValueError as exc:
        raise CallerFacingValidationError(f"run.hold: {exc}") from exc
    existing = run_hold.read_hold(rdir)

    if params.get("clear"):
        if existing is None:
            return {"run_id": run_id, "state": "absent"}
        if existing.holder_session != session and params.get(run_hold.CONTINUE_KEY) != run_id:
            return {"run_id": run_id, "state": "held_by_other", "reply": run_hold.held_reply(run_id, existing)}
        run_hold.clear_hold(rdir)
        return {"run_id": run_id, "state": "cleared"}

    if existing is not None and existing.holder_session != session and params.get(run_hold.CONTINUE_KEY) != run_id:
        return {"run_id": run_id, "state": "held_by_other", "reply": run_hold.held_reply(run_id, existing)}
    hold = run_hold.HeldRun(
        holder_session=session,
        pm_turn_ref=_text(params, "pm_turn_ref"),
        release_condition=_text(params, "release_condition"),
        held_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    rdir.mkdir(parents=True, exist_ok=True)
    run_hold.write_hold(rdir, hold)
    return {"run_id": run_id, "state": "held", "holder_session": session}

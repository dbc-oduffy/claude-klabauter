"""Retract a `staged_stall` halt proven false: the run's driver is live.

`staged_stall.halt_stalled` halts a staged run idle past the bound with no agent progress in any session
recorded on it. A halt written before that detector read every driver session (or by a driver whose
journal was briefly quiet) leaves a live run stamped halted; this op rewrites that marker `resumed` with
`retracted_*` fields and keeps the run's live-run index row. One marker read, one run-stage read, the
stall scan's bounded journal stats; no git, no spawn.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import halted_run, staged_stall
from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage, record_dir

OP_NAME = "dispatch.retract_staged_stall"


def retract_staged_stall(root: Path, run_id: str, *, dry_run: bool = False, now: Optional[float] = None) -> dict:
    """Retract `run_id`'s `staged_stall` halt when a recorded driver session or a fire is in flight; the handler's body.

    Reply `{ok: true, run_id, driver_session, retracted}` (`dry_run: true` and `retracted: false` when
    dry), else `{ok: false, run_id, reason}` with reason `no-run`, `not-halted` (no `halted` marker),
    `not-staged-stall` (another halt kind) or `driver-not-live`. Writes `halted.json` and `run-stage.json`
    only, never unlinks the marker."""
    try:
        rdir = record_dir(root, run_id)
    except ValueError:
        return {"ok": False, "run_id": run_id, "reason": "no-run"}
    marker = halted_run._read(rdir)
    if marker is None or marker.get("status") != halted_run.STATUS_HALTED:
        return {"ok": False, "run_id": run_id, "reason": "not-halted"}
    if marker.get("halted") != staged_stall.HALT_STAGED_STALL:
        return {"ok": False, "run_id": run_id, "reason": "not-staged-stall"}
    cutoff = (time.time() if now is None else now) - staged_stall.STAGED_STALL_BOUND_SECS
    driver: Optional[str] = next(
        (sid for sid in staged_stall._recorded_sessions(read_run_stage(root, run_id) or {})
         if staged_stall.agent_in_flight(sid, cutoff)),
        None,
    )
    if driver is None and not staged_stall.fire_in_flight(root, run_id, cutoff):
        return {"ok": False, "run_id": run_id, "reason": "driver-not-live"}
    reply = {"ok": True, "run_id": run_id, "driver_session": driver}
    if dry_run:
        return {**reply, "retracted": False, "dry_run": True}
    reason = f"staged_stall halt false: driver {'session ' + driver if driver else 'fire'} in flight"
    return {**reply, "retracted": halted_run.mark_retracted(rdir, reason)}


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.retract_staged_stall": params `{run_id, dry_run?}`; reply as `retract_staged_stall`."""
    from coordinator_core.lifecycle import main_worktree_root

    if repo_root is None:
        return {"error": f"{OP_NAME} requires repo_root"}
    params = params if isinstance(params, dict) else {}
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"error": f"{OP_NAME} requires run_id"}
    return retract_staged_stall(main_worktree_root(Path(repo_root)), run_id, dry_run=params.get("dry_run") is True)

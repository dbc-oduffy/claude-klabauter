"""A fire that exits failed leaves a halt record for every run it was driving.

Without this a Workflow that dies mid-run (a worker reply that never parses, an agent that threw) exits
1 with no ``halted.json``: nothing counts it and a resumed run's prior record stays ``resumed``.
``halt_failed_fire`` is the one writer, through ``halted_run.write_halt_marker`` (what a first-time halt
uses): a ``resumed`` marker is replaced by a ``halted`` one naming the new failure; a ``halted`` marker
is left as is; a run with no record dir is not invented. The new record is then committed by
``dispatch.terminal_commit`` given ``halted_by``, the call a first halt's next_action makes, so it lands
as the same named-path halt commit and counts in the committed halt tally. Never raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

HALT_RUN_FAILED = "run_failed"
#: A launcher-child script answered `stage_pending` to a driver that never ran its staged twin (a fire with no launcher).
HALT_STAGE_PENDING_UNRESOLVED = "stage_pending_unresolved"
_REASON_MAX = 600
#: A record already halted, or closed out by a landing, is never overwritten by a later failed fire.
_KEEP_STATUSES = (halted_run.STATUS_HALTED, halted_run.STATUS_LANDED, halted_run.STATUS_SUPERSEDED, halted_run.STATUS_CLOSED)


def failed_run_ids(result: object, script: Optional[str]) -> list:
    """Run ids a failed fire drove: the failed children of a launcher result, else the run a digest result names, else the run its script is named for."""
    ids: list = []
    if isinstance(result, dict) and isinstance(result.get("runs"), list):
        ids += [r["run_id"] for r in result["runs"]
                if isinstance(r, dict) and r.get("status") == "failed" and isinstance(r.get("run_id"), str)]
    if not ids and isinstance(result, dict) and isinstance(result.get("run_id"), str):
        ids.append(result["run_id"])
    if not ids and script:
        stem = Path(script).name.split(".workflow")[0].split(".launcher")[0]
        if stem.startswith("warp-"):
            ids.append(stem)
    return ids


def stage_pending_cause(result: object) -> Optional[str]:
    """The named reason a fire that returned an unanswered `stage_pending` halts with: the stage id (manifest handle) and the next action."""
    pending = result.get("stage_pending") if isinstance(result, dict) else None
    if not pending:
        return None
    handle = pending.get("handle") if isinstance(pending, dict) else None
    handle = handle if isinstance(handle, dict) else {}
    stage = f"{handle.get('path')}@{str(handle.get('sha256'))[:12]}"
    twin = pending.get("twin_path") if isinstance(pending, dict) else None
    nxt = (f"fire the staged twin {twin}" if isinstance(twin, str) and twin
           else "refire the run so its stage is restaged")
    return f"stage {stage} was still pending when the script returned; next action: {nxt}"


def halt_failed_fire(repo_root: Path, run_ids: list, error: str, commits: Optional[dict] = None,
                     *, halted: str = HALT_RUN_FAILED) -> list:
    """Write and commit the halt record for each run; return the ids written. *commits* receives each commit reply.

    A named *halted* cause other than `run_failed` replaces a prior halt that names a different cause: that stale record
    (an adopted `orphaned` halt a refire re-armed) says nothing of why this fire failed, and leaving it commits no halt.
    """
    written: list = []
    reason = f"fire failed: {error}"[:_REASON_MAX]
    for run_id in run_ids:
        try:
            rd = record_dir(Path(repo_root), run_id)
            if not rd.is_dir():
                continue
            prior = halted_run._read(rd) or {}
            stale = (halted != HALT_RUN_FAILED and prior.get("status") == halted_run.STATUS_HALTED
                     and prior.get("halted") != halted)
            if prior.get("status") in _KEEP_STATUSES and not stale:
                continue
            sizing_rel = prior.get("sizing_rel")
            action = None
            if halted != HALT_RUN_FAILED and sizing_rel:
                from coordinator_core.ops.dispatch_emit.ask_contract import refire_next_action_for

                action = refire_next_action_for(halted, run_id=run_id, sizing_rel=sizing_rel, detail=reason)
            halted_run.write_halt_marker(
                rd, halted=halted, sizing_rel=sizing_rel,
                plan_rel=prior.get(halted_run.HALT_PLAN_FIELD), run_base_sha=prior.get(halted_run.HALT_RUN_BASE_FIELD),
                reason=reason, next_action=action,
            )
            written.append(run_id)
            reply = _commit(Path(repo_root), run_id, prior, reason, halted, action)
        except Exception as exc:
            reply = {"committed": False, "error": f"{type(exc).__name__}: {exc}"}
        if commits is not None and run_id in written:
            commits[run_id] = reply
    return written


def _commit(repo_root: Path, run_id: str, prior: dict, reason: str, halted: str = HALT_RUN_FAILED,
            action: Optional[dict] = None) -> dict:
    """Commit the run's halt record through the first-halt route (`dispatch.terminal_commit` + `halted_by`)."""
    from coordinator_core.lifecycle import git_common_dir
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM

    halted_by = {"halted": halted, "reason": reason, **({"next_action": action} if action else {})}
    plan = prior.get(halted_run.HALT_PLAN_FIELD)
    if isinstance(plan, str) and plan:
        halted_by[halted_run.HALT_PLAN_FIELD] = plan
    return terminal_commit._handler({"run_id": run_id, HALTED_RECORD_PARAM: halted_by}, repo_root=git_common_dir(repo_root))

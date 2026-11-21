"""workflow.cancel: stop one live warp run by run_id.

Writes the run's `halted.json` with `halted="cancelled"` (a halted record no refire resumes) and drops
the run from the live-run index, releasing its claim markers. `dispatch.ask_stage` refuses the next
stage start of a run whose marker reads cancelled (`cancelled_refusal`), and the headless driver checks it
before each agent. The run's live headless fire is stopped in-engine (`fire_stop`): identity-verified, tree
leaf-first, driver last. No git.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import halted_run, live_run_index
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, refire_next_action_for
from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params

CANCELLED = "cancelled"

_PARAMS = (
    Field("run_id", "nonempty_str", required=True),
    Field("reason", "str"),
)

_DEFAULT_DETAIL = "cancelled by workflow.cancel; fire the sizing fresh"


def cancelled_refusal(record_dir: Path) -> Optional[str]:
    """The refusal text when `record_dir`'s marker is a live `cancelled` halt, else None.

    One marker read. A refire rewrites the marker to `resumed`, which this lets through; a later
    halt of another kind overwrites `cancelled` and is not refused here either.
    """
    marker = halted_run.read_strict(Path(record_dir))
    if marker is None or marker.get("status") != halted_run.STATUS_HALTED or marker.get("halted") != CANCELLED:
        return None
    return (
        f"run {Path(record_dir).name} was cancelled by workflow.cancel; no further stage starts. "
        "Fire its sizing fresh (`args.fresh: true`)."
    )


@register_op("workflow.cancel")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.cancel": params `run_id` (str, required), `reason` (str, optional).

    Marks the live run cancelled in `state/warp/<run_id>/halted.json` (`halted: "cancelled"`, with a
    `next_action`), then drops it from the live-run index and releases its claims. Replies
    `{ok, run_id, halted, sizing, already_cancelled, stopped, released}`, plus `fire_still_running`
    (pids) with `released=false` when a verified fire process survives the stop. A run with no record dir, one whose
    marker is landed, closed or superseded, or one bound to no sizing replies `{"error": str}`;
    cancelling an already-cancelled run is an ok that changes nothing but the index.
    """
    if repo_root is None:
        return {"error": "workflow.cancel requires repo_root"}
    refusal = validate_params("workflow.cancel", params, _PARAMS, strict=True)
    if refusal is not None:
        return refusal
    run_id = params["run_id"]
    if not safe_id(run_id):
        return {"error": f"workflow.cancel: run_id {run_id!r} is not a safe run id"}
    root = main_worktree_root(Path(repo_root))
    record_dir = root / RECORD_DIR_ROOT / run_id
    if not record_dir.is_dir():
        return {"error": f"workflow.cancel: unknown run {run_id}: no record dir {RECORD_DIR_ROOT}/{run_id}"}
    try:
        marker = halted_run.read_strict(record_dir)
    except ValueError as exc:
        return {"error": f"workflow.cancel: {exc}"}
    status = (marker or {}).get("status")
    if status in (halted_run.STATUS_LANDED, halted_run.STATUS_CLOSED, halted_run.STATUS_SUPERSEDED):
        return {"error": f"workflow.cancel: run {run_id} is already {status}; nothing to cancel"}
    sizing_rel = halted_run.bound_sizing_rel(root, run_id)
    if status == halted_run.STATUS_HALTED and (marker or {}).get("halted") == CANCELLED:
        return _release(root, run_id, sizing_rel, already_cancelled=True)
    if sizing_rel is None:
        return {"error": f"workflow.cancel: run {run_id} is bound to no sizing, so a fresh fire could not bind it"}
    detail = (params.get("reason") or "").strip() or _DEFAULT_DETAIL
    prior = marker or {}
    halted_run.write_halt_marker(
        record_dir,
        halted=CANCELLED,
        sizing_rel=sizing_rel,
        plan_rel=prior.get(halted_run.HALT_PLAN_FIELD),
        run_base_sha=prior.get(halted_run.HALT_RUN_BASE_FIELD),
        held_rows=prior.get(halted_run.HALT_HELD_ROWS_FIELD),
        reason=detail,
        next_action=refire_next_action_for(CANCELLED, run_id=run_id, sizing_rel=sizing_rel, detail=detail),
    )
    return _release(root, run_id, sizing_rel, already_cancelled=False)


def _release(root: Path, run_id: str, sizing_rel: Optional[str], *, already_cancelled: bool) -> dict:
    """Stop the run's live fire(s), then drop the run from the index. The marker is already written, so a
    fire that outlives the stop halts itself at its next agent (`headless.execute`). A surviving verified
    fire PID keeps the run indexed and replies `released=false` with `fire_still_running`."""
    from coordinator_core.ops.dispatch_emit import fire_stop

    fires = fire_stop.stop_run_fires(root, run_id)
    reply = {
        "ok": True, "run_id": run_id, "halted": CANCELLED, "sizing": sizing_rel,
        "already_cancelled": already_cancelled, "stopped": fires["stopped"],
    }
    if fires["survivors"]:
        reply.update(released=False, fire_still_running=fires["survivors"])
        return reply
    live_run_index.remove(root, run_id)
    reply["released"] = live_run_index.run_holds_no_claim(root, run_id)
    return reply

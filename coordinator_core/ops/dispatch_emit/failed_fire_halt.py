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


def halt_failed_fire(repo_root: Path, run_ids: list, error: str, commits: Optional[dict] = None) -> list:
    """Write and commit the halt record for each run; return the ids written. *commits* receives each commit reply."""
    written: list = []
    reason = f"fire failed: {error}"[:_REASON_MAX]
    for run_id in run_ids:
        try:
            rd = record_dir(Path(repo_root), run_id)
            if not rd.is_dir():
                continue
            prior = halted_run._read(rd) or {}
            if prior.get("status") in _KEEP_STATUSES:
                continue
            halted_run.write_halt_marker(
                rd, halted=HALT_RUN_FAILED, sizing_rel=prior.get("sizing_rel"),
                plan_rel=prior.get(halted_run.HALT_PLAN_FIELD), run_base_sha=prior.get(halted_run.HALT_RUN_BASE_FIELD),
                reason=reason,
            )
            written.append(run_id)
            reply = _commit(Path(repo_root), run_id, prior, reason)
        except Exception as exc:
            reply = {"committed": False, "error": f"{type(exc).__name__}: {exc}"}
        if commits is not None and run_id in written:
            commits[run_id] = reply
    return written


def _commit(repo_root: Path, run_id: str, prior: dict, reason: str) -> dict:
    """Commit the run's halt record through the first-halt route (`dispatch.terminal_commit` + `halted_by`)."""
    from coordinator_core.lifecycle import git_common_dir
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM

    halted_by = {"halted": HALT_RUN_FAILED, "reason": reason}
    plan = prior.get(halted_run.HALT_PLAN_FIELD)
    if isinstance(plan, str) and plan:
        halted_by[halted_run.HALT_PLAN_FIELD] = plan
    return terminal_commit._handler({"run_id": run_id, HALTED_RECORD_PARAM: halted_by}, repo_root=git_common_dir(repo_root))

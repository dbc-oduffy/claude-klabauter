"""``dispatch.ship_hand_landed_batons``: ships a hand-landed run's claimed batons at the landed sha.

Params: ``run_id`` (string, required: the run whose work the EM landed by hand), ``sha`` (full 40-hex commit
sha, required: the landed work commit, an ancestor of HEAD). Resolves the run's claimed batons as the terminal
close does (``close_without_script._claimed_batons``: the close-job record's, else the run's own baton), runs
``run_close_baton_ship.baton_ship_leg`` at ``sha``, and settles the ``baton-ship`` leg on the run's close-job
record when one exists. Ships batons only: plan status, review stamp and completion entry are not touched.
A rerun is ``discharged`` and writes nothing. One git spawn (``rev-list``, the ancestry check).
Reply: ``{ok, run_id, sha, status, detail, batons, recorded}`` or ``{ok: false, error}``; ``status`` is the
leg's ``ran`` | ``discharged`` | ``not-applicable`` (the run claims no baton).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.ship_hand_landed_batons"

_SHA40 = re.compile(r"[0-9a-f]{40}")
_LEG = "baton-ship"


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _settle(root: Path, stem: str, record: Optional[dict], result) -> bool:
    """Settle the ``baton-ship`` leg on the close-job record; False when there is no record or it already holds a paid leg."""
    from coordinator_core.ops.dispatch_emit import run_close

    if record is None:
        return False
    held = (record.get("settled") or {}).get(_LEG)
    if isinstance(held, dict) and held.get("status") in ("ran", "discharged"):
        return False
    run_close.amend_close_job(root, stem, settled={_LEG: result})
    return True


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.ship_hand_landed_batons": ship the run's claimed batons at the landed sha."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_contract, run_close, run_token
    from coordinator_core.ops.dispatch_emit.close_without_script import _claimed_batons
    from coordinator_core.ops.dispatch_emit.halted_run_superseded import _all_ancestors_of_head
    from coordinator_core.ops.dispatch_emit.run_close_baton_ship import baton_ship_leg

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    run_id, sha = params.get("run_id"), params.get("sha")
    if not isinstance(run_id, str) or not run_id or not safe_id(run_id):
        return _refuse(f"{OP_NAME} requires a safe string run_id")
    if not isinstance(sha, str) or not _SHA40.fullmatch(sha.strip().lower()):
        return _refuse(f"{OP_NAME} requires sha: the full 40-hex landed commit")
    sha = sha.strip().lower()
    root = main_worktree_root(Path(repo_root))

    stem = run_close.resolve_close_job_stem(root, run_id)
    record = run_close._load(root, stem) if stem else None
    try:
        known = record is not None or ask_contract.record_dir(root, run_id).is_dir()
    except ValueError as exc:
        return _refuse(str(exc))
    if not known:
        return _refuse(f"unknown run {run_id}: no close-job record and no run record directory")
    if run_token.is_live(root, run_id):
        return _refuse(f"{run_id} holds a live run token; a running run closes itself")

    batons = _claimed_batons(root, run_id, record or {})
    if not batons:
        return {
            "ok": True, "run_id": run_id, "sha": sha, "status": "not-applicable",
            "detail": "run claims no baton", "batons": [], "recorded": False,
        }
    why = _all_ancestors_of_head(root, [sha])
    if why:
        return _refuse(why)
    result = baton_ship_leg(root, run_id, batons, sha)
    if result.status == "failed":
        return _refuse(result.detail)
    recorded = _settle(root, stem, record, result) if stem else False
    return {
        "ok": True, "run_id": run_id, "sha": sha, "status": result.status,
        "detail": result.detail, "batons": batons, "recorded": recorded,
    }

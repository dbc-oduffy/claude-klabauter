"""Orphaned-run verdict: may this session adopt a run dir whose emitting session is gone?

One run-stage.json read, one halted.json read, one scandir per record root for a superseding run,
one `session_live` call. No git, no corpus scan.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT, RUN_DIR_ROOT, RUN_STAGE_FILE, _read_json_object,
)

OP_NAME = "dispatch.adopt_orphaned_run"
HALTED_KIND = "orphaned"
#: Halt kind of a gate fire that halted to hand over the adopt call; it never supersedes the orphan.
ORPHAN_PENDING = "orphan_pending"

_TERMINAL_STAGES = ("halted", "landed")
_ROOTS = (RECORD_DIR_ROOT, RUN_DIR_ROOT)


def adopt_call(run_id: str, repo_root) -> str:
    """The one engine call that adopts `run_id` under the calling session."""
    params = json.dumps({"repo_root": Path(repo_root).as_posix(), "run_id": run_id})
    return f"coordinator-invoke {OP_NAME} '{params}'"


def run_dir_of(repo_root: Path, run_id: str) -> Optional[Path]:
    """The existing run dir for `run_id`: state/warp first, then scratch/warp; None when absent or unsafe."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        return None
    for rel in _ROOTS:
        cand = Path(repo_root) / rel / run_id
        if cand.is_dir():
            return cand
    return None


def _stamp(run_id: str) -> str:
    """The 22-char `%Y%m%dT%H%M%S-hex6` tail of an `ask-*` / `warp-<stem>-*` run id."""
    return run_id[-22:]


def _read_marker(dir_: Path) -> Optional[dict]:
    return _read_json_object(dir_ / "halted.json")


def _superseder(repo_root: Path, run_id: str, sizing_rel: str) -> Optional[str]:
    """A later `warp-<stem>-*` / `ask-*` run binding `sizing_rel`, else None."""
    stem = PurePosixPath(sizing_rel.replace("\\", "/")).stem
    prefix = f"warp-{stem}-"
    mine = _stamp(run_id)
    found: Optional[str] = None
    for rel in _ROOTS:
        try:
            with os.scandir(Path(repo_root) / rel) as it:
                for entry in it:
                    name = entry.name
                    if name == run_id or not entry.is_dir() or not safe_id(name):
                        continue
                    if name.startswith(prefix):
                        later = name[len(prefix):] > mine and len(name) == len(prefix) + len(mine)
                    elif name.startswith("ask-"):
                        later = name[4:] > mine and len(name) - 4 == len(mine)
                    else:
                        continue
                    if not later or (found is not None and name <= found):
                        continue
                    run = _read_json_object(Path(entry.path) / "run.json") if name.startswith("ask-") else None
                    if name.startswith("ask-") and (
                        PurePosixPath(str((run or {}).get("sizing_rel") or "").replace("\\", "/")).as_posix()
                        != PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()
                    ):
                        continue
                    marker = _read_marker(Path(entry.path))
                    if marker and marker.get("halted") == ORPHAN_PENDING:
                        continue
                    found = name
        except OSError:
            continue
    return found


def orphan_verdict(repo_root: Path, run_id: str, session_id: str) -> dict:
    """`{orphaned, reason, recorded_session, stage, superseded_by}` for `run_id` as seen by `session_id`.

    `orphaned` is True only with reason "ok". Refusals: "no-run-stage", "terminal-stage",
    "already-halted", "self", "owner-live", "superseded" (`superseded_by` names the later run).
    """
    verdict = {"orphaned": False, "reason": None, "recorded_session": None, "stage": None, "superseded_by": None}
    dir_ = run_dir_of(repo_root, run_id)
    stage_rec = _read_json_object(dir_ / RUN_STAGE_FILE) if dir_ else None
    if stage_rec is None:
        return {**verdict, "reason": "no-run-stage"}
    recorded = stage_rec.get("session_id")
    recorded = recorded if isinstance(recorded, str) and recorded else None
    stage = stage_rec.get("stage")
    verdict.update(recorded_session=recorded, stage=stage)
    if stage in _TERMINAL_STAGES:
        return {**verdict, "reason": "terminal-stage"}
    if _read_marker(dir_) is not None:
        return {**verdict, "reason": "already-halted"}
    if recorded is not None:
        if recorded == session_id:
            return {**verdict, "reason": "self"}
        from coordinator_core.session.liveness import session_live

        if session_live(recorded):
            return {**verdict, "reason": "owner-live"}
    run = _read_json_object(dir_ / "run.json") or {}
    sizing_rel = run.get("sizing_rel")
    if isinstance(sizing_rel, str) and sizing_rel:
        later = _superseder(Path(repo_root), run_id, sizing_rel)
        if later:
            return {**verdict, "reason": "superseded", "superseded_by": later}
    return {**verdict, "orphaned": True, "reason": "ok"}


def _plan_rel(repo_root: Path, run_dir: Path) -> Optional[str]:
    """The run's plan, named by its `<plan-stem>.spine.md` record file, when `docs/plans/<stem>.md` exists."""
    try:
        names = sorted(e.name for e in os.scandir(run_dir) if e.name.endswith(".spine.md"))
    except OSError:
        return None
    for name in names:
        rel = f"docs/plans/{name[:-len('.spine.md')]}.md"
        if (Path(repo_root) / rel).is_file():
            return rel
    return None


def _resume_point(repo_root: Path, run_dir: Path) -> tuple[str, str, Optional[str]]:
    """`(halted kind, first stage without a landed receipt, plan_rel)`, by the evidence on disk."""
    from coordinator_core.ops.dispatch_emit import ask_stage_receipt as receipt

    plan_rel = _plan_rel(repo_root, run_dir)
    data = receipt._read_receipt(run_dir)
    if data is not None and receipt.landed(run_dir, data["digest"]) is not None:
        return f"{HALTED_KIND}_staged", "execute", plan_rel
    if plan_rel is not None:
        return f"{HALTED_KIND}_planned", "stage", plan_rel
    return HALTED_KIND, "plan", None


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.adopt_orphaned_run": adopt a dead session's run under the caller.

    Params `{run_id, session_id?, dry_run?}`. Reply `{ok, run_id, adopted_from, resume_stage, halted,
    next_action}` or `{ok: false, reason, ...}` (the verdict's refusal reason, `superseded_by` when
    superseded). Writes `run-stage.json` and `halted.json` only; re-executes nothing."""
    from coordinator_core.lifecycle import main_worktree_root
    from coordinator_core.ops.dispatch_emit import halt_refire
    from coordinator_core.ops.dispatch_emit.ask_contract import adopt_run_stage_at
    from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
    from coordinator_core.session.core import resolve_session_id

    if repo_root is None:
        return {"error": f"{OP_NAME} requires repo_root"}
    params = params if isinstance(params, dict) else {}
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"error": f"{OP_NAME} requires run_id"}
    session_id = params.get("session_id")
    if not (isinstance(session_id, str) and session_id):
        session_id = resolve_session_id()
    if not session_id:
        return {"ok": False, "run_id": run_id, "reason": "no-session"}
    root = main_worktree_root(Path(repo_root))
    verdict = orphan_verdict(root, run_id, session_id)
    if not verdict["orphaned"]:
        out = {"ok": False, "run_id": run_id, "reason": verdict["reason"]}
        if verdict["superseded_by"]:
            out["superseded_by"] = verdict["superseded_by"]
        return out
    run_dir = run_dir_of(root, run_id)
    sizing_rel = (_read_json_object(run_dir / "run.json") or {}).get("sizing_rel")
    if not isinstance(sizing_rel, str) or not sizing_rel:
        return {"ok": False, "run_id": run_id, "reason": "no-sizing"}
    halted, stage, plan_rel = _resume_point(root, run_dir)
    call = f'Workflow({{name: "coordinator:engine-ask", args: {{"sizing": {json.dumps(sizing_rel)}}}}})'
    next_action = halt_refire.refire_next_action(
        call=call, run_id=run_id, sizing=sizing_rel, halted=halted, stage=stage,
        detail=f"Adopted from dead session {verdict['recorded_session']}; resumes at stage {stage}.",
    )
    adopted_from = [verdict["recorded_session"]] if verdict["recorded_session"] else []
    reply = {
        "ok": True, "run_id": run_id, "adopted_from": adopted_from, "resume_stage": stage,
        "halted": halted, "next_action": next_action,
    }
    if params.get("dry_run") is True:
        return {**reply, "dry_run": True}
    # Marker first: a failure between the two writes leaves a halted run the refire resumes, never an
    # adopted-but-unhalted one that the verdict reads as "self" and no session can re-adopt.
    write_halt_marker(
        run_dir, halted=halted, sizing_rel=sizing_rel, plan_rel=plan_rel,
        reason=f"orphaned: session {verdict['recorded_session']} is gone", next_action=next_action,
    )
    adopt_run_stage_at(run_dir, session_id)
    return reply

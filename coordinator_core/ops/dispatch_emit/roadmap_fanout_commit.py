"""The roadmap fan-out record commit: ``dispatch.terminal_commit`` given ``fanout``.

A roadmap-route run plans and does not execute: this lands the planning record as one
``ceremony.commit_v2`` call (the run's ``state/warp/<run_id>/`` top-level files, each ready plan
with its cited sizing and baton, whichever of them differ from the index), then fans each plan out
as its own sibling run through ``ask_fanout``'s sizings arm. A plan with no resolvable sizing
refuses before any write. A path a live peer's claim holds is withheld and named. A fan-out
refusal after a landed commit is reported under ``fanout.error`` and never un-lands the commit.
The run token is redeemed, then consumed on a landed commit and released otherwise.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import FANOUT_RECORD_PARAM, RECORD_DIR_ROOT

PLANS_ROOT = "docs/plans"


def is_fanout_record_call(params: object) -> bool:
    """True when ``params[FANOUT_RECORD_PARAM]`` is a dict with a non-empty list of str ``plans``."""
    spec = params.get(FANOUT_RECORD_PARAM) if isinstance(params, dict) else None
    plans = spec.get("plans") if isinstance(spec, dict) else None
    return isinstance(plans, list) and bool(plans) and all(isinstance(p, str) and p for p in plans)


def _plan_writes(worktree_root: Path, plan_rel: str) -> Optional[list]:
    """The union of the plan's row writes in spine order, or ``None`` when it declares none."""
    from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo
    from coordinator_core.ops.dispatch_emit.terminal_commit import _read_rel

    text = _read_rel(worktree_root, plan_rel)
    if text is None:
        return None
    out: list = []
    for row in load_rows_memo(text).rows:
        for key in ("writes", "writes_under"):
            value = row.get(key)
            for path in value if isinstance(value, list) else ():
                if isinstance(path, str) and path.strip() and path not in out:
                    out.append(path.strip())
    return out or None


def _resolve_plans(worktree_root: Path, plans: list) -> tuple[Optional[list], Optional[dict]]:
    """``[(plan, sizing, baton, writes)]`` per plan, or ``(None, refusal)`` naming the first bad one."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _cited_baton, _cited_sizing, _error

    out: list = []
    for plan in plans:
        guarded = contained_path(worktree_root / plan, [worktree_root / PLANS_ROOT])
        if guarded is None or not guarded.is_file():
            return None, _error(f"fanout plan {plan!r} is not a file under {PLANS_ROOT}", refused="fanout-plan-path")
        rel = guarded.relative_to(worktree_root.resolve()).as_posix()
        sizing = _cited_sizing(worktree_root, rel)
        if sizing is None:
            return None, _error(f"fanout plan {rel!r} cites no sizing_object under state/sizings", refused="fanout-no-sizing")
        out.append((rel, sizing, _cited_baton(worktree_root, sizing), _plan_writes(worktree_root, rel)))
    return out, None


def commit_fanout_record(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error

    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _error(f"params.run_id {run_id!r} is not a single safe path segment", refused="run-id-unsafe")
    if contained_path(worktree_root / RECORD_DIR_ROOT / run_id, [worktree_root / RECORD_DIR_ROOT]) is None:
        return _error(f"record dir for {run_id!r} escapes {RECORD_DIR_ROOT}", refused="record-dir-escapes")

    token = params.get("run_token")
    script_path = params.get("script_path")
    if token is not None and not run_token.redeem(worktree_root, run_id, token, script_path):
        return _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
    landed = False
    try:
        reply = _land_and_fan_out(worktree_root, repo_root, params, run_id)
        landed = bool(reply.get("committed"))
        return reply
    finally:
        if token is not None:
            if landed:
                run_token.consume(worktree_root, run_id)
            else:
                run_token.release(worktree_root, run_id, token, script_path)


def _land_and_fan_out(worktree_root: Path, repo_root: Path, params: dict, run_id: str) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit import ask_fanout
    from coordinator_core.ops.dispatch_emit.halted_record_commit import record_dir_changes
    from coordinator_core.ops.dispatch_emit.run_close import sibling_release
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error, _peer_held, _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    record_dir_rel = f"{RECORD_DIR_ROOT}/{run_id}"
    base = {"record_dir": record_dir_rel}
    resolved, refusal = _resolve_plans(worktree_root, params[FANOUT_RECORD_PARAM]["plans"])
    if refusal is not None:
        return refusal
    session_id = params.get("session_id")
    if not (isinstance(session_id, str) and _UUID_RE.fullmatch(session_id)):
        session_id = None

    declared: list = []
    for plan, sizing, baton, _writes in resolved:
        for path in (plan, sizing, baton):
            if path and path not in declared:
                declared.append(path)
    snapshot = _worktree_snapshot(worktree_root, declared, [record_dir_rel], [])
    if snapshot is None:
        return _error(f"git status over {record_dir_rel} failed", **base)
    record, record_gone = record_dir_changes(worktree_root, record_dir_rel, snapshot.changed)
    product = [p for p in declared if p in snapshot.changed and (worktree_root / p).is_file()]
    held = _peer_held(worktree_root, session_id, product)
    withheld = {p: "peer-held" for p in sorted(held)}
    product = [p for p in product if p not in held]
    paths = record + product
    if not paths and not record_gone:
        return {
            "committed": False, "sha": None, "nothing_to_commit": True,
            **({"withheld": withheld} if withheld else {}), **base,
        }

    stems = [PurePosixPath(plan).stem for plan, *_ in resolved]
    message = f"warp: record roadmap {run_id} plans and fan out\n\nPlans: " + ", ".join(stems)
    commit_params: dict = {"paths": paths + record_gone, "message": message}
    if record_gone:
        commit_params["deleted_paths"] = record_gone
        commit_params["declared_reverts"] = record_gone
    if product:
        commit_params["ignored_untracked"] = snapshot.ignored_among(paths)
    if session_id is not None:
        commit_params["session_id"] = session_id
    try:
        reply = reentrant_dispatch("ceremony.commit_v2", commit_params, repo_root=repo_root)
    except OpUnavailableError:
        return _error("ceremony.commit_v2 is not registered", **base)
    if not isinstance(reply, dict):
        return _error(f"unexpected commit_v2 reply: {reply!r}", **base)
    out: dict = {**reply, "paths": paths + record_gone, **({"withheld": withheld} if withheld else {}), **base}
    sha = reply.get("sha")
    if not (reply.get("committed") and sha):
        return out

    items = []
    for plan, sizing, _baton, writes in resolved:
        item: dict = {"sizing": sizing, "label": PurePosixPath(plan).stem}
        if writes is not None:
            item["writes"] = writes
        items.append(item)
    out["fanout"] = ask_fanout.fanout({"sizings": items}, worktree_root)
    siblings = sibling_release(worktree_root, run_id, sha)
    if siblings is not None:
        out["siblings"] = siblings
    return out

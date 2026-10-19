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

import yaml

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import FANOUT_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
    INTERACTION_MODE_PARAM,
    NOT_FIRED_KEY,
    PARENT_SIZING_KEY,
    not_fired_entry,
    not_fired_refusal,
)
from coordinator_core.session.record_homes import home_dir

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


def _plan_waves(spec: dict) -> tuple[dict, Optional[dict]]:
    """``({plan: wave}, None)`` from the optional ``waves`` mapping, or ``({}, refusal)``."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error

    waves = spec.get("waves")
    if waves is None:
        return {}, None
    if not isinstance(waves, dict):
        return {}, _error("fanout waves must map plan path to wave", refused="fanout-waves")
    for plan, wave in waves.items():
        if plan not in spec["plans"]:
            return {}, _error(f"fanout waves names {plan!r}, not in plans", refused="fanout-waves")
        if isinstance(wave, bool) or not isinstance(wave, int) or wave < 0:
            return {}, _error(f"fanout wave for {plan!r} must be an integer >= 0", refused="fanout-waves")
    return waves, None


def _parent_mode(worktree_root: Path, parent: object) -> tuple[Optional[str], Optional[dict]]:
    """``(interaction_mode or None, None)`` from the parent run's sizing, or ``(None, refusal)``."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error

    if parent is None:
        return None, None
    home = Path(home_dir(str(worktree_root), "sizings"))
    guarded = contained_path(worktree_root / parent, [home]) if isinstance(parent, str) and parent else None
    if guarded is None or not guarded.is_file():
        return None, _error(f"fanout {PARENT_SIZING_KEY} {parent!r} is not a file under the sizings home", refused="fanout-parent-sizing")
    doc = yaml.safe_load(guarded.read_text(encoding="utf-8"))
    mode = doc.get("interaction_mode") if isinstance(doc, dict) else None
    return (mode if isinstance(mode, str) and mode else None), None


def _run_mode(spec: dict) -> Optional[str]:
    """The run's own `interaction_mode` the fan-out spec carries, else None."""
    mode = spec.get(INTERACTION_MODE_PARAM)
    return mode.strip() if isinstance(mode, str) and mode.strip() else None


def _plan_entries(plans: list, waves: dict, reason: str, detail: Optional[str] = None) -> list:
    return [not_fired_entry(reason=reason, plan=p, wave=waves.get(p), detail=detail) for p in plans]


def _unfired_siblings(fan: object, plans: list, waves: dict, sizings: list) -> list:
    """Entries for every plan the fan-out reply gave no fire line."""
    if not isinstance(fan, dict):
        return _plan_entries(plans, waves, "fanout-refused", f"unexpected fan-out reply: {fan!r}")
    if fan.get("exit_code") == 1:
        return _plan_entries(plans, waves, "fanout-refused", str(fan.get("error")))
    ready = {r.get("sizing") for r in fan.get("ready") or () if isinstance(r, dict)}
    runs = {r.get("label"): r for r in fan.get("runs") or () if isinstance(r, dict)}
    out: list = []
    for plan, sizing in zip(plans, sizings):
        if sizing in ready:
            continue
        after = (runs.get(PurePosixPath(plan).stem) or {}).get("after")
        entry = (
            ("after", "waits on " + ", ".join(after)) if after else ("queued", None)
        )
        out.append(not_fired_entry(reason=entry[0], plan=plan, wave=waves.get(plan), detail=entry[1]))
    return out


def _resolve_plans(worktree_root: Path, plans: list) -> tuple[Optional[list], Optional[dict]]:
    """``[(plan, sizing, baton, writes)]`` per plan, or ``(None, refusal)`` naming the first bad one."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _cited_baton, _cited_sizing, _error

    out: list = []
    for plan in plans:
        guarded = contained_path(worktree_root / plan, [worktree_root / PLANS_ROOT])
        if guarded is None or not guarded.is_file():
            return None, _error(f"fanout plan {plan!r} is not a file under {PLANS_ROOT}", refused="fanout-plan-path")
        rel = guarded.relative_to(worktree_root.resolve()).as_posix()
        if not guarded.read_text(encoding="utf-8", errors="replace").strip():
            return None, _error(f"fanout plan {rel!r} is empty (zero bytes or whitespace only)", refused="fanout-plan-empty")
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
        landed = bool(reply.get("committed") or reply.get("fanned_out_at_head"))
        return reply
    finally:
        if token is not None:
            if landed:
                run_token.consume(worktree_root, run_id)
            else:
                run_token.release(worktree_root, run_id, token, script_path)


def _head_sha_when_all_tracked(worktree_root: Path, record_dir_rel: str, declared: list) -> Optional[str]:
    """HEAD's sha when the record dir holds a tracked file and every declared plan, sizing and baton
    is tracked; ``None`` otherwise. Callers have already seen none of them differ from the index/HEAD."""
    from coordinator_core.git import git_index
    from coordinator_core.git.git_state import head_sha

    try:
        entries = git_index.parse_index_identity(worktree_root, set(declared), prefixes=[record_dir_rel])
    except git_index.IndexParseError:
        return None
    prefix = record_dir_rel + "/"
    if not any(p.startswith(prefix) for p in entries) or any(p not in entries for p in declared):
        return None
    return head_sha(worktree_root, fresh=True)


def _land_and_fan_out(worktree_root: Path, repo_root: Path, params: dict, run_id: str) -> dict:
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit import ask_fanout
    from coordinator_core.ops.dispatch_emit.halted_record_commit import record_dir_changes
    from coordinator_core.ops.dispatch_emit.run_close import sibling_release
    from coordinator_core.ops.dispatch_emit.terminal_commit import _error, _peer_held, _worktree_snapshot
    from coordinator_core.warm.entry_seam import OpUnavailableError, reentrant_dispatch

    record_dir_rel = f"{RECORD_DIR_ROOT}/{run_id}"
    base = {"record_dir": record_dir_rel}
    spec = params[FANOUT_RECORD_PARAM]
    waves, refusal = _plan_waves(spec)
    if refusal is not None:
        return refusal
    upstream = spec.get(NOT_FIRED_KEY, [])
    message = not_fired_refusal(upstream)
    if message is not None:
        return _error(f"fanout {message}", refused="fanout-not-fired")
    inherited, refusal = _parent_mode(worktree_root, spec.get(PARENT_SIZING_KEY))
    if refusal is not None:
        return refusal
    inherited = inherited or _run_mode(spec)
    resolved, refusal = _resolve_plans(worktree_root, spec["plans"])
    if refusal is not None:
        return refusal
    not_fired: list = [dict(entry) for entry in upstream]
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
    held = _peer_held(worktree_root, session_id, product, run_id=run_id)
    withheld = {p: "peer-held" for p in sorted(held)}
    product = [p for p in product if p not in held]
    paths = record + product
    if not paths and not record_gone:
        head = None if withheld else _head_sha_when_all_tracked(worktree_root, record_dir_rel, declared)
        if head is None:
            not_fired += _plan_entries(spec["plans"], waves, "nothing-to-commit")
            return {
                "committed": False, "sha": None, "nothing_to_commit": True,
                **({"withheld": withheld} if withheld else {}), **base,
                "fanout": {NOT_FIRED_KEY: not_fired},
            }
        sha = head
        out: dict = {"committed": False, "sha": head, "nothing_to_commit": True, "fanned_out_at_head": True, **base}
    else:
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
            reply = None
            failure = "ceremony.commit_v2 is not registered"
        else:
            failure = None if isinstance(reply, dict) else f"unexpected commit_v2 reply: {reply!r}"
        if failure is not None:
            not_fired += _plan_entries(spec["plans"], waves, "commit-not-landed", failure)
            return _error(failure, fanout={NOT_FIRED_KEY: not_fired}, **base)
        out = {**reply, "paths": paths + record_gone, **({"withheld": withheld} if withheld else {}), **base}
        sha = reply.get("sha")
        if not (reply.get("committed") and sha):
            detail = str(reply.get("error") or reply.get("refused") or "commit did not land")
            out["fanout"] = {NOT_FIRED_KEY: not_fired + _plan_entries(spec["plans"], waves, "commit-not-landed", detail)}
            return out

    items = []
    for given, (plan, sizing, _baton, writes) in zip(spec["plans"], resolved):
        item: dict = {"sizing": sizing, "label": PurePosixPath(plan).stem}
        if given in waves:
            item["wave"] = waves[given]
        if writes is not None:
            item["writes"] = writes
        items.append(item)
    fan_params: dict = {"sizings": items}
    if inherited:
        fan_params[INTERACTION_MODE_PARAM] = inherited
    fan = ask_fanout.fanout(fan_params, worktree_root)
    not_fired += _unfired_siblings(fan, spec["plans"], waves, [i["sizing"] for i in items])
    out["fanout"] = {**fan, NOT_FIRED_KEY: not_fired}
    siblings = sibling_release(worktree_root, run_id, sha)
    if siblings is not None:
        out["siblings"] = siblings
    return out

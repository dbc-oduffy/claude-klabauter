"""The fanout mode of ``dispatch.land_run``: a roadmap run's record commit, then the plans' fan-out.

A roadmap-route run plans and does not execute. This lands the planning record as one
``commit_paths`` commit: the run's ``state/warp/<run_id>/`` top-level files plus each ready plan with
its cited sizing and baton, whichever differ from HEAD. Landing-set membership is closed: only those
paths enter, so a peer run's dirty file is never staged. Each plan then fans out as its own sibling
run through ``ask_fanout``'s sizings arm. A plan with no resolvable sizing refuses before any write.
A path a live peer's claim holds is withheld whole and named. A fan-out refusal after a landed commit
is reported under ``fanout.error`` and never un-lands the commit. The run token is redeemed, consumed
(with a spent receipt) on a landed commit and released otherwise; a repeat call after a landing is
answered ``already_landed``.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path, PurePosixPath
from typing import Optional

import yaml

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import FANOUT_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
    EDGES_FROM_BLOCKED_BY,
    EDGES_FROM_PARAM,
    HELD_KEY,
    INTERACTION_MODE_PARAM,
    ITEM_BLOCKED_BY_KEY,
    ITEM_IDS_KEY,
    NOT_FIRED_KEY,
    PARENT_SIZING_KEY,
    not_fired_entry,
    not_fired_refusal,
)
from coordinator_core.session.record_homes import home_dir

PLANS_ROOT = "docs/plans"


def _error(message: str, **extra: object) -> dict:
    return {"committed": False, "sha": None, "error": message, **extra}


def land(worktree_root: Path, repo_root: Path, params: dict) -> dict:
    """Mode entry ``land_run`` imports: redeem the run token, land the record, fan the plans out.

    Reply: ``committed``, ``sha``, ``paths``, ``record_dir``, ``withheld`` (peer-held paths),
    ``fanout`` (the ``ask_fanout`` reply plus ``not_fired``), ``siblings``; ``fanned_out_at_head``
    when the record was already at HEAD; ``already_landed`` on a repeat call."""
    from coordinator_core.ops.dispatch_emit import run_token

    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _error(f"params.run_id {run_id!r} is not a single safe path segment", refused="run-id-unsafe")
    if contained_path(worktree_root / RECORD_DIR_ROOT / run_id, [worktree_root / RECORD_DIR_ROOT]) is None:
        return _error(f"record dir for {run_id!r} escapes {RECORD_DIR_ROOT}", refused="record-dir-escapes")

    token = params.get("run_token")
    script_path = params.get("script_path")
    if token is not None and not run_token.redeem(worktree_root, run_id, token, script_path):
        receipt = run_token.spent_receipt(worktree_root, run_id, token, script_path)
        if receipt is not None:
            return {"committed": True, "sha": receipt.get("sha"), "already_landed": True}
        return _error(
            "params.run_token does not match a live, unspent token minted for params.run_id and "
            "this script (a token admits one landed commit)",
            refused="run-token",
        )
    landed = None
    try:
        reply = _land_and_fan_out(worktree_root, params, run_id)
        if reply.get("committed") or reply.get("fanned_out_at_head"):
            landed = {"sha": reply.get("sha"), "paths": reply.get("paths") or []}
        return reply
    finally:
        if token is not None:
            if landed is not None:
                run_token.consume(worktree_root, run_id, landed=landed)
            else:
                run_token.release(worktree_root, run_id, token, script_path)


def _plan_writes(worktree_root: Path, plan_rel: str) -> Optional[list]:
    """The union of the plan's row writes in spine order, or ``None`` when it declares none."""
    from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel

    text = read_rel(worktree_root, plan_rel)
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
    if parent is None:
        return None, None
    home = Path(home_dir(str(worktree_root), "sizings"))
    guarded = contained_path(worktree_root / parent, [home]) if isinstance(parent, str) and parent else None
    if guarded is None or not guarded.is_file():
        return None, _error(
            f"fanout {PARENT_SIZING_KEY} {parent!r} is not a file under the sizings home",
            refused="fanout-parent-sizing",
        )
    doc = yaml.safe_load(guarded.read_text(encoding="utf-8"))
    mode = doc.get("interaction_mode") if isinstance(doc, dict) else None
    return (mode if isinstance(mode, str) and mode else None), None


def _run_mode(spec: dict) -> Optional[str]:
    """The run's own ``interaction_mode`` the fan-out spec carries, else None."""
    mode = spec.get(INTERACTION_MODE_PARAM)
    return mode.strip() if isinstance(mode, str) and mode.strip() else None


def _baton_edge_fields(worktree_root: Path, baton_rel: Optional[str]) -> tuple[list, list]:
    """``(ids, blocked_by)`` from the baton frontmatter: its non-empty str ``stub_id`` and
    ``handoff_id``, and ``blocked_by`` (a scalar reads as one item). ``([], [])`` when the baton is
    missing or its frontmatter does not parse. In-process read: no spawn."""
    from coordinator_core.frontmatter.primitives import split_frontmatter
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel

    text = read_rel(worktree_root, baton_rel) if baton_rel else None
    split = split_frontmatter(text) if text is not None else None
    if split is None:
        return [], []
    try:
        fm = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return [], []
    if not isinstance(fm, dict):
        return [], []
    ids = [v for k in ("stub_id", "handoff_id") for v in [fm.get(k)] if isinstance(v, str) and v]
    raw = fm.get("blocked_by")
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return ids, [v for v in items if isinstance(v, str) and v]


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
    held = {r.get("label"): r.get("reason") for r in fan.get(HELD_KEY) or () if isinstance(r, dict)}
    out: list = []
    for plan, sizing in zip(plans, sizings):
        if sizing in ready:
            continue
        label = PurePosixPath(plan).stem
        if label in held:
            out.append(not_fired_entry(reason="held", plan=plan, wave=waves.get(plan), detail=held[label]))
            continue
        after = (runs.get(label) or {}).get("after")
        reason, detail = ("after", "waits on " + ", ".join(after)) if after else ("queued", None)
        out.append(not_fired_entry(reason=reason, plan=plan, wave=waves.get(plan), detail=detail))
    return out


def _cited_baton(worktree_root: Path, sizing_rel: str) -> Optional[str]:
    """Worktree-relative path of the handoff the sizing names as its top-level ``baton:``, when it is
    an existing file under the handoffs home."""
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel

    text = read_rel(worktree_root, sizing_rel)
    if text is None:
        return None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    cited = doc.get("baton") if isinstance(doc, dict) else None
    if not isinstance(cited, str) or not cited:
        return None
    guarded = contained_path(worktree_root / cited, [Path(home_dir(str(worktree_root), "handoffs"))])
    if guarded is None or not guarded.is_file():
        return None
    return guarded.relative_to(worktree_root.resolve()).as_posix()


def _cited_sizing(worktree_root: Path, plan_rel: str) -> Optional[str]:
    """Worktree-relative path of the existing sizing file under the sizings home the plan's
    frontmatter cites as ``sizing_object:``."""
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.ops.dispatch_emit.register_gate import sizing_file_rel
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel

    text = read_rel(worktree_root, plan_rel)
    split = split_frontmatter(text) if text is not None else None
    cited = read_fm_field_unquoted(split.fm_text, "sizing_object") if split else None
    return sizing_file_rel(worktree_root, cited) if cited else None


def _resolve_plans(worktree_root: Path, plans: list) -> tuple[Optional[list], Optional[dict]]:
    """``[(plan, sizing, baton, writes)]`` per plan, or ``(None, refusal)`` naming the first bad one."""
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


def _peer_held(worktree_root: Path, session_id: Optional[str], paths: list, run_id: str) -> set:
    """The ``paths`` another session's touch claim, or a live peer run's declared writes, holds
    (DR-462, whole path). ``run_id`` keeps this run from holding its own declared paths. No git spawn."""
    if not paths:
        return set()
    held: set = set()
    if session_id:
        from coordinator_core.session import claim_index

        try:
            answer = claim_index.classify_paths(session_id, paths, cwd=str(worktree_root))
        except Exception:  # noqa: BLE001 -- an unreadable claim index holds nothing, as the old landing did
            return set()
        held = {p for p, o in answer.by_path.items() if o.verdict == claim_index.OWNERSHIP_PEER}
    from coordinator_core.ops import warp_run_deliverable

    try:
        run_claims = warp_run_deliverable.live_run_write_claims(
            worktree_root, paths, exclude_session=session_id or "", cwd=str(worktree_root), exclude_run=run_id,
        )
    except Exception:  # noqa: BLE001
        run_claims = None
    return held | set(run_claims or ())


def _head_sha_when_all_tracked(worktree_root: Path, record_dir_rel: str, declared: list) -> Optional[str]:
    """HEAD's sha when the record dir holds a tracked file and every declared plan, sizing and baton
    is tracked; ``None`` otherwise. Callers have already seen none of them differ from HEAD."""
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


def _land_and_fan_out(worktree_root: Path, params: dict, run_id: str) -> dict:
    from coordinator_core.git.commit import CommitRefused, NothingToCommit, commit_paths, hash_worktree_blobs_via_spawn
    from coordinator_core.git.commit_trailers import _UUID_RE
    from coordinator_core.ops.dispatch_emit import ask_fanout
    from coordinator_core.ops.dispatch_emit.run_close import sibling_release
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import record_dir_changes, worktree_snapshot

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
    snapshot = worktree_snapshot(worktree_root, declared, [record_dir_rel], [])
    if snapshot is None:
        return _error(f"git status over {record_dir_rel} failed", **base)
    record, record_gone = record_dir_changes(worktree_root, record_dir_rel, snapshot.changed)
    product = [p for p in declared if p in snapshot.changed and (worktree_root / p).is_file()]
    held = _peer_held(worktree_root, session_id, product, run_id)
    withheld = {p: "peer-held" for p in sorted(held)}
    product = [p for p in product if p not in held]
    paths = record + product

    out: dict
    if not paths and not record_gone:
        head = None if withheld else _head_sha_when_all_tracked(worktree_root, record_dir_rel, declared)
        if head is None:
            return {
                "committed": False, "sha": None, "nothing_to_commit": True,
                **({"withheld": withheld} if withheld else {}), **base,
                "fanout": {NOT_FIRED_KEY: not_fired + _plan_entries(spec["plans"], waves, "nothing-to-commit")},
            }
        sha = head
        out = {"committed": False, "sha": head, "nothing_to_commit": True, "fanned_out_at_head": True, **base}
    else:
        stems = [PurePosixPath(plan).stem for plan, *_ in resolved]
        message = f"warp: record roadmap {run_id} plans and fan out\n\nPlans: " + ", ".join(stems)
        try:
            outcome = commit_paths(
                worktree_root, paths, message,
                deleted_paths=record_gone, declared_reverts=record_gone,
                blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree_root),
            )
        except NothingToCommit:
            return {
                "committed": False, "sha": None, "nothing_to_commit": True,
                **({"withheld": withheld} if withheld else {}), **base,
                "fanout": {NOT_FIRED_KEY: not_fired + _plan_entries(spec["plans"], waves, "nothing-to-commit")},
            }
        except CommitRefused as err:
            failure = str(err)
            entries = not_fired + _plan_entries(spec["plans"], waves, "commit-not-landed", failure)
            return _error(failure, refused="commit-refused", fanout={NOT_FIRED_KEY: entries}, **base)
        sha = outcome.sha
        out = {
            "committed": True, "sha": sha, "paths": paths + record_gone,
            **({"withheld": withheld} if withheld else {}), **base,
        }

    edge_fields = [_baton_edge_fields(worktree_root, baton) for _plan, _sizing, baton, _writes in resolved]
    by_blocked_by = any(ids for ids, _blocked in edge_fields)
    items = []
    for given, (plan, sizing, _baton, writes), (ids, blocked_by) in zip(spec["plans"], resolved, edge_fields):
        item: dict = {"sizing": sizing, "label": PurePosixPath(plan).stem}
        if given in waves:
            item["wave"] = waves[given]
        if writes is not None:
            item["writes"] = writes
        if by_blocked_by:
            item[ITEM_IDS_KEY] = ids
            item[ITEM_BLOCKED_BY_KEY] = blocked_by
        items.append(item)
    fan_params: dict = {"sizings": items}
    if by_blocked_by:
        fan_params[EDGES_FROM_PARAM] = EDGES_FROM_BLOCKED_BY
    if inherited:
        fan_params[INTERACTION_MODE_PARAM] = inherited
    fan = ask_fanout.fanout(fan_params, worktree_root)
    not_fired += _unfired_siblings(fan, spec["plans"], waves, [i["sizing"] for i in items])
    out["fanout"] = {**fan, NOT_FIRED_KEY: not_fired}
    siblings = sibling_release(worktree_root, run_id, sha)
    if siblings is not None:
        out["siblings"] = siblings
    return out

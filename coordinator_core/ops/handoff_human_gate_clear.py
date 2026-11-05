"""
coordinator_core.ops.handoff_human_gate_clear -- the human-leg sibling of gate-recheck (DR-266).

Purpose: `handoff.human_gate_clear` discharges the `kind: human` legs of an awaiting_gate
handoff's `gate_evidence` on a validated PM citation (a recorded `baton.pm_turns` turn, or a
commit whose message carries the PM's verbatim words), stamps the citation into
`gate_cleared_by`, flips to ready_to_fire when no other leg is left open, and commits that one
file. Any refusal writes nothing and commits nothing.

Self-registration: importing this module fires @register_op("handoff.human_gate_clear").
"""

from __future__ import annotations

import re
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.frontmatter.primitives import (
    _locate_nested_block,
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
    _retire_gate_dependency,
)
from coordinator_core.frontmatter.schema_validate import format_validation_errors
from coordinator_core.git.commit import (
    CommitRefused,
    FilterUnsupported,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.git.git_state import read_commit
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.baton_pm_turns import read_turns
from coordinator_core.ops.ceremony.commit_message import compose_message
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.handoff_transition import (
    _PathNotContained,
    _apply_derived_readiness,
    _insert_fm_array_field,
    _now_iso,
    _replace_fm_array_field,
    _resolve_path,
    _retire_blocked_by,
    _retire_gate_evidence,
    _reresolve_gate_evidence_leg,
    _validate_fm,
    _write_gate_evidence_results,
)
from coordinator_core.reconcile.gate_eval import reduce_gate_evidence

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_CITATION_PREFIX = "pm-turn:"


def _fail(message: str) -> dict:
    return {"ok": False, "exit_code": 1, "applied": False, "error": message}


def _validate_citation(params: dict, worktree: Path) -> "tuple[Optional[str], Optional[str]]":
    """Return (citation, None) when the PM's act is on the record, else (None, reason).

    The citation proves the PM's words exist; it does not prove the PM meant this gate.
    """
    pm_turn = params.get("pm_turn")
    commit_sha = params.get("commit_sha")
    verbatim = params.get("verbatim")
    if (pm_turn is None) == (commit_sha is None):
        return None, "exactly one of 'pm_turn' or 'commit_sha' is required"

    if pm_turn is not None:
        if verbatim is not None:
            return None, "'verbatim' is only valid with 'commit_sha'"
        if not isinstance(pm_turn, dict):
            return None, "'pm_turn' must be an object {turn, session_id?}"
        turn = pm_turn.get("turn")
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            return None, (
                "pm_turn.turn must be an integer >= 0 (a negative turn is positional "
                "from the latest and would change meaning as turns accrue)"
            )
        session_id = pm_turn.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
            return None, "pm_turn.session_id must be a non-empty string when given"
        reply = read_turns((session_id or "").strip() or None, str(worktree), turn=turn)
        if not reply.get("ok"):
            return None, f"pm_turn refused: {reply.get('reason') or 'turn not recorded'}"
        return f"{_CITATION_PREFIX}{reply['session_id']}#{turn}", None

    if not isinstance(commit_sha, str) or not _FULL_SHA.match(commit_sha.strip()):
        return None, "commit_sha must be a full 40-hex commit sha"
    if not isinstance(verbatim, str) or not verbatim.strip():
        return None, "'verbatim' (the PM's words as the commit message carries them) is required with commit_sha"
    sha = commit_sha.strip().lower()
    commit = read_commit(worktree, sha)
    if commit is None:
        return None, f"commit_sha {sha} does not resolve to a commit"
    if verbatim not in commit.message:
        return None, f"commit {sha[:12]} message does not contain the given verbatim"
    return sha, None


def _gate_cleared_items(fm_dict: dict) -> List[str]:
    raw = fm_dict.get("gate_cleared_by")
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _stamp_gate_cleared_by(fm: str, items: List[str]) -> str:
    if _locate_nested_block(fm, "gate_cleared_by") is not None:
        return _replace_fm_array_field(fm, "gate_cleared_by", items)
    return _insert_fm_array_field(fm, "gate_cleared_by", items, "deployment_state")


def _clear_human_legs(
    fm: str, citation: str, leg_ids: Optional[List[str]], worktree: Path, at: str
) -> "tuple[str, str]":
    """Return (new_fm, deployment_state). Raises MutateAbort on any refusal."""
    fm_dict = yaml.safe_load(fm) or {}
    evidence = fm_dict.get("gate_evidence")
    legs = evidence.get("legs") if isinstance(evidence, dict) else None
    legs = [leg for leg in legs if isinstance(leg, dict)] if isinstance(legs, list) else []
    human = [leg for leg in legs if leg.get("kind") == "human"]
    if not human:
        raise MutateAbort("handoff carries no human gate_evidence leg")
    if leg_ids is not None:
        known = {leg.get("leg_id") for leg in human}
        unknown = [i for i in leg_ids if i not in known]
        if unknown:
            raise MutateAbort(f"leg_ids {unknown} are not human legs of this handoff")
        human = [leg for leg in human if leg.get("leg_id") in set(leg_ids)]

    cleared_ids = {leg.get("leg_id") for leg in human}
    results: List[Dict[str, Any]] = [
        {
            "leg_id": leg.get("leg_id"),
            "kind": "human",
            "status": "satisfied",
            "reason": f"human leg {leg.get('leg_id')!r} cleared by {citation}",
        }
        for leg in human
    ]
    remaining = [leg for leg in legs if leg.get("leg_id") not in cleared_ids]
    status = "freed"
    if remaining:
        status, _texts, remaining_results = reduce_gate_evidence(
            {"legs": [_reresolve_gate_evidence_leg(leg, at) for leg in remaining]}
        )
        results.extend(remaining_results)

    items = _gate_cleared_items(fm_dict)
    if citation not in items:
        items.append(citation)
    fm = _stamp_gate_cleared_by(fm, items)
    fm = _write_gate_evidence_results(fm, status, at, results)

    if status != "freed":
        return fm, "awaiting_gate"

    fm = replace_fm_field(fm, "deployment_state", "ready_to_fire")
    fm = _retire_gate_dependency(fm)
    fm = _retire_gate_evidence(fm)
    fm = _retire_blocked_by(fm, fm_dict)
    fm = _apply_derived_readiness(fm, worktree)
    return fm, "ready_to_fire"


def human_gate_clear_sync(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Core of 'handoff.human_gate_clear'.

    Params: handoff_path (required); exactly one of pm_turn {turn:int>=0, session_id?} or
    commit_sha (full 40-hex) with verbatim; optional leg_ids [str].
    Returns {ok, applied, gate_cleared_by, commit_sha, deployment_state} or an error envelope.
    """
    handoff_path = (params.get("handoff_path") or "").strip()
    if not handoff_path:
        return _fail("handoff.human_gate_clear: 'handoff_path' is required")
    if repo_root is None:
        return _fail("handoff.human_gate_clear: repo_root is required")
    leg_ids = params.get("leg_ids")
    if leg_ids is not None and not (
        isinstance(leg_ids, list) and leg_ids and all(isinstance(i, str) for i in leg_ids)
    ):
        return _fail("handoff.human_gate_clear: 'leg_ids' must be a non-empty list of strings")

    worktree = main_worktree_root(repo_root)
    try:
        path = _resolve_path(handoff_path, worktree)
    except _PathNotContained as exc:
        return _fail(f"handoff.human_gate_clear: {exc}")

    citation, refusal = _validate_citation(params, worktree)
    if citation is None:
        return _fail(f"handoff.human_gate_clear: {refusal}")

    state: dict = {"applied": False, "deployment_state": None, "items": [], "handoff_id": path.stem}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {handoff_path}")
        fm = split.fm_text
        deployment = read_fm_field(fm, "deployment_state")
        fm_dict = yaml.safe_load(fm) or {}
        items = _gate_cleared_items(fm_dict)
        state["items"] = items
        state["deployment_state"] = deployment
        if deployment == "ready_to_fire" and citation in items:
            return old_text
        if deployment != "awaiting_gate":
            raise MutateAbort(
                f'requires deployment_state:awaiting_gate (found "{deployment}") — {handoff_path}'
            )
        handoff_id = fm_dict.get("handoff_id")
        if isinstance(handoff_id, str) and handoff_id:
            state["handoff_id"] = handoff_id
        new_fm, new_state = _clear_human_legs(fm, citation, leg_ids, worktree, _now_iso())
        errors = _validate_fm(new_fm)
        if errors:
            raise MutateAbort(
                f"handoff frontmatter validation failed: {format_validation_errors(errors)}"
            )
        state["applied"] = True
        state["deployment_state"] = new_state
        state["items"] = _gate_cleared_items(yaml.safe_load(new_fm) or {})
        return rebuild(split, new_fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _fail(f"handoff.human_gate_clear: handoff not found: {handoff_path}")
    except LockTimeout as exc:
        return _fail(f"handoff.human_gate_clear: timed out waiting for file lock: {exc}")
    except MutateAbort as exc:
        return _fail(f"handoff.human_gate_clear: {exc.args[0] if exc.args else 'mutation aborted'}")

    reply: dict = {
        "ok": True,
        "exit_code": 0,
        "applied": state["applied"],
        "gate_cleared_by": state["items"],
        "commit_sha": None,
        "deployment_state": state["deployment_state"],
    }
    if not state["applied"]:
        return reply

    rel = path.relative_to(worktree.resolve()).as_posix()
    message = apply_missing_trailers(
        compose_message(subject=f"handoff: clear human gate {state['handoff_id']} ({citation})"),
        worktree,
        [rel],
    )
    try:
        outcome = commit_paths(
            worktree,
            [rel],
            message,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree),
        )
    except (CommitRefused, FilterUnsupported) as exc:
        return dict(
            reply,
            ok=False,
            exit_code=1,
            error=f"handoff.human_gate_clear: {rel} stamped but commit refused: {exc}",
        )
    reply["commit_sha"] = outcome.sha
    return reply


@register_op("handoff.human_gate_clear")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'handoff.human_gate_clear': `human_gate_clear_sync` on a worker thread."""
    import asyncio

    return await asyncio.to_thread(human_gate_clear_sync, params, repo_root)

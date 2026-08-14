"""
coordinator_core.ops.session_resolve_address — JSON-RPC "session.resolve_address".

Purpose: expose `coordinator_core.session.reachability.resolve_address` through
the op registry so EMs and dispatched agents alike reach the live UUID ->
`SendMessage`-address resolver the same way, consistent with the rest of the
engine's op-registry surface (handoff § 3's stated shape).

Self-registration: importing this module calls
register_op("session.resolve_address", _session_resolve_address) as a
side-effect. Add this module to `coordinator_core/ops/__init__.py`'s
`_EAGER_OP_MODULES` (and `coordinator_core/ops/_registry_map.py`'s
`OP_MODULE_MAP`) to trigger registration at dispatch time.

Spec backlink: `state/handoffs/2026-08-13-session-owner-reachability-registry.md` § 1

Negative-spec:
    - Never falls back to a guessed address on any failure path -- an
      exception from `resolve_address` is NOT caught here; a raise is a
      louder, more honest failure than a silently-wrong address, and
      `reachability.resolve_address` itself never raises to its own caller
      per its docstring (it degrades to `not_reachable` internally).
    - No `session_id` param aliasing/normalization beyond what
      `reachability._matching_session_ids` already does (exact match
      only) -- this module is a thin JSON-RPC veneer only.
    - An `ambiguous` outcome's `candidates[i]["address"]` is `None` for a
      matching id that itself lacks a usable name/socket, never a raw
      session id -- inherited unchanged from `reachability.Candidate`
      (Review: code-reviewer -- P3, "confidently wrong address" shape the
      spec's Anti-scope forbids).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ipc import register_op
from coordinator_core.session import reachability


def _candidate_to_dict(candidate: "reachability.Candidate") -> Dict[str, Any]:
    return {
        "session_id": candidate.session_id,
        "name": candidate.name,
        "ref": candidate.ref,
        "address": candidate.address,
    }


@register_op("session.resolve_address")
def _session_resolve_address(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session.resolve_address" handler.

    Params:
        session_id (str, required) -- a full session UUID, in any of the
            recording conventions already in the tree (`claimed_by`,
            `authoring_session`, `created_by_session`, an `agent_sessions`
            entry, or a `state/subagent-share/<id>/` directory name) -- all
            of them carry a bare UUID string, so no per-convention
            unwrapping is needed here. A short prefix is no longer
            accepted; it now resolves to `not_reachable` like any other
            unmatched string (see `reachability._matching_session_ids`).

    Returns:
        {"outcome": "own_session"|"reachable"|"not_reachable"|"ambiguous",
         "session_id": str|None, "address": str|None,
         "candidates": [{"session_id","name","ref","address"}, ...]}

    A missing/empty `session_id` param yields `{"outcome": "not_reachable", ...}`
    rather than raising -- this op sits on advisory read paths (e.g. the
    `baton-assemble` collision warning) that must degrade, never block.
    """
    session_id = params.get("session_id") if isinstance(params, dict) else None
    if not isinstance(session_id, str):
        session_id = ""

    result = reachability.resolve_address(session_id)

    return {
        "outcome": result.outcome,
        "session_id": result.session_id,
        "address": result.address,
        "candidates": [_candidate_to_dict(c) for c in result.candidates],
    }

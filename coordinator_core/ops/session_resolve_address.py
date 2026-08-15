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
    - `reason` is passed through from `reachability.ResolveResult`
      unmodified and is never synthesized here: no default string, no
      `getattr(result, "reason", ...)` tolerance, no re-derivation from
      `outcome`. A `reason` missing from the resolver is an AttributeError
      at this boundary by design -- a fabricated "unknown" would report a
      cause this module cannot know.
    - An `ambiguous` outcome's `candidates[i]["address"]` is `None` for a
      matching id that itself lacks a usable name/socket, never a raw
      session id -- inherited unchanged from `reachability.Candidate`
      (Review: code-reviewer -- P3, "confidently wrong address" shape the
      spec's Anti-scope forbids).
    - `caller_messaging_gate` describes the CALLING session and never the
      resolved target -- it is named for its subject for that reason. It is
      not merged into `outcome`/`reason`, and `reachability.messaging_
      available()` is not consulted to build it: the two answer different
      questions (box-level deliverability vs. this session's own gate
      request), and folding either into the other is the collapse
      `coordinator_core.session.messaging_gate` exists to undo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ipc import register_op
from coordinator_core.session import messaging_gate, reachability


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
         "session_id": str|None, "address": str|None, "reason": str|None,
         "candidates": [{"session_id","name","ref","address"}, ...],
         "caller_messaging_gate": {"state","requested","inbox_bound","note"}}

    `caller_messaging_gate` (`coordinator_core.session.messaging_gate`) is
    emitted on EVERY outcome, and is about the CALLING session, not the
    resolved target. It exists because a `not_reachable` /
    `peer-messaging-unavailable` pair reads identically whether nothing ever
    asked the harness to open its cross-session inbox or whether this session
    asked and the inbox did not open -- the second is a claude-klabauter defect
    (`state/bug-backlog/2026-08-15-the-messaging-gate-default-ships-and-no-
    session-binds.yaml`) and the first is not, and three repos read the
    collapsed reading as "the remote GrowthBook flag is still off". A reader
    deciding whether to plan around a human relay branches on
    `state == "requested-unbound"`; the other three states say the channel was
    never asked for, was declined, or is open.

    `reason` carries `reachability.ResolveResult.reason` verbatim: one of
    `NotReachableReason`'s five constants on the "not_reachable" arm,
    `None` on every other outcome. Always PRESENT in the dict, `None`-
    valued when unset, exactly like `session_id`/`address` -- a consumer
    switching on `outcome` alone is unaffected, and one that wants to say
    WHY a live-but-unaddressable peer differs from a nonexistent session
    reads it here instead of re-deriving it from a second registry read
    it does not have.

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
        "reason": result.reason,
        "candidates": [_candidate_to_dict(c) for c in result.candidates],
        "caller_messaging_gate": messaging_gate.to_dict(messaging_gate.classify()),
    }

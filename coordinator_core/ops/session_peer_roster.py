"""
coordinator_core.ops.session_peer_roster — JSON-RPC "session.peer_roster".

Purpose: expose `coordinator_core.session.peer_roster.build_roster` through
the op registry, mirroring `coordinator_core.ops.session_resolve_address`'s
own registration convention exactly — EMs and dispatched agents alike reach
the live peer-roster read the same way they reach the resolver.

Self-registration: importing this module calls
register_op("session.peer_roster", _session_peer_roster) as a side-effect.
Registered in `coordinator_core/ops/__init__.py`'s `_EAGER_OP_MODULES`,
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`, and
`coordinator_core/op_scopes.py` (scope "none", same resolution story as
`session.resolve_address` — the harness peer registry is machine-global, not
per-worktree, and is never keyed off `repo_root`).

Spec backlink: `state/handoffs/2026-08-13-live-peer-roster.md` §§ 1-2, 4.

Negative-spec:
    - Never falls back to a guessed/unfiltered roster on any failure path --
      `peer_roster.build_roster` already degrades to an empty list on any
      internal failure per its own docstring; this veneer does not add a
      second try/except around it.
    - `repo_root` param, when present, is used VERBATIM as the filter root
      -- no path resolution, no git-root walk, no existence check. A
      missing/non-string `repo_root` param falls back to the CALLING
      process's own `os.getcwd()` (the current-repo default), never an
      unfiltered dump.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ipc import register_op
from coordinator_core.session import peer_roster


def _row_to_dict(row: "peer_roster.PeerRow") -> Dict[str, Any]:
    return {
        "session_id": row.session_id,
        "address": row.address,
        "name": row.name,
        "ref": row.ref,
        "cwd": row.cwd,
        "status": row.status,
        "running_seconds": row.running_seconds,
        "is_self": row.is_self,
        "self_determination": row.self_determination,
        "messaging_available": row.messaging_available,
    }


@register_op("session.peer_roster")
def _session_peer_roster(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session.peer_roster" handler.

    Params:
        repo_root (str, optional) -- filter the roster to sessions whose
            `cwd` is this path or a subdirectory of it. Defaults to the
            CALLING process's own `os.getcwd()` when omitted (the
            "current repo" view) -- pass a sibling repo's absolute path for
            the "who is in repo X" view (e.g. the cross-repo memo case).

    Returns:
        {"rows": [{"session_id", "address", "name", "ref", "cwd", "status",
                    "running_seconds", "is_self", "self_determination",
                    "messaging_available"}, ...]}

    `self_determination` and `messaging_available` are carried through
    verbatim from `peer_roster.PeerRow`, matching what
    `coordinator/bin/session-reachability-cli.py`'s own row serializer
    already emits for the first of them. Both are per-row by that
    dataclass's contract, not this veneer's choice; `messaging_available`
    is what tells a caller that an all-`address: null` roster is an
    unbound harness inbox rather than a set of stale or unresolvable
    records. A row dict is the whole answer for a consumer holding only
    the rows, so neither field is dropped here.

    Scope "none" (op_scopes.py): this op's own `repo_root` engine kwarg is
    never resolved/injected for a "none"-scoped op -- the `repo_root` WIRE
    PARAM above (read from `params`, not this function's kwarg) is the only
    way a caller narrows the filter target. The unused kwarg itself mirrors
    `session_resolve_address.py`'s `_session_resolve_address` signature
    exactly (same dead `repo_root: Optional[Path] = None` param) -- kept for
    registration-shape consistency across the two ops, not vestigial
    copy-paste (Review: code-reviewer -- P3, confirmed against the sibling).
    """
    override = params.get("repo_root") if isinstance(params, dict) else None
    target_root = override if isinstance(override, str) and override else None

    rows = peer_roster.build_roster(target_root)

    return {"rows": [_row_to_dict(row) for row in rows]}

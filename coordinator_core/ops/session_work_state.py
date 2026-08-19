"""
coordinator_core.ops.session_work_state — JSON-RPC "session.work_state".

Purpose: expose `coordinator_core.session.work_state.build_work_state`
through the op registry, mirroring `coordinator_core.ops.session_peer_
roster`'s own registration convention exactly — EMs and dispatched agents
alike reach the corpus-keyed held/unclaimed read the same way they reach
the live peer roster.

Self-registration: importing this module calls
register_op("session.work_state", _session_work_state) as a side-effect.
Registered in `coordinator_core/ops/__init__.py`'s `_EAGER_OP_MODULES`,
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`,
`coordinator_core/op_scopes.py` (scope "common_dir" -- unlike
`session.peer_roster`'s "none", this op reads `state/handoffs/`, which is
main-worktree-rooted repo state, exactly the case `op_scopes.py`'s own
comment block names for "common_dir"), and
`coordinator_core/authz/classification.py` (OpClass.COMPUTE_ONLY).

Spec backlink: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md,
chunk C3.

Negative-spec:
    - Never re-derives readiness or re-implements any of `build_work_state`'s
      own held/unclaimed logic -- this veneer's only job is to unwrap the
      engine-injected `repo_root` and hand it to `build_work_state`
      unchanged, then return its result verbatim.
    - Takes `repo_root` as the engine kwarg (scope "common_dir" resolves and
      injects it) -- does NOT invent a wire param of the same name for
      callers to pass explicitly, unlike `session.peer_roster`'s deliberate
      wire-level `repo_root` filter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.session.work_state import build_work_state


@register_op("session.work_state")
def _session_work_state(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session.work_state" handler.

    Params: none consumed -- `repo_root` arrives ONLY as the engine-injected
        kwarg below (scope "common_dir"), never read off `params`.

    Returns:
        {"held": [...], "unclaimed": [...], "review_due": [...]} -- verbatim
        `build_work_state(repo_root)` output; see that function's own
        docstring for the full row-shape and four-bucket readiness contract.
    """
    return build_work_state(repo_root)

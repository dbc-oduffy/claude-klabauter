"""coordinator_core.hooks.nudge_cross_repo_cwd_boundary — CwdChanged
warm-engine op.

Port of: coordinator-content-repo coordinator/hooks/scripts/nudge-cross-repo-cwd-boundary.py.
Fires when a session's cwd crosses into or out of the claude-klabauter engine sibling
repo, flagging that no standing cross-repo commit grant survives the
crossing. Advisory only — never blocks/denies, always returns a computed
`additionalContext` envelope or a no-op.

UNRESOLVED SIBLING IS A NO-OP (negative spec, preserved from the DoE port):
when the claude-klabauter root cannot be resolved, this emits nothing rather than
falling back to a substring match. No module-level mutable state; the root
is re-resolved fresh on every call.
"""

from __future__ import annotations

import os

from coordinator_core._hook_envelope import no_advisory, payload_of
from coordinator_core.engine_root import coordinator_engine_root_with_class
from coordinator_core.hooks._envelope import context_only
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op

_REMEDIATION = (
    "[cross-repo boundary] crossed into/out of the claude-klabauter sibling repo -- no "
    "standing commit grant survives the crossing; a cross-repo commit needs "
    "per-session PM assent. When held: scoped commits only, never `-A`/`.`/"
    "`-a`."
)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _crosses_boundary(old_cwd: str, new_cwd: str, sibling_root: str) -> bool:
    root_norm = _norm(sibling_root)

    def _inside(candidate: str) -> bool:
        norm = _norm(candidate)
        return norm == root_norm or norm.startswith(root_norm + os.sep)

    return _inside(new_cwd) != _inside(old_cwd)


@register_op("hooks.nudge_cross_repo_cwd_boundary")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    old_cwd = field(params, "old_cwd")
    new_cwd = field(params, "new_cwd")
    if not old_cwd or not new_cwd:
        return no_advisory()

    try:
        sibling_root, _resolution_class = coordinator_engine_root_with_class()
    except Exception:
        sibling_root = None
    if not sibling_root:
        # UNRESOLVED SIBLING IS A NO-OP -- never a substring fallback.
        return no_advisory()

    try:
        crosses = _crosses_boundary(old_cwd, new_cwd, sibling_root)
    except Exception:
        return no_advisory()
    if not crosses:
        return no_advisory()

    return context_only("CwdChanged", _REMEDIATION)

"""coordinator_core.ops.landing_guard_selection -- op "landing.guard_selection": the test nodes a
landing owes for a path list, for hand-lands that bypass the land gate.

Returns the same node list the land gate's ``landing_invariant_nodes`` selects (repo-wide invariants
for the paths plus every ``always_run_guards`` entry), the declared guards alone, and the retest
command naming those nodes. COMPUTE_ONLY: it never runs pytest and spawns nothing; the caller runs
the returned ``retest_command``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op

OP_KEY = "landing.guard_selection"

_SCHEMA = {
    "type": "object",
    "properties": {
        "repo_root": {"type": "string",
                      "description": "Repo whose coordinator.local.md declares the guards; default the caller's worktree."},
        "paths": {"type": "array", "items": {"type": "string"},
                  "description": "Repo-relative paths the landing touches."},
    },
    "required": ["paths"],
    "additionalProperties": False,
}


@register_op(OP_KEY, params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "landing.guard_selection": params `paths`, `repo_root` (optional).

    Returns {"nodes", "guards", "retest_command"}; nodes equals
    ``landing_invariant_nodes(root, paths, None)``.
    """
    from coordinator_core.ops.dispatch_emit import always_run_guards
    from coordinator_core.ops.dispatch_emit.landing_integrity import landing_invariant_nodes
    from coordinator_core.ops.dispatch_emit.landing_targeted_tests import retest_remedy

    paths = params.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise CallerFacingValidationError("landing.guard_selection: paths must be a list of strings")
    raw = params.get("repo_root") or repo_root
    if not raw:
        raise CallerFacingValidationError("landing.guard_selection: no repo root")
    root = Path(raw)
    nodes = landing_invariant_nodes(root, paths, None)
    return {
        "nodes": nodes,
        "guards": always_run_guards.declared_guards(root),
        "retest_command": retest_remedy(None, nodes),
    }

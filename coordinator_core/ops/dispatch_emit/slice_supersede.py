"""``dispatch.supersede_slice``: marks a held sibling slice as superseded by a named run.

Params: ``{run_id, by}``. A superseded slice is never released by a sibling close and its fire is denied
naming ``by``. The mark is a write-once scratch file (``sibling_hold.mark_superseded``); the same ``by``
again is a no-op. Spawns nothing.
Reply: ``{ok, run_id, superseded_by, written}`` or ``{ok: false, error}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.supersede_slice"


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.supersede_slice": one status read, one marker write."""
    from coordinator_core.ops.dispatch_emit.sibling_hold import SupersedeRefused, mark_superseded

    if repo_root is None:
        return {"ok": False, "error": f"{OP_NAME} requires repo_root"}
    params = params if isinstance(params, dict) else {}
    run_id, by = params.get("run_id"), params.get("by")
    if not isinstance(run_id, str) or not isinstance(by, str) or not run_id or not by:
        return {"ok": False, "error": f"{OP_NAME} requires string params run_id and by"}
    try:
        result = mark_superseded(main_worktree_root(Path(repo_root)), run_id, by)
    except SupersedeRefused as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}

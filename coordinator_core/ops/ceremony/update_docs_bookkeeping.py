"""ceremony.update_docs_bookkeeping: the /update-docs doc inventory and index regen in one call.

Composes `ceremony.update_docs_worklist` and `docindex.emit` in process (no second
dispatch hop) and returns both replies. Judgment items stay in
`inventory.agent_worklist` for the agent; this op decides nothing.

Params: `ack` (advance the inventory baseline to HEAD), `write` (rewrite every index
whose comparison is ordinary drift), `rebaseline` (repair a hand-edited index region
from source). A default call reports and writes no index.

Returns {inventory, index_regen}: the bare replies of the two composed ops.

Self-registration: importing this module registers the op.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.ceremony.update_docs_worklist import _ceremony_update_docs_worklist
from coordinator_core.ops.docindex_emit import _docindex_emit


@register_op("ceremony.update_docs_bookkeeping")
def _ceremony_update_docs_bookkeeping(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.update_docs_bookkeeping" handler.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating
    worktree; both composed ops act on the main worktree.
    """
    if repo_root is None:
        raise ValueError(
            "ceremony.update_docs_bookkeeping requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None."
        )
    p = params or {}
    inventory = _ceremony_update_docs_worklist({"ack": bool(p.get("ack", False))}, repo_root=repo_root)
    index_regen = _docindex_emit(
        {
            "target_root": str(main_worktree_root(repo_root)),
            "write": bool(p.get("write", False)),
            "rebaseline": bool(p.get("rebaseline", False)),
        },
        repo_root=repo_root,
    )
    return {"inventory": inventory, "index_regen": index_regen}

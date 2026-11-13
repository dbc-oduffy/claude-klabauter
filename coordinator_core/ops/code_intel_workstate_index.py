"""coordinator_core.ops.code_intel_workstate_index -- "code_intel.workstate_index" JSON-RPC op.

Purpose: thin shell over ``symbol_knowledge.contract.workstate_index``: map symbols and files to the
plans, DRs, lessons and logs that mention them, in one ``state/symbol-knowledge/`` file.

Negative-spec:
  - Writes exactly one state file; never writes into example-retrieval-repo's store.
  - Holds no logic; the body lives in ``symbol_knowledge.workstate_index``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

__all__ = ["code_intel_workstate_index"]


@register_op("code_intel.workstate_index")
def code_intel_workstate_index(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "code_intel.workstate_index" handler. Takes no params."""
    from coordinator_core.symbol_knowledge import contract

    return contract.workstate_index(repo_root if repo_root is not None else Path.cwd())

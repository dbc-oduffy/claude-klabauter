"""coordinator_core.ops.code_intel_freshness -- "code_intel.freshness" JSON-RPC op.

Purpose: thin shell over ``symbol_knowledge.contract.freshness``: re-extract only the files a commit
changed and queue tier-1 descriptions for changed symbols with empty docstrings.

Negative-spec:
  - Makes no model call and writes nothing: COMPUTE_ONLY.
  - Holds no logic; the body lives in ``symbol_knowledge.freshness``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

__all__ = ["code_intel_freshness"]


@register_op("code_intel.freshness")
def code_intel_freshness(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "code_intel.freshness" handler. Params: ``sha`` (default ``"HEAD"``)."""
    from coordinator_core.symbol_knowledge import contract

    root = repo_root if repo_root is not None else Path.cwd()
    return contract.freshness(root, str((params or {}).get("sha") or "HEAD"))

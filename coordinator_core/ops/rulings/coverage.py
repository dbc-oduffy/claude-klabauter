"""
coordinator_core.ops.rulings.coverage — JSON-RPC "rulings.coverage".

Purpose: report which accepted PM rulings have an enforcement artifact, which are
prose-only, and which name an enforcement path that is not in the tree.

Wire params: none.

Reply: ``{"covered": [{id, enforcement}], "prose_only": [id], "dangling": [{id, enforcement}]}``.
  - covered: accepted, enforcement path exists.
  - prose_only: accepted, enforcement null (or blank).
  - dangling: accepted, enforcement path absent from the worktree.

Negative-spec:
  - Does NOT list superseded rulings in any bucket.
  - Does NOT spawn a subprocess; existence is ``Path.exists`` on the main worktree root.
  - Does NOT write anything, and does NOT judge whether the enforcement path actually
    enforces the ruling; it checks presence only.
  - Does NOT raise on a malformed register file; the file is skipped.
"""

from __future__ import annotations

MUTATES: list = []
GENERATES: list = []

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import STATUS_ACCEPTED, CoverageReply

RULINGS_COVERAGE_OP = "rulings.coverage"

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _in_tree(rel: str) -> bool:
    """A repo-relative path that cannot leave the worktree (absolute or `..` counts as dangling)."""
    p = Path(rel)
    return not p.is_absolute() and ".." not in p.parts


def coverage(worktree_root: Path) -> CoverageReply:
    """Bucket every accepted ruling under `worktree_root` by its enforcement state."""
    covered: list = []
    prose_only: list = []
    dangling: list = []
    for r in store.load(worktree_root):
        if r.status != STATUS_ACCEPTED:
            continue
        enf = (r.enforcement or "").strip()
        if not enf:
            prose_only.append(r.id)
        elif _in_tree(enf) and (Path(worktree_root) / enf).exists():
            covered.append({"id": r.id, "enforcement": enf})
        else:
            dangling.append({"id": r.id, "enforcement": enf})
    return {"covered": covered, "prose_only": prose_only, "dangling": dangling}


@register_op(RULINGS_COVERAGE_OP, params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "rulings.coverage" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("rulings.coverage requires a resolved repo_root")
    return dict(coverage(Path(main_worktree_root(repo_root))))

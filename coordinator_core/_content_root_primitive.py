"""coordinator_core._content_root_primitive — the coordinator CONTENT-root
join, as a LEAF module.

is a PURE function over the filesystem — it touches no other `coordinator_core`
module — yet before this move it lived in `coordinator_core/data_root.py`,
which DOES import elsewhere in the package (`coordinator_core.ops.
coordinator_doe_root`, which itself imports `coordinator_core.
resolve_coordinator_clone`). A module that needs the join at module level but
sits anywhere on that import edge (`resolve_coordinator_clone.py`,
`coordinator_core/ops/coordinator_doe_root.py`) cannot import it from
`data_root` without completing a cycle — which is exactly why those two
modules had grown their own hand-expanded copies of the join instead
(findings 4 and 8). Moving the join itself into a module with NO
`coordinator_core` imports of its own removes the constraint that was
generating the copies: any module in the package can import from here at
module level, cycle-free, by construction.

`coordinator_core/data_root.py` re-exports both names unchanged, so every
existing `from coordinator_core.data_root import content_root_for` keeps
working.

Sibling: `coordinator/bin/lib/coordinator_data_root.py` (bin/-side twin; the
two `content_root_for` + `FLAT_CONTENT_ROOT_MARKER` + `content_root_or_private`
trios MUST stay behaviourally identical — same two-candidate order, same
marker, same fallback — AC4). The bin/ twin is not split into a further leaf
module: it has no analogous intra-tree import-cycle constraint of its own
(bin/ CLIs cannot import `coordinator_core` at all, so there is nothing to
cycle against), so splitting it further would be a leaf module for its own
sake rather than one earned by a real constraint.

WHY A SHARED PRIMITIVE AND NOT ANOTHER INLINE JOIN (see `content_root_for`
below): the identical `Path(doe_root) / "coordinator" / ...` hardcode was
fixed pointwise in `data_root()` (see the F2 note there) and left standing at
~45 other call sites, each resolving correctly against a private tree and
producing a path that cannot exist against a published mirror — which is how
a cloud container reached "coordinator will NOT load in any interactive
session" with all of its content plainly on disk.
"""
from __future__ import annotations

import os
from pathlib import Path

FLAT_CONTENT_ROOT_MARKER = (".claude-plugin", "plugin.json")


def content_root_for(doe_root) -> Path | None:
    if not doe_root:
        return None
    if isinstance(doe_root, Path):
        base = doe_root
    else:
        raw = str(doe_root)
        base = Path(raw.rstrip("/\\") or raw)
    private = base / "coordinator"
    if private.is_dir():
        return private
    if base.joinpath(*FLAT_CONTENT_ROOT_MARKER).is_file():
        return base
    return None


def content_root_or_private(doe_root) -> str:
    content = content_root_for(doe_root)
    if content is not None:
        return str(content)
    return os.path.join(str(doe_root), "coordinator")

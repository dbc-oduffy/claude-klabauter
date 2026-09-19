"""coordinator_core._content_root_primitive — the coordinator CONTENT-root
join, as a LEAF module.

Review: overengineering-reviewer Q1 (the enabling refactor). `content_root_for`
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

#: The marker that makes a FLAT directory a coordinator content root. A flat
#: clone without its own plugin manifest is not one, and must keep failing —
#: the same gate `resolve_coordinator_clone` uses for its flat-layout rung, not
#: a second spelling of the concept.
FLAT_CONTENT_ROOT_MARKER = (".claude-plugin", "plugin.json")


def content_root_for(doe_root) -> Path | None:
    """The coordinator CONTENT root inside a resolved DoE root, either layout.

    THE ONE PLACE THIS JOIN BELONGS. Two live layouts hold coordinator content,
    and a caller that knows only one is broken on the other:

      <doe_root>/coordinator/     the private authoring tree
      <doe_root>/ (flat)          the published mirror

    Returns the content root, or None when `doe_root` is empty or holds neither
    layout. Never raises and never returns a path that does not exist — so a
    predicate call site reads `content_root_for(root) is not None`, and a path
    call site joins onto the result after a None check.

    Private layout is probed FIRST, so a private-tree root resolves exactly as
    it always did — this widens nothing for an existing caller.

    Must stay behaviourally identical to its twin in the other tree — same
    two-candidate order, same marker — for the same reason `data_root()`
    carries that constraint (AC4).
    """
    if not doe_root:
        return None
    if isinstance(doe_root, Path):
        base = doe_root
    else:
        # Review: code-reviewer F1 -- rstrip("/\\") alone collapses "/" or
        # "//" to "", and Path("") resolves to the process cwd, silently
        # probing cwd instead of failing closed on a degenerate root. Fall
        # back to the un-stripped string when stripping empties it, so an
        # all-slash root stays anchored at the filesystem root (where the
        # marker/private-dir checks below correctly find nothing).
        raw = str(doe_root)
        base = Path(raw.rstrip("/\\") or raw)
    private = base / "coordinator"
    if private.is_dir():
        return private
    if base.joinpath(*FLAT_CONTENT_ROOT_MARKER).is_file():
        return base
    return None


def content_root_or_private(doe_root) -> str:
    """`content_root_for`, falling back to the private-shape join.

    Review: overengineering-reviewer finding 2 — the shape ~10 call sites
    across `coordinator/bin/` actually needed (`content_root_for`, falling
    back to `<doe_root>/coordinator` when neither layout is present) was
    written once, module-locally, as `_shared.py::_content_root_or_private`,
    then hand-re-derived at each of those ~10 sites with a copy-pasted
    rationale comment. Promoted here as the one public spelling both twins
    and every call site route through.

    The fallback is a REAL requirement, not a redundant branch: it preserves
    each caller's own "candidate does not exist on disk" diagnostic, which
    would otherwise regress to a bare `None` with no path to name. Only the
    per-site hand-expansion was the defect — the fallback itself stays.
    """
    content = content_root_for(doe_root)
    if content is not None:
        return str(content)
    return os.path.join(str(doe_root), "coordinator")

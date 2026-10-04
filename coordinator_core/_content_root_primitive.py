"""coordinator_core._content_root_primitive — the coordinator CONTENT-root
join, as a LEAF module.

is a PURE function over the filesystem — it touches no other `coordinator_core`
module — yet before this move it lived in `coordinator_core/data_root.py`,
which DOES import elsewhere in the package. A module that needs the join at
module level but sits anywhere on that import edge (`resolve_coordinator_clone.py`)
cannot import it from `data_root` without completing a cycle — which is
exactly why such modules had grown their own hand-expanded copies of the join
instead (findings 4 and 8). Moving the join itself into a module with NO
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
below): the identical `Path(repo_root) / "coordinator" / ...` hardcode was
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


def content_root_for(repo_root) -> Path | None:
    if not repo_root:
        return None
    if isinstance(repo_root, Path):
        base = repo_root
    else:
        raw = str(repo_root)
        base = Path(raw.rstrip("/\\") or raw)
    private = base / "coordinator"
    if private.is_dir():
        return private
    if base.joinpath(*FLAT_CONTENT_ROOT_MARKER).is_file():
        return base
    return None


def content_root_or_private(repo_root) -> str:
    content = content_root_for(repo_root)
    if content is not None:
        return str(content)
    return os.path.join(str(repo_root), "coordinator")


def repo_root_from_plugin_root_candidate(
    candidate: str,
    *,
    drive_root_guard: str = "preserve",
    basename_compare: str = "normcase",
    manifest_relpath_fallback: bool = True,
    allow_unchanged_fallback: bool = True,
) -> str:
    """Normalize a CLAUDE_PLUGIN_ROOT-shaped value to the coordinator REPO root.

    CLAUDE_PLUGIN_ROOT is a *content* root: in the private/dev layout it is
    `<repo_root>/coordinator`, one level below the repo root; in the OSS flat
    layout the two coincide. Disambiguated by the `.claude-plugin/plugin.json`
    marketplace marker: directly under `candidate` means candidate IS the repo
    root; beside a `coordinator` basename means the parent is. Otherwise
    `candidate` comes back unchanged, or "" when `allow_unchanged_fallback` is
    False. Callers still gate on `os.path.isdir()` / manifest presence.

    The three bin-side copies each picked up a different review fix, so each
    divergence is an explicit parameter and none is resolved here:
      drive_root_guard: "preserve" restores the raw candidate when stripping the
        trailing separator would truncate a bare Windows drive root; "normpath"
        routes through `os.path.normpath` first and does not protect that case.
      basename_compare: "normcase" is case-insensitive on Windows only;
        "casefold" is case-insensitive on every platform.
      manifest_relpath_fallback: also accept a parent holding
        `coordinator/schemas/coordinator-registry.manifest.json` (no marker).
      allow_unchanged_fallback: False for call sites with no downstream
        manifest gate, so an unrecognizable candidate cannot win over an
        operator's explicit override just because it is a directory.
    """
    if drive_root_guard == "preserve":
        raw = candidate
        stripped = raw.rstrip("/\\")
        if len(stripped) == 2 and stripped[1] == ":" and raw != stripped:
            stripped = raw
    elif drive_root_guard == "normpath":
        stripped = os.path.normpath(candidate).rstrip("/\\")
    else:
        raise ValueError(f"unknown drive_root_guard: {drive_root_guard!r}")

    if os.path.isfile(os.path.join(stripped, *FLAT_CONTENT_ROOT_MARKER)):
        return stripped
    parent = os.path.dirname(stripped)
    basename = os.path.basename(stripped)
    if basename_compare == "normcase":
        basename_matches_coordinator = os.path.normcase(basename) == os.path.normcase("coordinator")
    elif basename_compare == "casefold":
        basename_matches_coordinator = basename.casefold() == "coordinator"
    else:
        raise ValueError(f"unknown basename_compare: {basename_compare!r}")
    if basename_matches_coordinator and os.path.isfile(os.path.join(parent, *FLAT_CONTENT_ROOT_MARKER)):
        return parent
    if manifest_relpath_fallback and basename_matches_coordinator and os.path.isfile(
        os.path.join(parent, "coordinator", "schemas", "coordinator-registry.manifest.json")
    ):
        return parent
    return candidate if allow_unchanged_fallback else ""

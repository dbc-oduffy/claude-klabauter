"""
coordinator_core.cartography.file_index — path -> system mapping via path rules.

Purpose: deterministic replacement for the file->system reconstruction the
architecture-survey skill was paying Haiku agents to rebuild by reading
(docs/problems/2026-07-12-architecture-survey-dogfood-observations.md § 5). The
proven primitive from the 2026-07-12-11h57 dogfood run: `git ls-files` + a
path-rule table, precomputed and handed to the synthesizer verbatim — **149
files, 0 unmapped** in that run.

Rule shape: a repo-relative path is mapped to a system name by its top-level
tracked-directory component (`state/`, `coordinator_core/`, `docs/`, `archive/`,
`cross-repo/`, `bin/`, `tasks/`, `skills/`, ...); a tracked file with no `/` in its
repo-relative path (a root-level file) maps to the reserved ``"repo-root"``
bucket rather than being left unmapped. This mirrors the coarse-grained,
directory-shaped taxonomy already in use by docs/architecture/systems-index.md,
and is deliberately coarser than that atlas's curated sub-splits (e.g.
`coordinator_core/ops/` decomposes further into emit-engine/worklife-ops/etc in
the hand-curated atlas) — this primitive is the RAG-absent-fallback substrate
layer the atlas's Opus synthesis judgment then refines, not a replacement for
that judgment (§ Problem-2 "extraction vs. judgment" framing).

Spec backlink: pln-claude-klabauter-cartography-substrate-a-26eb2e § C2

Negative-spec:
  - Does NOT compute per-system file counts or cross-system connections — this
    module returns the path->system mapping only; aggregation is a `GROUP BY`
    a consumer performs over the returned mapping (Problem-2 § 6.5).
  - Does NOT special-case dotfiles/dotdirs — a tracked path starting with a dot
    directory (e.g. `.claude-plugin/foo.json`) maps by that directory name like
    any other top-level component; it is a real tracked path, not noise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.cartography.tree import list_tracked_files

#: Reserved system name for a tracked file with no directory component
#: (a root-level file, e.g. "README.md").
REPO_ROOT_SYSTEM = "repo-root"


def system_for_path(relpath: str) -> str:
    """Return the system name for a single repo-relative path.

    Rule: the first path-separator-delimited component of `relpath` (its
    top-level tracked directory). A path with no separator (a root-level
    file) maps to REPO_ROOT_SYSTEM. Every well-formed repo-relative path
    (as emitted by `git ls-files`) maps to exactly one system — there is no
    "unmapped" outcome for this rule.
    """
    normalized = relpath.replace("\\", "/")
    parts = normalized.split("/", 1)
    if len(parts) == 1:
        return REPO_ROOT_SYSTEM
    return parts[0]


def build_file_index(target_root: str | Path, scope: Optional[Sequence[str]] = None) -> dict:
    """Build the path -> system index for every tracked file under `target_root`.

    Args:
        target_root: root of the tracked-file tree (must be inside a git worktree).
        scope: optional sequence of git pathspecs narrowing the walk to a
            subset of the tracked-file tree, applied at
            `cartography.tree.list_tracked_files` enumeration time — same
            param shape as `cartography.tree.build_tree`'s `scope`, so a
            caller narrows both ops identically. Absent/empty, output is
            byte-identical to the pre-scope-param behaviour (the full
            tracked-file set).

    Returns:
        {
            "target_root": str,
            "index": {"<repo-relative path>": "<system>", ...},
            "systems": {"<system>": <file_count>, ...},
            "file_count": int,
            "unmapped_count": int,   # always 0 — every tracked path maps by rule
        }
    """
    tracked = list_tracked_files(target_root, scope=scope)

    index = {}
    systems: dict = {}
    unmapped_count = 0
    for relpath in tracked:
        system = system_for_path(relpath)
        if not system:
            unmapped_count += 1
            continue
        index[relpath] = system
        systems[system] = systems.get(system, 0) + 1

    return {
        "target_root": str(Path(target_root).resolve()),
        "index": index,
        "systems": systems,
        "file_count": len(tracked),
        "unmapped_count": unmapped_count,
    }

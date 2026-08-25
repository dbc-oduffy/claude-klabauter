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

Spec backlink: pln-makima-cartography-substrate-a-26eb2e § C2

Negative-spec:
  - Does NOT compute per-system file counts or cross-system connections — this
    module returns the path->system mapping only; aggregation is a `GROUP BY`
    a consumer performs over the returned mapping (Problem-2 § 6.5).
  - Does NOT special-case dotfiles/dotdirs — a tracked path starting with a dot
    directory (e.g. `.claude-plugin/foo.json`) maps by that directory name like
    any other top-level component; it is a real tracked path, not noise.

Untracked-corpus arm (`include_untracked`), added for a fresh input corpus
that has not been committed yet — a same-session scan target frequently has
files that are staged or entirely new, and a silently-short inventory over
just `git ls-files` is the worst failure shape for an indexer whose whole
job is completeness. OPT-IN, default off: `build_file_index`'s existing
caller (the registered `cartography.file_index` op, `ops/cartography_file_index.py`)
must see byte-identical output with the arm left at its default — an
unconditional widen would let scratch/venv/build output enter the index for
every existing caller, a real perf cost this module does not get to impose
uninvited. `list_untracked_files` shells out to
`git ls-files --others --exclude-standard` (read-only; same DR-208
precedent `list_tracked_files` cites) — `--exclude-standard` honors
`.gitignore`, so build/venv/scratch trees callers already ignore stay
excluded even with the arm on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.cartography._guard import path_guard
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


def list_untracked_files(target_root: str | Path) -> list:
    """Return the sorted list of untracked, non-ignored files under `target_root`.

    Shells out to `git ls-files --others --exclude-standard` (read-only git
    query — same DR-208 precedent `cartography.tree.list_tracked_files`
    cites). `--exclude-standard` applies `.gitignore`/`.git/info/exclude`/
    core excludes, so scratch/venv/build output already ignored by the repo
    stays excluded. Raises RuntimeError if `target_root` is not inside a git
    worktree or the git invocation otherwise fails.
    """
    root = path_guard(target_root, ".")
    cmd = ["git", "ls-files", "--others", "--exclude-standard"]
    from coordinator_core.win_portability import no_console_creationflags

    result = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-files --others failed under {root!r} "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    return sorted(line for line in result.stdout.splitlines() if line)


def build_file_index(
    target_root: str | Path,
    scope: Optional[Sequence[str]] = None,
    include_untracked: bool = False,
) -> dict:
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
        include_untracked: OPT-IN, default False. When True, also folds in
            every untracked, non-ignored file under `target_root`
            (`list_untracked_files`) into `index`/`systems`/`file_count` —
            a fresh input corpus is frequently untracked and a silently-short
            inventory is the worst failure shape here. `scope` does NOT
            narrow the untracked arm — `git ls-files --others` has no
            pathspec applied here, by design: the untracked corpus is
            usually exactly what `scope` would otherwise be narrowing away.
            Left at its default, this parameter changes nothing about the
            existing registered-op caller's output (see module docstring).

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
    all_paths = list(tracked)
    if include_untracked:
        untracked = list_untracked_files(target_root)
        seen = set(all_paths)
        for relpath in untracked:
            if relpath not in seen:
                all_paths.append(relpath)
                seen.add(relpath)

    index = {}
    systems: dict = {}
    unmapped_count = 0
    for relpath in all_paths:
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
        "file_count": len(all_paths),
        "unmapped_count": unmapped_count,
    }

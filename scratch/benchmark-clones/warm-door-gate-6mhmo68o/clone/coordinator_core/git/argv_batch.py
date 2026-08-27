"""coordinator_core.git.argv_batch -- argv-length batching for a pathspec
handed to a single `git` subprocess call, against the Windows `CreateProcess`
command-line cap.

Relocated (2026-08-26, docs/plans/2026-08-26-the-archival-commit-helper-
computes-its-own-tree.md, C1) from `coordinator_core.ops.ceremony.git_native`
where it originated (2026-08-15, itself a promotion out of
`commit_pipeline.py`'s own `_diverging_paths_chunked`). This is argv
batching, not tree algebra -- it does not belong beside `tree_spine.py`'s
spine-rewrite helpers, which is why C1 splits the move into two destination
modules rather than one. `git_native.py` re-exports both names so every
existing caller (`commit_pipeline.py`'s module-level aliases,
`commit_gates.py`'s direct import, `git_state.py`'s function-scoped import)
keeps importing what it imports today -- see each site's own comment for why.

Pure relocation: behaviour, signature, and docstring content are unchanged
from the promoted original.
"""

from __future__ import annotations

from typing import List, Sequence

#: Character budget for the pathspec batch handed to a single `diverging_
#: paths()` `git diff` subprocess call from `commit_scoped()`'s own
#: divergence check, sized well under the Windows `CreateProcess` command-
#: line cap (32767 UTF-16 code units) with generous headroom for the
#: `git.exe` path itself, the fixed `diff --cached --name-only --` argv
#: prefix, per-argument quoting overhead around any path containing a
#: space, and the second, unfiltered `git diff --name-only --` call
#: `diverging_paths()` also issues against the same pathspec. Same value
#: and reasoning as `commit_pipeline._DIVERGENCE_CHECK_ARGV_BUDGET_CHARS`
#: (a percolate-publish batch, ~2000-2700 paths, blows the raw 32767 cap
#: outright on one argv -- `rc=127`, "divergence indeterminate") --
#: promoted here (2026-08-15) as the shared home for `_chunk_paths()`
#: itself, so `commit_pipeline.py` imports both rather than growing a
#: second, independently-drifting copy. See `_chunk_paths()`'s own
#: docstring for why this module, not `commit_pipeline.py`, is the shared
#: home: `commit_pipeline.py` already imports `git_native` (this module),
#: so the reverse import direction would be circular.
_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS = 6000


def _chunk_paths(
    paths: Sequence[str], *, budget_chars: int = _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS
) -> List[List[str]]:
    """Pack `paths` into argv-safe chunks, each bounded by `budget_chars`.

    Promoted here (2026-08-15) from `commit_pipeline.py` (where it was
    originally extracted from `_diverging_paths_chunked`, that module's
    first caller of this packing shape) so `commit_scoped()`'s own chunked
    `diverging_paths()` divergence check (below) can reuse the identical
    packer and budget instead of forking a second, subtly-different copy.
    `commit_pipeline.py` cannot host the shared copy itself: it already
    imports this module (`from coordinator_core.ops.ceremony import
    git_native`), so a `git_native` -> `commit_pipeline` import back would
    be circular. Behaviour is unchanged from the original -- this is a
    promotion, not a rewrite; `commit_pipeline.py` now imports `_chunk_
    paths`/`_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS` from here instead of
    defining its own.

    Packing is greedy and order-preserving: a path never crosses a chunk
    boundary, and no chunk exceeds `budget_chars` (a single path longer
    than the budget still gets its own one-path chunk rather than being
    dropped or truncated -- callers must not assume every chunk is
    non-trivially sized). Returns `[]` for an empty `paths`, never a
    single empty chunk.
    """
    if not paths:
        return []

    chunks: List[List[str]] = []
    chunk: List[str] = []
    chunk_chars = 0
    for p in paths:
        added = len(p) + 1
        if chunk and chunk_chars + added > budget_chars:
            chunks.append(chunk)
            chunk = []
            chunk_chars = 0
        chunk.append(p)
        chunk_chars += added
    if chunk:
        chunks.append(chunk)
    return chunks

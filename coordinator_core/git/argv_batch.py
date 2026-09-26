
from __future__ import annotations

from typing import List, Sequence

#: and reasoning as `commit_pipeline._DIVERGENCE_CHECK_ARGV_BUDGET_CHARS`
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

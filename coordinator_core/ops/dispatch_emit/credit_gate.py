"""Credit gate: pure set arithmetic deciding whether a terminal commit may credit a chunk.

Stdlib only, POSIX repo-relative strings, no subprocess or git. ``terminal_commit`` feeds it the
snapshot it already holds.
"""

from __future__ import annotations

from typing import AbstractSet, Iterable, Mapping, Optional, Sequence

REFUSED_CREDITED_UNCOMMITTED = "credited-chunk-uncommitted"
REFUSED_NO_CREDITED_LANDING = "no-credited-chunk-landed"

DOCS_CHUNK_ID = "docs"
SIZING_CHUNK_ID = "sizing"

#: Chunk ids ``ask_stage.stage`` mints that carry run records, not delivery. terminal_commit's
#: "run-note" scope chunk never appears in ``request.chunks`` for crediting, so it is not a member.
BOOKKEEPING_CHUNK_IDS = frozenset({DOCS_CHUNK_ID, SIZING_CHUNK_ID})


def stranded_residue(
    stranded: Mapping[str, Sequence[str]], paths: Iterable[str]
) -> dict[str, list[str]]:
    """The subset of ``paths`` a stranded chunk declares, as ``{chunk id: sorted paths}``."""
    wanted = set(paths)
    out: dict[str, list[str]] = {}
    for cid, declared in stranded.items():
        hit = sorted(wanted.intersection(declared))
        if hit:
            out[cid] = hit
    return out


def uncommitted_credited(
    credited: Mapping[str, Sequence[str]],
    commit_paths: AbstractSet[str],
    deleted: AbstractSet[str],
    sibling: AbstractSet[str],
    changed: Optional[AbstractSet[str]],
) -> dict[str, list[str]]:
    """Per credited chunk, the paths that differ from HEAD yet are not in this commit.

    ``changed`` None means the snapshot was unanswerable: every declared path counts as changed.
    An empty result means the credit holds.
    """
    landing = commit_paths | deleted | sibling
    out: dict[str, list[str]] = {}
    for cid, declared in credited.items():
        missing = sorted(
            p for p in set(declared) if (changed is None or p in changed) and p not in landing
        )
        if missing:
            out[cid] = missing
    return out


def no_credited_landing(
    stranded_ids: AbstractSet[str],
    contributing_ids: AbstractSet[str],
    landed_ids: AbstractSet[str],
) -> bool:
    """True when something is stranded and no non-bookkeeping chunk contributes or already landed."""
    if not stranded_ids:
        return False
    return not ((set(contributing_ids) | set(landed_ids)) - BOOKKEEPING_CHUNK_IDS)

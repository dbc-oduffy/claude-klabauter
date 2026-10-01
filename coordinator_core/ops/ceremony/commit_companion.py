"""Pure helper: which peer-held paths does a commit leave behind untracked.

Takes plain mappings (no claim-substrate import). One index walk, one stat per
candidate missing from the index; no subprocess, no directory walk, no cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from coordinator_core.git import git_index
from coordinator_core.session.claim_index import _normalize_key


def untracked_companions(
    worktree_root: Path,
    held_by: Mapping[str, Sequence[str]],
    peers: Iterable[str],
    pathspec: Iterable[str],
) -> Dict[str, List[str]]:
    """Return ``{sid: sorted paths}`` for each peer in ``peers`` holding a path that
    is outside ``pathspec``, absent from the git index, and a file on disk.

    Sids with no companions are omitted. ``git_index.IndexParseError`` (including
    the v4 refusal) propagates unchanged; the caller owns degradation.
    """
    root = Path(worktree_root)
    excluded = {_normalize_key(p) for p in pathspec}
    per_peer: Dict[str, List[str]] = {}
    for sid in peers:
        held = [_normalize_key(p) for p in held_by.get(sid, ())]
        mine = sorted({p for p in held if p not in excluded})
        if mine:
            per_peer[sid] = mine
    candidates = {p for paths in per_peer.values() for p in paths}
    if not candidates:
        return {}

    tracked = git_index.parse_index_identity(root, wanted=candidates)
    on_disk = {p for p in candidates if p not in tracked and (root / p).is_file()}

    result: Dict[str, List[str]] = {}
    for sid, paths in per_peer.items():
        found = [p for p in paths if p in on_disk]
        if found:
            result[sid] = found
    return result

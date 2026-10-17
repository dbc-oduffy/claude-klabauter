"""Pure helpers: which peer-held paths a commit leaves behind untracked, and which
dirty paths the committing session itself holds outside its pathspec.

Takes plain mappings (no claim-substrate import). One index walk, one stat per
candidate missing from the index; no subprocess, no directory walk, no cache.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Collection, Dict, Iterable, List, Mapping, Sequence

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


def _differs_from_index(path: Path, entry: git_index.IndexIdentity) -> bool:
    """True when the file's content differs from the index blob; hashes only on a stat mismatch."""
    st = path.stat()
    if (
        st.st_size == entry.size
        and int(st.st_mtime_ns // 1_000_000_000) == entry.mtime
        and st.st_mtime_ns % 1_000_000_000 == entry.mtime_nsec
    ):
        return False
    data = path.read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return blob != entry.sha


def own_left_behind(
    worktree_root: Path,
    held_by: Mapping[str, Sequence[str]],
    own_sid: str,
    pathspec: Iterable[str],
    contested: Collection[str] = (),
) -> List[str]:
    """Sorted normalized paths ``own_sid`` holds, outside ``pathspec``, not in ``contested``,
    that are dirty against the index: untracked-and-a-file-on-disk, or tracked with a stat
    mismatch whose content differs. Zero subprocess; IndexParseError propagates.
    """
    root = Path(worktree_root)
    excluded = {_normalize_key(p) for p in pathspec}
    excluded.update(_normalize_key(p) for p in contested)
    candidates = sorted(
        {p for p in map(_normalize_key, held_by.get(own_sid, ())) if p not in excluded}
    )
    if not candidates:
        return []

    tracked = git_index.parse_index_identity(root, wanted=set(candidates))
    dirty: List[str] = []
    for p in candidates:
        f = root / p
        try:
            entry = tracked.get(p)
            if entry is None:
                if f.is_file():
                    dirty.append(p)
            elif f.is_file() and _differs_from_index(f, entry):
                dirty.append(p)
        except OSError:
            continue
    return dirty

"""Pure helper: which dirty paths the committing session itself holds outside its pathspec.

Takes plain mappings (no claim-substrate import). One index walk, one stat per
candidate; no subprocess, no directory walk, no cache.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Collection, Iterable, Iterator, List, Mapping, Optional, Sequence

from coordinator_core.git import git_index
from coordinator_core.session.claim_index import _normalize_key

#: Resolver ``candidates -> claimed subset`` (``None`` = contest unknown). Set by a caller that
#: knows runs of the committing session which hold paths the session-level claim cannot separate.
SiblingClaims = Callable[[Sequence[str]], Optional[Collection[str]]]

_sibling_claims: ContextVar[Optional[SiblingClaims]] = ContextVar(
    "own_left_behind_sibling_claims", default=None
)


@contextmanager
def excluding_sibling_claims(resolver: SiblingClaims) -> Iterator[None]:
    """Within the block, ``own_left_behind`` drops every candidate ``resolver`` reports claimed,
    and returns nothing when it reports the contest unknown (``None``)."""
    token = _sibling_claims.set(resolver)
    try:
        yield
    finally:
        _sibling_claims.reset(token)


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
    if _blob_sha(data) == entry.sha:
        return False
    # core.autocrlf=true (Windows default) indexes the LF form of a CRLF working file; an
    # EOL-only difference is not a pending change, and no subprocess may ask git.
    return _blob_sha(data.replace(b"\r\n", b"\n")) != entry.sha


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def own_left_behind(
    worktree_root: Path,
    held_by: Mapping[str, Sequence[str]],
    own_sid: str,
    pathspec: Iterable[str],
    contested: Collection[str] = (),
) -> List[str]:
    """Sorted normalized paths ``own_sid`` holds, outside ``pathspec``, not in ``contested``,
    that are dirty against the index: untracked-and-a-file-on-disk, or tracked with a stat
    mismatch whose content differs. Under ``excluding_sibling_claims`` a path a sibling run
    claims is also dropped. Zero subprocess; IndexParseError propagates.
    """
    root = Path(worktree_root)
    excluded = {_normalize_key(p) for p in pathspec}
    excluded.update(_normalize_key(p) for p in contested)
    candidates = sorted(
        {p for p in map(_normalize_key, held_by.get(own_sid, ())) if p not in excluded}
    )
    resolver = _sibling_claims.get()
    if resolver is not None and candidates:
        claimed = resolver(candidates)
        if claimed is None:
            return []
        sibling = {_normalize_key(p) for p in claimed}
        candidates = [p for p in candidates if p not in sibling]
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

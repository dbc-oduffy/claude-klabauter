"""Pure line-based 3-way merge that lands one row's pre->post delta onto HEAD bytes.

Stdlib only, zero spawns, zero I/O. Lines split with keepends, so line endings and a
missing trailing newline round-trip byte-for-byte.
"""
from __future__ import annotations

import difflib
from typing import List, Optional, Tuple

_Edit = Tuple[int, int, Tuple[bytes, ...]]


def _edits(base: List[bytes], other: List[bytes]) -> List[_Edit]:
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [
        (i1, i2, tuple(other[j1:j2]))
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    ]


def _collide(a: _Edit, b: _Edit) -> bool:
    a1, a2, _ = a
    b1, b2, _ = b
    if a1 == a2 and b1 == b2:
        return a1 == b1
    if a1 == a2:
        return b1 < a1 < b2
    if b1 == b2:
        return a1 < b1 < a2
    return a1 < b2 and b1 < a2


def apply_own_delta(pre: bytes, post: bytes, head: bytes) -> Optional[bytes]:
    """Apply the pre->post delta onto head; None when a hunk collides with a head change.

    A hunk whose pre-side span is unchanged in head applies; head changes elsewhere
    are kept. An identical change on both sides counts once.
    """
    if pre == head:
        return post
    if pre == post:
        return head
    base = pre.splitlines(keepends=True)
    ours = _edits(base, post.splitlines(keepends=True))
    theirs = _edits(base, head.splitlines(keepends=True))
    merged: List[_Edit] = list(theirs)
    for o in ours:
        if o in theirs:
            continue
        if any(_collide(o, t) for t in theirs):
            return None
        merged.append(o)
    merged.sort(key=lambda e: (e[0], e[1]))
    out: List[bytes] = []
    pos = 0
    for i1, i2, lines in merged:
        out.extend(base[pos:i1])
        out.extend(lines)
        pos = i2
    out.extend(base[pos:])
    return b"".join(out)

"""Hunk-level staging for a path two unlanded runs both edited.

Contract: ``split`` answers, for one co-claimed path, which worktree hunks against HEAD are this
run's (its ``write_baseline`` blob to the worktree) and whether a foreign run's hunk overlaps one
of them. Zero spawns; stdlib only at module top, readers imported lazily (cold-import pin).

Invariant: every range is compared in one coordinate space, the worktree's lines. Baselines and
the worktree are raw bytes and meet HEAD (checkin form) only through ``kept_work.checkin_form``.
Trap: a foreign run whose baseline predates ours and that kept editing after our baseline is
indistinguishable from us on those lines; its later hunks are read as ours.
Trap: the staged blob is derived from worktree bytes at call time; the commit that consumes it
lands that snapshot, so an edit made between the split and the commit is not carried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_Range = Tuple[int, int]
_Op = Tuple[str, int, int, int, int]


@dataclass(frozen=True)
class Split:
    """Result of ``split``.

    ``staged_blob``: sha of the worktree text with every non-own hunk reverted to its HEAD text
    (checkin form, written through ``git_objects.write_object``); None when ``overlaps`` is
    non-empty (refuse).
    ``own_headers``: ``@@ -a,b +c,d @@`` headers (HEAD against worktree) of this run's hunks.
    ``overlaps``: ``(run_id, header)`` for each foreign hunk sharing worktree lines with an own
    hunk; ``run_id`` is None for pre-run dirt no run owns.
    """

    staged_blob: Optional[str]
    own_headers: List[str] = field(default_factory=list)
    overlaps: List[Tuple[Optional[str], str]] = field(default_factory=list)


def _hit(a: _Range, b: _Range) -> bool:
    """Shared lines; a zero-length range is a point that hits only strictly inside or on an equal
    point. Adjacency is not a hit."""
    (s1, e1), (s2, e2) = a, b
    if s1 < e1 and s2 < e2:
        return s1 < e2 and s2 < e1
    if s1 == e1 and s2 == e2:
        return s1 == s2
    if s1 == e1:
        return s2 < s1 < e2
    return s1 < s2 < e1


def _touch(r: _Range, blk: _Range) -> bool:
    """Like ``_hit`` but a point on a block's edge counts: ownership of a diff block."""
    if _hit(r, blk):
        return True
    (s, e), (b0, b1) = r, blk
    if s == e:
        return b0 <= s <= b1
    return b0 == b1 and s <= b0 <= e


def _span(start: int, end: int) -> str:
    n = end - start
    return f"{start if n == 0 else start + 1},{n}"


def _header(blk: _Op) -> str:
    return f"@@ -{_span(blk[1], blk[2])} +{_span(blk[3], blk[4])} @@"


def _changed(a: List[bytes], b: List[bytes]) -> List[_Op]:
    from coordinator_core.git.since_base_overlap import _opcodes

    return [op for op in _opcodes(a, b) if op[0] != "equal"]


def _map(ops: List[_Op], pos: int, end: bool) -> int:
    """A left-side position carried through the full opcodes ``ops`` to the right side; inside a
    changed block a start maps to the block's start and an end to its end."""
    for tag, i1, i2, j1, j2 in ops:
        if i1 <= pos <= i2:
            if tag == "equal":
                return j1 + (pos - i1)
            return j2 if end else j1
    return pos


def _refuse(run: Optional[str], header: str) -> Split:
    return Split(staged_blob=None, overlaps=[(run, header)])


def split(
    root: Path,
    path: str,
    *,
    own_run_id: str,
    own_baseline_blob: str,
    foreign: Sequence[Tuple[str, str]],
) -> Split:
    """Split ``path``'s worktree diff into this run's hunks and everyone else's.

    ``foreign`` is ``[(run_id, baseline_blob)]`` for each co-claiming run. See ``Split``. Any
    unreadable object, refused filter conversion or unsplittable mixed block refuses (a
    non-empty ``overlaps``); it never raises on content.
    """
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit
    from coordinator_core.git.since_base_overlap import _head_sha, _lines, _opcodes
    from coordinator_core.ops.dispatch_emit import kept_work

    root = Path(root)
    common = resolve_git_common_dir(root)

    def lifted(blob: str) -> Optional[List[bytes]]:
        obj = git_objects.read_object(common, blob)
        if obj is None:
            return None
        data = kept_work.checkin_form(root, path, obj[1])
        return None if data is None else data.splitlines(keepends=True)

    head = _head_sha(root)
    head_lines = _lines(common, _blob_at_commit(common, head, path)) if head else None
    try:
        raw = (root / path).read_bytes()
    except OSError:
        raw = None
    wt = kept_work.checkin_form(root, path, raw) if raw is not None else None
    own_base = lifted(own_baseline_blob)
    if head_lines is None or wt is None or own_base is None:
        return _refuse(None, "unreadable")
    work = wt.splitlines(keepends=True)

    blocks = _changed(head_lines, work)
    own_ops = _changed(own_base, work)
    own: List[Tuple[_Op, _Range]] = [(op, (op[3], op[4])) for op in own_ops]

    # (owner, worktree range) per foreign hunk; overlap_rs are foreign hunks over an own hunk.
    foreign_rs: List[Tuple[Optional[str], _Range]] = []
    overlap_rs: List[Tuple[Optional[str], _Range]] = []

    f_data = []
    for run_id, blob in foreign:
        fb = lifted(blob)
        if fb is None:
            return _refuse(run_id, "unreadable")
        f_data.append((run_id, fb, [(op[3], op[4]) for op in _changed(fb, work)]))

    def predates(fb: List[bytes]) -> bool:
        to_ob = _opcodes(fb, own_base)
        for _, _, _, j1, j2 in _changed(head_lines, fb):
            if not any(
                t == "equal" and a1 <= j1 and j2 <= a2 for t, a1, a2, _, _ in to_ob
            ):
                return False
        return True

    postdating = [(r, fb, fr) for r, fb, fr in f_data if not predates(fb)]
    predating = [(r, fb, fr) for r, fb, fr in f_data if predates(fb)]

    # Pre-run dirt: HEAD against our baseline, carried into worktree coordinates.
    full_ob_w = _opcodes(own_base, work)
    full_h_ob = _changed(head_lines, own_base)
    for _, _, _, j1, j2 in full_h_ob:
        wr = (_map(full_ob_w, j1, False), _map(full_ob_w, j2, True))
        owner = next(
            (r for r, _, fr in predating if any(_touch(wr, f) for f in fr)), None
        )
        foreign_rs.append((owner, wr))

    # Postdating runs: their baseline already carries our earlier edits.
    claimed: List[int] = []
    for run_id, fb, f_ranges in postdating:
        ob_fb = _changed(own_base, fb)
        for f in f_ranges:
            foreign_rs.append((run_id, f))
        for idx, (op, wr) in enumerate(own):
            if not any(_hit(wr, f) for f in f_ranges):
                continue
            ob = (op[1], op[2])
            if any(_hit(ob, (a1, a2)) for _, a1, a2, _, _ in ob_fb):
                overlap_rs.append((run_id, wr))
            else:
                claimed.append(idx)
    own = [o for idx, o in enumerate(own) if idx not in claimed]
    own_rs = [wr for _, wr in own]

    def head_of(wr: _Range) -> str:
        blk = next((b for b in blocks if _touch(wr, (b[3], b[4]))), None)
        return _header(blk) if blk else f"@@ -0,0 +{_span(wr[0], wr[1])} @@"

    overlaps: List[Tuple[Optional[str], str]] = []
    seen = set()

    def add(owner: Optional[str], wr: _Range) -> None:
        item = (owner, head_of(wr))
        if item not in seen:
            seen.add(item)
            overlaps.append(item)

    for owner, wr in overlap_rs:
        add(owner, wr)
    for owner, fr in foreign_rs:
        if any(_hit(fr, o) for o in own_rs):
            add(owner, fr)
    if overlaps:
        return Split(staged_blob=None, overlaps=overlaps)

    out: List[bytes] = []
    own_headers: List[str] = []
    pos = 0
    for blk in blocks:
        _, i1, i2, j1, j2 = blk
        out.extend(work[pos:j1])
        pos = j2
        rng = (j1, j2)
        has_own = any(_touch(o, rng) for o in own_rs)
        has_foreign = any(_touch(f, rng) for _, f in foreign_rs)
        if has_own and not has_foreign:
            out.extend(work[j1:j2])
            own_headers.append(_header(blk))
        elif not has_own:
            out.extend(head_lines[i1:i2])
        elif i2 - i1 == j2 - j1 and j2 > j1:
            for k in range(j2 - j1):
                line = (j1 + k, j1 + k + 1)
                if any(_hit(line, o) for o in own_rs):
                    out.append(work[j1 + k])
                else:
                    out.append(head_lines[i1 + k])
            own_headers.append(_header(blk))
        else:
            return _refuse(None, _header(blk))
    out.extend(work[pos:])
    staged = git_objects.write_object(common, b"blob", b"".join(out))
    return Split(staged_blob=staged, own_headers=own_headers)

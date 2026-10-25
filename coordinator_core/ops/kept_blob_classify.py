"""Does a recovered kept blob still matter against HEAD: SUPERSEDED or NOT-LANDED?

Invariant: classification needs only "is HEAD unchanged by the merge" and "is there a conflict".
test_kept_blob_classify pins those two answers against `git merge-file -p`; the merged bytes of a
conflicting merge are not byte-compared with git's.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Verdict:
    verdict: str  # "SUPERSEDED" | "NOT-LANDED" | "CARRIED" (EOL-only difference from HEAD)
    commit: Optional[str]
    why: str


_Hunk = Tuple[int, int, List[bytes]]  # base[i1:i2] replaced by these side lines


def _hunks(base: List[bytes], side: List[bytes]) -> List[_Hunk]:
    sm = SequenceMatcher(None, base, side, autojunk=False)
    return [(i1, i2, side[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _apply(base: List[bytes], start: int, end: int, hunks: List[_Hunk]) -> List[bytes]:
    out: List[bytes] = []
    pos = start
    for i1, i2, lines in hunks:
        out += base[pos:i1] + lines
        pos = i2
    return out + base[pos:end]


def _with_nl(lines: List[bytes]) -> List[bytes]:
    if lines and not lines[-1].endswith(b"\n"):
        return lines[:-1] + [lines[-1] + b"\n"]
    return lines


def merge3(base: bytes, kept: bytes, head: bytes) -> Tuple[bytes, int]:
    """(merged bytes, conflict count): kept's changes over base applied onto head, diff3 style.

    Hunks that touch or overlap on both sides form one region: identical edits are taken once,
    differing edits become a conflict between git-style markers (head side first).
    """
    b = base.splitlines(keepends=True)
    k = kept.splitlines(keepends=True)
    h = head.splitlines(keepends=True)
    hk, hh = _hunks(b, k), _hunks(b, h)
    out: List[bytes] = []
    conflicts = 0
    pos = 0
    ik = ih = 0
    while ik < len(hk) or ih < len(hh):
        starts = []
        if ik < len(hk):
            starts.append(hk[ik][0])
        if ih < len(hh):
            starts.append(hh[ih][0])
        start = min(starts)
        end = start
        ck: List[_Hunk] = []
        ch: List[_Hunk] = []
        grew = True
        while grew:
            grew = False
            while ik < len(hk) and hk[ik][0] <= end:
                ck.append(hk[ik])
                end = max(end, hk[ik][1])
                ik += 1
                grew = True
            while ih < len(hh) and hh[ih][0] <= end:
                ch.append(hh[ih])
                end = max(end, hh[ih][1])
                ih += 1
                grew = True
        out += b[pos:start]
        if ck and not ch:
            out += _apply(b, start, end, ck)
        elif ch and not ck:
            out += _apply(b, start, end, ch)
        else:
            tk, th = _apply(b, start, end, ck), _apply(b, start, end, ch)
            if tk == th:
                out += th
            else:
                conflicts += 1
                out += [b"<<<<<<< head\n"] + _with_nl(th) + [b"=======\n"] + _with_nl(tk) + [b">>>>>>> kept\n"]
        pos = end
    out += b[pos:]
    return b"".join(out), conflicts


def _added_lines(base: bytes, kept: bytes) -> set:
    b = base.splitlines(keepends=True)
    k = kept.splitlines(keepends=True)
    added = set()
    for tag, _i1, _i2, j1, j2 in SequenceMatcher(None, b, k, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            added.update(k[j1:j2])
    return added


def _first_landing(
    base: bytes, kept: bytes, history: List[Tuple[str, datetime, bytes]]
) -> Optional[str]:
    added = _added_lines(base, kept)
    for commit, _when, blob in history:
        if blob == base or not added <= set(blob.splitlines(keepends=True)):
            continue
        merged, n = merge3(base, kept, blob)
        if n == 0 and merged == blob:
            return commit
    return None


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def classify(
    base: bytes,
    kept: bytes,
    head: Optional[bytes],
    history: List[Tuple[str, datetime, bytes]],
) -> Verdict:
    """3-way merge base->kept onto head: SUPERSEDED(commit) when head is unchanged, else NOT-LANDED."""
    if head is None:
        return Verdict("NOT-LANDED", None, "path absent at HEAD")
    if head == kept:
        return Verdict("SUPERSEDED", _first_landing(base, kept, history), "HEAD equals kept")
    nbase, nkept, nhead = _lf(base), _lf(kept), _lf(head)
    if nkept == nhead:
        return Verdict("CARRIED", None, "EOL-only difference from HEAD")
    merged, n = merge3(nbase, nkept, nhead)
    if n:
        return Verdict("NOT-LANDED", None, f"merge conflicts ({n}) onto HEAD")
    if merged == nhead:
        return Verdict("SUPERSEDED", _first_landing(base, kept, history), "kept hunks already in HEAD")
    return Verdict("NOT-LANDED", None, "kept hunks missing from HEAD (clean merge changes HEAD)")

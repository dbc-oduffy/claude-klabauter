"""Pure scanner for lettered parts of an ask and the parts a delivery note records as dropped."""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import NamedTuple


class StatedPart(NamedTuple):
    label: str
    text: str


class DroppedPart(NamedTuple):
    label: str
    text: str
    phrase: str


_MARKER = re.compile(r"(?<![\w(])\(([a-z])\)\s*", re.IGNORECASE)
_REF = re.compile(r"\(([a-z])\)", re.IGNORECASE)
_ROW_ID = re.compile(r"\bR\d+\b")
_SENTENCE_END = re.compile(r"(?<=[.;!?])\s+|\n+")
_DROP = re.compile(
    r"\bnot\s+(?:been\s+)?(?:done|investigated|addressed|delivered|implemented|attempted)\b"
    r"|\bwas\s+not\s+(?:done|investigated|addressed|delivered|implemented|attempted)\b"
    r"|\bnot\s+part\s+of\b"
    r"|\bout\s+of\s+scope\b"
    r"|\bdeferred\b|\bdropped\b",
    re.IGNORECASE,
)


def lettered_parts(text: str) -> tuple[StatedPart, ...]:
    """Parse the first run of "(a) ...", "(b) ..." spans; a run must start at (a) and climb one letter at a time."""
    marks = list(_MARKER.finditer(text or ""))
    parts: list[StatedPart] = []
    want = "a"
    for i, m in enumerate(marks):
        if m.group(1).lower() != want:
            continue
        end = len(text)
        for nxt in marks[i + 1 :]:
            if nxt.group(1).lower() == chr(ord(want) + 1):
                end = nxt.start()
                break
        body = re.sub(r"\s+(?:and|or)$", "", text[m.end() : end].strip().rstrip(",;").strip())
        parts.append(StatedPart(want, body))
        want = chr(ord(want) + 1)
        if want > "z":
            break
    return tuple(parts)


def dropped_parts(note_text: str, ruled: Collection[str] = ()) -> tuple[DroppedPart, ...]:
    """Lettered parts or ledger row ids whose sentence says not done, not investigated, or out of scope; labels in `ruled` are excluded."""
    skip = {r.strip().lower() for r in ruled}
    stated = {p.label: p.text for p in lettered_parts(note_text)}
    found: dict[str, DroppedPart] = {}

    def _add(label: str, sentence: str, phrase: str) -> None:
        key = label.lower()
        if key in skip or key in found:
            return
        found[key] = DroppedPart(label.lower() if len(label) == 1 else label, stated.get(key, sentence.strip()), phrase)

    for sentence in _SENTENCE_END.split(note_text or ""):
        sm = _DROP.search(sentence)
        if not sm:
            continue
        marks = list(_MARKER.finditer(sentence))
        prefix_end = marks[0].start() if marks else len(sentence)
        if marks and sm.start() >= prefix_end:
            for i, m in enumerate(marks):
                seg_end = marks[i + 1].start() if i + 1 < len(marks) else len(sentence)
                if m.start() <= sm.start() < seg_end:
                    _add(m.group(1), sentence, sm.group(0))
                    break
            continue
        refs = [m.group(1) for m in _REF.finditer(sentence)] + _ROW_ID.findall(sentence)
        for ref in refs:
            _add(ref, sentence, sm.group(0))
    return tuple(found.values())

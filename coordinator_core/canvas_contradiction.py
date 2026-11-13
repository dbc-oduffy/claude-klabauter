"""Pure check of steering text against the PM's words and a canvas's settled decisions.

`governing_sources` gathers the PM verbatim and the `[thread:<id>]` items of each governing
canvas's SETTLED.md; `contradictions` flags candidate text that contradicts a source sentence.
Stdlib only, no subprocess, never raises on a missing or malformed source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Optional, Sequence

SETTLED_NAME = "SETTLED.md"
_HEADING = "## Thread-settled behaviour"
_THREAD_ITEM = re.compile(r"^\s*[-*]\s+(?:\[item\]\s+)?\[thread:([^\]\s]+)\]\s*(.*\S)")
_COMMENT = re.compile(r"<!--.*?-->", re.S)

_IN_PLACE = {"modal", "dialog", "popup", "overlay", "sheet", "drawer", "banner", "toast"}
_ROUTE_WORDS = {"page", "screen", "route", "navigate", "redirect"}
_PATH_TOKEN = re.compile(r"(?:^|[\s(\[\"'`])/[A-Za-z][\w\-.]*(?:/[\w\-.]+)*")
_NEGATORS = {"not", "never", "no", "without", "don't", "dont", "doesn't", "cannot"}
_ANTONYMS = (
    ("dismissable", "non-dismissable"),
    ("optional", "required"),
    ("show", "hide"),
    ("before", "after"),
)
_STOPWORDS = frozenset(
    "a an the and or of to in on at for with by from is are be been was were it its this that these "
    "those as if then than so but into onto up out over under should must will would can could may "
    "might shall do does did has have had i we you they he she them our your their there here when "
    "while which who what also only just".split()
)
_WORD = re.compile(r"[a-z][a-z'\-]*")
_EXCERPT = 200


def governing_sources(repo_root, sizing: Optional[Mapping], paths: Sequence[str]) -> list:
    """Sources a steering text must not contradict: `{kind, ref, text}` per PM quote and canvas item."""
    sources: list = []
    try:
        from coordinator_core.ops.dispatch_emit.requirements_ledger import pm_words

        for i, text in enumerate(pm_words(sizing), 1):
            sources.append({"kind": "pm-verbatim", "ref": f"pm-verbatim#{i}", "text": text})
    except Exception:
        pass
    try:
        from coordinator_core import design_registry

        hits, error = design_registry.lookup(repo_root, list(paths or []))
    except Exception:
        return sources
    if error or not hits:
        return sources
    root = Path(repo_root)
    for hit in hits:
        harvest = hit.get("harvest_path")
        if not harvest:
            continue
        settled = (root / harvest).parent / SETTLED_NAME
        try:
            body = settled.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for tid, text in _settled_items(body):
            sources.append({"kind": "canvas-settled", "ref": f"{hit.get('id')}#thread:{tid}", "text": text})
    return sources


def _settled_items(body: str) -> list:
    body = _COMMENT.sub("", body)
    out: list = []
    inside = False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line.strip() == _HEADING
            continue
        if not inside:
            continue
        m = _THREAD_ITEM.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def contradictions(text: str, sources) -> list:
    """Findings `{rule, source_ref, source_excerpt, candidate_excerpt, reason}`; empty when none."""
    if not isinstance(text, str) or not text.strip():
        return []
    cand_sentences = _sentences(text)
    found: list = []
    for source in sources or []:
        body = source.get("text") if isinstance(source, Mapping) else None
        if not isinstance(body, str):
            continue
        ref = str(source.get("ref", ""))
        for s_sent in _sentences(body):
            for c_sent in cand_sentences:
                reason = _surface_kind(s_sent, c_sent)
                rule = "surface-kind"
                if reason is None:
                    reason = _polarity(s_sent, c_sent)
                    rule = "polarity"
                if reason is not None:
                    found.append(
                        {
                            "rule": rule,
                            "source_ref": ref,
                            "source_excerpt": s_sent[:_EXCERPT],
                            "candidate_excerpt": c_sent[:_EXCERPT],
                            "reason": reason,
                        }
                    )
    return found


def _sentences(text: str) -> list:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _words(sentence: str) -> list:
    return _WORD.findall(sentence.lower().replace("’", "'"))


def _in_place_kinds(sentence: str) -> set:
    return {w.rstrip("s") if w.rstrip("s") in _IN_PLACE else w for w in _words(sentence)} & _IN_PLACE


def _routes(sentence: str) -> bool:
    return bool(_PATH_TOKEN.search(sentence)) or bool(set(_words(sentence)) & _ROUTE_WORDS)


def _surface_kind(source: str, candidate: str) -> Optional[str]:
    s_kinds, c_kinds = _in_place_kinds(source), _in_place_kinds(candidate)
    if s_kinds and _routes(candidate) and not c_kinds:
        return f"source names an in-place {sorted(s_kinds)[0]}; candidate routes to a destination"
    if c_kinds and _routes(source) and not s_kinds:
        return f"candidate names an in-place {sorted(c_kinds)[0]}; source routes to a destination"
    return None


def _content(words: list) -> set:
    out: set = set()
    for w in words:
        if w in _STOPWORDS or w in _NEGATORS:
            continue
        out.add(w[4:] if w.startswith("non-") else w)
    return out


def _antonym_sides(words: set) -> set:
    return {(i, side) for i, pair in enumerate(_ANTONYMS) for side, w in enumerate(pair) if w in words}


def _negated(words: list) -> bool:
    return any(w in _NEGATORS or (w.startswith("non-") and not _is_pair_member(w)) for w in words)


def _is_pair_member(word: str) -> bool:
    return any(word in pair for pair in _ANTONYMS)


def _polarity(source: str, candidate: str) -> Optional[str]:
    s_words, c_words = _words(source), _words(candidate)
    shared = _content(s_words) & _content(c_words)
    if len(shared) < 2:
        return None
    s_sides, c_sides = _antonym_sides(set(s_words)), _antonym_sides(set(c_words))
    for i, side in s_sides:
        if (i, 1 - side) in c_sides and (i, side) not in c_sides:
            return f"antonym pair {_ANTONYMS[i][0]}/{_ANTONYMS[i][1]} opposes across the two sentences"
    if _negated(s_words) != _negated(c_words):
        return f"exactly one side is negated over shared terms {sorted(shared)[:4]}"
    return None

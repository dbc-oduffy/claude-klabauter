"""Pure extractor of the repo paths a sizing's ask text rules out.

No subprocess, no git, no tree walk (stdlib plus `ledger_remaining_paths._clean`). Not a natural-language parser: a negation the
trigger set misses is simply not recognised.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Optional

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import _clean

_VERB = r"(?:touch|edit|modify|write|change)"
_TRIGGER = re.compile(
    rf"\b(?:not\s+(?:to\s+)?{_VERB}|(?:do\s+not|don'?t|never)\s+{_VERB}"
    r"|exclud(?:e|es|ed|ing)|out\s+of\s+scope\s*:|except)\b[:\s]*",
    re.IGNORECASE,
)
_LEAVE = re.compile(r"\bleave\s+(.+?)\s+alone\b", re.IGNORECASE)
_BARE = re.compile(r"^[\w\-]+$")
_FILLER = frozenset(
    "the a an any all this that these those it them anything everything file files in of to or and "
    "under dir directory directories folder folders path paths code tree".split()
)
_CLAUSE_END = re.compile(r"[;\n]|\.(?=\s|$)")


def _object_tokens(text: str) -> Iterator[str]:
    """Yield the cleaned path objects of one negated clause, stopping at the first prose word."""
    clause = _CLAUSE_END.split(text, 1)[0]
    joiner = True
    seen = False
    for raw in clause.split():
        word = raw.strip(",;:()`'\"").lower()
        if word in _FILLER:
            joiner = joiner or word in {"or", "and"}
            continue
        rel = _clean(raw)
        bare = raw.strip(",;:()`'\"")
        if rel is None and _BARE.match(bare) and (not seen or (joiner and re.search(r"[-_]", bare))):
            rel = bare
        if rel is None:
            return
        if seen and not joiner:
            return
        yield rel.rstrip("/")
        seen = True
        joiner = raw.endswith(",")


def _texts(sizing: Mapping[str, Any]) -> Iterator[str]:
    intent = sizing.get("intent")
    if isinstance(intent, str):
        yield intent
    analysis = sizing.get("em_analysis")
    if isinstance(analysis, Mapping) and isinstance(analysis.get("relayed_text"), str):
        yield analysis["relayed_text"]
    register = sizing.get("requirement_register")
    rows = register.get("rows") if isinstance(register, Mapping) else None
    for row in rows if isinstance(rows, Sequence) and not isinstance(rows, str) else ():
        if isinstance(row, Mapping) and isinstance(row.get("source_text"), str):
            yield row["source_text"]


def excluded_paths(sizing: Mapping[str, Any]) -> tuple[str, ...]:
    """Repo-relative paths or prefixes the sizing's ask text rules out, de-duplicated, in order.

    Register rows count whatever their status: a deferred row's exclusion still binds.
    """
    out: list[str] = []
    for text in _texts(sizing):
        for m in _TRIGGER.finditer(text):
            out.extend(p for p in _object_tokens(text[m.end():]) if p not in out)
        for m in _LEAVE.finditer(text):
            out.extend(p for p in _object_tokens(m.group(1)) if p not in out)
    return tuple(dict.fromkeys(out))


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def is_excluded(path: str, excluded: Sequence[str]) -> bool:
    """True when `path` equals an entry, sits under it, or has a segment equal to a bare entry."""
    p = _norm(path)
    segments = p.split("/")
    for entry in excluded:
        e = _norm(entry).rstrip("/")
        if not e:
            continue
        if p.casefold() == e.casefold() or p.casefold().startswith(e.casefold() + "/") or (
            "/" not in e and e.casefold() in [s.casefold() for s in segments]
        ):
            return True
    return False


def drop_excluded(paths: Sequence[str], excluded: Sequence[str]) -> list[str]:
    """Order-preserving filter removing every path `is_excluded` covers."""
    if not excluded:
        return list(paths)
    return [p for p in paths if not is_excluded(p, excluded)]

"""coordinator_core.roadmap.consumer_clause — refuse an exit-criterion statement whose
wire/use/feed/read/inject clause names no consumer-side command, and detect producer
outputs no row consumes.

A clause that only says a producer writes an artefact lets a run conclude "met" while
nothing reads the artefact. Pure in-process regex; no spawns, no I/O.
"""

from __future__ import annotations

import re
from typing import Optional

from coordinator_core.roadmap.post_stamp_clause import (
    EXIT_CRITERION_REFUSAL_HEAD,
    REMEDIATION_MARKER,
)

#: The wiring verbs and their inflections a clause is judged on.
WIRE_VERBS: tuple[str, ...] = (
    "wire", "wires", "wired",
    "use", "uses", "used", "using",
    "feed", "feeds", "fed", "feeding",
    "read", "reads", "reading",
    "inject", "injects", "injected", "injecting",
)

# A verb joined to a neighbour by "/", "-" or a word char ("wire/use/feed", "read-only",
# "use.py") is a mention, not a clause. The phrase keeps up to two following words.
_VERB_PHRASE = re.compile(
    r"(?<![-\w/])(?:" + "|".join(WIRE_VERBS) + r")(?![-\w/]|\.\w)(?:\s+[\w.'-]+){0,2}",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(r"(?<=[.;])\s+|\s*;\s*|,\s+and\s+", re.IGNORECASE)
_QUOTED = re.compile(r'"[^"]*"|“[^”]*”')
_BACKTICKED = re.compile(r"`[^`]+`")
_REPORTS = re.compile(r"\b(?:reports|shows|prints|returns|logs)\b", re.IGNORECASE)

_CONSUMER_WORDING = "a backticked consumer command, or '<command> reports|shows|prints|returns|logs' its result"


def _clauses(statement: object) -> list[str]:
    text = " ".join(str(statement or "").split())
    return _CLAUSE_SPLIT.split(text)


def producer_only_clause(statement: object) -> Optional[str]:
    """Return the offending wire-verb phrase of the first producer-only clause, else None.

    A clause with a wire verb passes when it carries a backticked command or a
    `<command> reports|shows|prints|returns|logs` wording; quoted text is a mention.
    Trap: any unbackticked use/read/feed clause is flagged, wiring or not.
    """
    for clause in _clauses(statement):
        if _BACKTICKED.search(clause) or _REPORTS.search(_QUOTED.sub(" ", clause)):
            continue
        m = _VERB_PHRASE.search(_QUOTED.sub(" ", clause))
        if m:
            return m.group(0).rstrip(".,;:")
    return None


def consumer_clause_refusal(statement: object) -> Optional[str]:
    """Refusal message naming the producer-only clause, or None when the statement is clean.

    Shares `EXIT_CRITERION_REFUSAL_HEAD` / `REMEDIATION_MARKER` with the other
    exit-criterion refusals so remediable-wording consumers keep parsing it.
    """
    hit = producer_only_clause(statement)
    if hit is None:
        return None
    verb = hit.split()[0].lower()
    return (
        f"{EXIT_CRITERION_REFUSAL_HEAD}producer-only {verb} clause ({hit!r}); "
        f"no consumer-side command shows the consumer using it. "
        f"{REMEDIATION_MARKER}{_CONSUMER_WORDING}."
    )


_DET = r"(?:(?:the|a|an)\s+)?"
_TOKEN = r"[\w.\-/]+"
_STOP = r"(?:and|or|but|so|yet|because|that|which|is|are|in|on|at|to|for|with)"
_PHRASE = rf"{_TOKEN}(?:\s+(?!{_STOP}\b){_TOKEN}){{0,2}}"
_SEAM_PATTERNS = (
    re.compile(rf"\bnothing\s+(?:feeds|reads|consumes|uses)\s+{_DET}(?P<seam>{_PHRASE})", re.IGNORECASE),
    re.compile(rf"{_DET}(?P<seam>{_TOKEN})\s+has\s+no\s+consumer\b", re.IGNORECASE),
    re.compile(rf"\bno\s+consumer\s+(?:row\s+)?for\s+{_DET}(?P<seam>{_PHRASE})", re.IGNORECASE),
    re.compile(rf"\bunwired\s+{_DET}(?P<seam>{_PHRASE})", re.IGNORECASE),
)
# Only a file-like or snake_case token is an artefact: "writes tests" is not a seam.
_PRODUCES = re.compile(
    rf"\b(?:writes|emits|produces|ships)\s+{_DET}(?P<seam>(?:[\w-]+\.\w+|[\w.-]*[/_][\w./-]*))",
    re.IGNORECASE,
)


def _clean(seam: str) -> str:
    return seam.strip().rstrip(".,;:)(")


def _row_text(row: dict) -> str:
    return " ".join(str(v) for v in row.values())


def unowned_seams(texts: list[str], acceptance_rows: Optional[list[dict]] = None) -> list[dict]:
    """Each `{seam, source}` for a producer output nothing consumes, deduplicated by seam.

    `source` is the matched phrase. Texts are scanned for "nothing feeds X", "X has no
    consumer", "no consumer (row) for X", "unwired X"; an acceptance row that writes,
    emits, produces or ships an artefact no other row's text names is a seam too.
    """
    found: dict[str, dict] = {}

    def add(seam: str, source: str) -> None:
        seam = _clean(seam)
        if seam and seam.lower() not in found:
            found[seam.lower()] = {"seam": seam, "source": " ".join(source.split())}

    for text in texts or []:
        for pat in _SEAM_PATTERNS:
            for m in pat.finditer(str(text or "")):
                add(m.group("seam"), m.group(0))
    rows = [r for r in (acceptance_rows or []) if isinstance(r, dict)]
    for i, row in enumerate(rows):
        for m in _PRODUCES.finditer(str(row.get("statement") or "")):
            seam = _clean(m.group("seam"))
            others = " ".join(_row_text(r) for j, r in enumerate(rows) if j != i).lower()
            if seam and seam.lower() not in others:
                add(seam, m.group(0))
    return list(found.values())

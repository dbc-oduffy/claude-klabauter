"""Refuse an instruction to fall back from the example-retrieval-repo corpus to engine-source reads.

Rule CORPUS-UNREACHABLE-STOPS-NEVER-FALLS-BACK: an unreachable corpus means stop and report
(BLOCKED, memo example-retrieval-repo-em), never read engine source instead. Enforced at `workflow.revise`
and the ask-brief composer (`ask_brief.load_em_brief`); this module is the single predicate.
"""

from __future__ import annotations

import re
from typing import Optional

RULE = "CORPUS-UNREACHABLE-STOPS-NEVER-FALLS-BACK"

_OUTAGE = re.compile(
    r"\b(?:project[- ]?rag|corpus|rag)\b[^.\n]{0,80}?"
    r"\b(?:unreachable|down|econnrefused|drops?|dropped|unavailable|offline|not reachable)\b"
    r"|\b(?:unreachable|down|econnrefused|unavailable|offline)\b[^.\n]{0,40}?"
    r"\b(?:project[- ]?rag|corpus|rag)\b",
    re.IGNORECASE,
)

_FALLBACK = re.compile(
    r"\bfall(?:ing)?[- ]?back\s+(?:to|on)\s+(?:the\s+)?(?:engine|source|direct|reading|grep|raw)"
    r"|\bengine[- ]?source\b"
    r"|\b(?:read|reading|reads|grep|grepping|search|inspect)\s+(?:the\s+)?(?:engine\s+)?source\b"
    r"|\bsource\s+directly\b|\bdirectly\s+from\s+(?:the\s+)?source\b",
    re.IGNORECASE,
)

_NEGATED = re.compile(
    r"\b(?:do\s+not|don'?t|never|must\s+not|without|instead\s+of|no)\s+(?:\w+\s+){0,2}?"
    r"(?:fall(?:ing)?[- ]?back|fallback|read(?:ing)?|grep(?:ping)?)[^.;\n]*",
    re.IGNORECASE,
)


def fallback_refusal(text: Optional[str]) -> Optional[str]:
    """Return a one-line refusal naming RULE when text pairs a corpus outage with a source-read fallback, else None."""
    if not text or not text.strip():
        return None
    if not _OUTAGE.search(text):
        return None
    if not _FALLBACK.search(_NEGATED.sub(" ", text)):
        return None
    return (
        f"{RULE}: corpus outage plus an instruction to read engine source; "
        "stop, report BLOCKED, memo example-retrieval-repo-em."
    )

import re

from .roster import AGENT_IDENTIFIERS, PERSONA_NAMES

_PERSONA_ALT = "|".join(re.escape(p) for p in PERSONA_NAMES)
_AGENT_ALT = "|".join(re.escape(a) for a in AGENT_IDENTIFIERS)

PATTERNS: dict[str, re.Pattern] = {
    # Live source carries a namespaced form (`Review: coordinator:code-reviewer`)
    # and several trailer shapes (`(Finding 3)`, `(F1)`, `(F1, F2)`,
    # `(2026-09-25 F3)`) -- the namespace prefix and each trailer are optional,
    # so the pattern matches on the bare `Review: <roster-token>` alone too.
    "review_colon": re.compile(
        rf"\bReview:\s*(?:[A-Za-z][\w-]*:)?(?:{_AGENT_ALT}|{_PERSONA_ALT})\b"
        rf"(?:\s*\((?:Finding\s*\d+|F\d+(?:\s*,\s*F\d+)*|\d{{4}}-\d{{2}}-\d{{2}}\s+F\d+)\))?",
        re.IGNORECASE,
    ),
    "finding_ref": re.compile(r"\bFinding\s*\d+(?:\s*/\s*\d+)?\b", re.IGNORECASE),
    "persona_fnum": re.compile(rf"\b(?:{_PERSONA_ALT})\s*,?\s*F\d+\b"),
    "per_review": re.compile(
        rf"\bper\s+(?:{_AGENT_ALT}|{_PERSONA_ALT}|review|reviewer)\b", re.IGNORECASE
    ),
    "staff_eng_finding": re.compile(r"\bstaff-eng finding\b", re.IGNORECASE),
    "reviewer_finding": re.compile(r"\breviewer finding\b", re.IGNORECASE),
    "review_corrected": re.compile(r"\bREVIEW-CORRECTED\b", re.IGNORECASE),
    # BREAK-CLASS only counts as attribution when tied to a persona/review
    "break_class_attributed": re.compile(
        rf"\bBREAK-CLASS\b(?=.{{0,40}}(?:{_PERSONA_ALT}|review|finding))"
        rf"|(?:{_PERSONA_ALT}|review|finding).{{0,40}}\bBREAK-CLASS\b",
        re.IGNORECASE,
    ),
    "persona_attributed": re.compile(
        rf"(?:\bper\s+|\(|//\s*|#\s*)(?:{_PERSONA_ALT})\s*[:,—-]"
    ),
}

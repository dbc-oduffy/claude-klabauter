"""coordinator_core.roadmap.post_stamp_clause — refuse an exit-criterion statement that
names a state the review stamp or its terminal cascade itself produces.

Such a clause deadlocks: the stamp needs the judge to say met, the judge cannot see the
state until after the stamp, and the stamp needs the review stamp. Pure in-process regex;
no spawns, no I/O.
"""

from __future__ import annotations

import re
from typing import Optional

# Own-artifact reference: definite or possessive, never "a/any <subject>".
_OWN = r"(?:the|this|its|the plan's(?: own)?)\s+(?:own\s+)?(?:sizing|plan|baton|handoff)"
_VERB = r"(?:reaches|reach|becomes|become|lands as|flips to|is stamped|gets to)"
_STATE = r"(?:shipped|implemented|stamped|closed|continued)"
_REACH = re.compile(rf"\b{_OWN}\b[^.;\n]{{0,30}}?\b{_VERB}\b[^.;\n]{{0,15}}?\b{_STATE}\b", re.IGNORECASE)
_CASCADE = re.compile(r"\bthe\s+(?:deliverable\s+)?cascade\s+fires\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:cannot|can't|never|not|refuses?|until|no longer|without)\b", re.IGNORECASE)
_MACHINERY = re.compile(
    r"\b(?:fixtures?|tests?|records?)\b|\b(?:a|an|any)\s+(?:\w+\s+){0,2}(?:sizing|plan|baton|handoff)\b",
    re.IGNORECASE,
)


def post_stamp_clause(statement: object) -> Optional[str]:
    """Return the offending clause text, or None when the statement is clean."""
    text = " ".join(str(statement or "").split())
    for clause in re.split(r"(?<=[.;])\s+|\s*;\s*", text):
        if _NEGATION.search(clause) or _MACHINERY.search(clause):
            continue
        m = _REACH.search(clause) or _CASCADE.search(clause)
        if m:
            return m.group(0)
    return None


def post_stamp_refusal(statement: object) -> Optional[str]:
    """Terse refusal message, or None when the statement is clean."""
    hit = post_stamp_clause(statement)
    if hit is None:
        return None
    return (
        f"exit criterion names a post-stamp state ({hit!r}); the stamp or its cascade "
        "produces it. Restate as the pre-stamp mechanism: the test that proves the "
        "cascade would fire."
    )


_SUITE_TIER = re.compile(
    r"\b(?:fast|full|broad|whole|entire)(?:[\s-]+test)?[\s-]+(?:tier|suite)s?\b|\btier[\s-]*u\b",
    re.IGNORECASE,
)


def suite_tier_refusal(statement: object) -> Optional[str]:
    """Refusal message when the statement names a suite tier (fast/full/broad tier or suite, tier-U), else None.

    Shared by `sizing-assemble --exit-criterion` and `sizing.accept_exit_criterion`.
    """
    m = _SUITE_TIER.search(" ".join(str(statement or "").split()))
    if m is None:
        return None
    return (
        f"exit criterion names a suite tier ({m.group(0)!r}). "
        "Use: tests covering touched files pass."
    )


_EXIT_HEADING = re.compile(r"^#{1,6}\s+exit criteria\b", re.IGNORECASE)
_ITEM = re.compile(r"^\s{0,3}\d+[.)]\s+")


def exit_criteria_items(plan_text: str) -> list[str]:
    """Each numbered item of the plan body's `## Exit criteria` section, continuation lines joined."""
    items: list[str] = []
    in_section = False
    for line in plan_text.splitlines():
        if line.startswith("#"):
            in_section = bool(_EXIT_HEADING.match(line))
            continue
        if not in_section:
            continue
        if _ITEM.match(line):
            items.append(_ITEM.sub("", line).strip())
        elif items and line.strip() and line[:1].isspace():
            items[-1] += " " + line.strip()
    return items


def _body_refusal(plan_text: str, check) -> Optional[str]:
    for n, item in enumerate(exit_criteria_items(plan_text), 1):
        refusal = check(item)
        if refusal is not None:
            return f"body exit criterion {n}: {refusal}"
    return None


def post_stamp_body_refusal(plan_text: str) -> Optional[str]:
    """Refusal for the first body exit-criteria item naming a post-stamp state, else None."""
    return _body_refusal(plan_text, post_stamp_refusal)


def suite_tier_body_refusal(plan_text: str) -> Optional[str]:
    """Refusal for the first body exit-criteria item naming a suite tier, else None."""
    return _body_refusal(plan_text, suite_tier_refusal)

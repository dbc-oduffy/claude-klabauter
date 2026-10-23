"""Reach-claim types, canonical-form extractor and refusal composer for delivery notes.

Pure, no I/O. A delivery note states "`X` reaches `Y`"; the in-tree judge
(`reach_claims_tree.judge_reach_claims`) decides each claim and this module composes the landing
refusal from the verdicts.

Judge contract (implemented in `reach_claims_tree.py`):
`judge_reach_claims(root: Path, claims, *, landing_paths=(), deleted_paths=(), withheld_paths=())
-> tuple[ReachVerdict, ...]`. The three keyword sets make the scanned file set the tree being
landed, not the index alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

REACH_CLAIM_REFUSED = "reach-claim-unreachable"

REACH_CLAIM_FORM = (
    'State every claim that one code symbol reaches another as "`X` reaches `Y`", both '
    "backticked; the commit checks each against the tree and refuses the landing when the tree "
    "does not support it."
)

REACH_VERDICTS = ("reachable", "unreachable", "undecidable")

_ANCHOR_CAP = 300
_IDENT = r"[A-Za-z_][\w.\-/]*(?::[A-Za-z_][\w.]*)?"
_CLAIM = re.compile(r"`(" + _IDENT + r")`\s+reaches\s+`(" + _IDENT + r")`")
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.DOTALL | re.MULTILINE)
_VERDICTS_HEAD = re.compile(r"^##[ \t]+Verdicts\b.*$", re.MULTILINE)
_NEGATION = re.compile(r"\b(?:not|never|no|cannot|can't|doesn't|don't|isn't|neither|nor)\b", re.I)


@dataclass(frozen=True)
class ReachClaim:
    source: str
    target: str
    anchor: str


@dataclass(frozen=True)
class ReachVerdict:
    claim: ReachClaim
    verdict: str
    why: str


def extract_reach_claims(note_text: str) -> tuple[ReachClaim, ...]:
    """Return each distinct "`X` reaches `Y`" claim in note order.

    Fenced blocks and everything from a `## Verdicts` heading on are ignored, as are claims whose
    line carries a negation before the match.
    """
    head = _VERDICTS_HEAD.search(note_text)
    text = note_text[: head.start()] if head else note_text
    text = _FENCE.sub("", text)
    seen: set[tuple[str, str]] = set()
    out: list[ReachClaim] = []
    for line in text.splitlines():
        for m in _CLAIM.finditer(line):
            if _NEGATION.search(line[: m.start()]):
                continue
            key = (m.group(1), m.group(2))
            if key in seen:
                continue
            seen.add(key)
            out.append(ReachClaim(key[0], key[1], line.strip()[:_ANCHOR_CAP]))
    return tuple(out)


def reach_refusal(verdicts: Iterable[ReachVerdict]) -> Optional[str]:
    """None unless a verdict is `unreachable`; else the refusal naming the first such claim."""
    for v in verdicts:
        if v.verdict == "unreachable":
            return (
                f"{REACH_CLAIM_REFUSED}: `{v.claim.source}` does not reach "
                f"`{v.claim.target}` in the tree: {v.why}. "
                "Correct the change or the note, then re-fire the same sizing."
            )
    return None


def reach_reply(verdicts: Iterable[ReachVerdict]) -> dict:
    vs = tuple(verdicts)
    return {
        "checked": len(vs),
        "unreachable": [v.claim.anchor for v in vs if v.verdict == "unreachable"],
        "undecidable": [v.claim.anchor for v in vs if v.verdict == "undecidable"],
    }

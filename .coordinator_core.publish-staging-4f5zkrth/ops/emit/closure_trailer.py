"""
coordinator_core.ops.emit.closure_trailer — post-extraction normalizer for
already-extracted ``Closes:`` git-trailer values.

Spec: docs/plans/2026-07-17-commit-closure-emission-fact.md, task C1, AC1,
DECISION-2. Input is the list of trailer VALUES already isolated by C3's
git-native extraction (``git log --format=%(trailers:key=Closes,valueonly)``,
house precedent at coverage.py's Session-Id extraction) — this module never
sees raw commit messages/bodies. Because git's own trailer-block parser did
that isolation, prose "closes" substrings in a commit body structurally
cannot reach this function (resolves Patrik F7 — no trailer-block-discipline
bug class to reproduce here).

The recognizer is a table of compiled regex patterns (not a single hardcoded
regex) so PR-link / ``closes #N`` / multi-ID-per-value forms drop in as
future table rows, once opticon confirms the grammar, without touching this
module's public signature (DECISION-2).
"""

from __future__ import annotations

import re

from coordinator_core.tracker_id_grammar import ITEM_ID_BODY

# Each pattern must expose exactly one capturing group yielding the item_id.
# Ordered by specificity; first pattern to match a given value wins.
# Row 1: a bare "<ID>" value, e.g. "RECS-42" (JIRA-shaped tracker ids).
# Row 2 (this pass): a bare sovereign-tracker item.id, e.g.
# "itm-20260818-fix-the-thing-a1b2c3-0123456789ab" (DEC-16 grammar, shared
# with the mint path via `tracker_id_grammar.ITEM_ID_BODY` so the two cannot
# diverge). Future rows (PR-link forms, "closes #N", multi-ID-per-value) are
# additive — append, don't reorder.
_TRAILER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*-\d+)\s*$"),
    re.compile(rf"^\s*({ITEM_ID_BODY})\s*$"),
]


def parse_closure_trailers(trailer_values: list[str]) -> list[str]:
    """Normalize already-extracted ``Closes:`` trailer values into item_ids.

    Pure function: no git, no I/O, no subprocess. ``trailer_values`` is the
    list of trailer VALUES for one or more commits (already isolated by C3's
    git-native extraction, DECISION-2) — never raw commit bodies. Returns the
    recognized item_ids in input order; returns ``[]`` when none of the
    values match any row in the pattern table.
    """
    item_ids: list[str] = []
    for value in trailer_values:
        for pattern in _TRAILER_PATTERNS:
            match = pattern.match(value)
            if match:
                item_ids.append(match.group(1))
                break
    return item_ids

"""Ties `docs/reference/destructive-git-enforcement.md`'s classification
table to the live `guard_roster()` population so a guard added, renamed, or
removed reds this table rather than silently drifting from it.

Spec backlink: docs/plans/2026-09-11-destructive-git-guards-are-action-shaped.md,
chunk C2 (depends on C1's table).

Negative spec: this test does not itself judge whether a given entry's
action-enforcing/syntax-enforcing label is correct -- only that the SET of
`id`s the table classifies is exactly `{e.id for e in guard_roster()}`. A
row count is deliberately never hardcoded here (the doc itself notes the
population is live, not frozen); the comparison is set-equality against
`len(guard_roster())`/the roster's own ids, not a literal number.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.bash_guards.roster import guard_roster

_DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "reference"
    / "destructive-git-enforcement.md"
)

_TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<id>[^|]+?)\s*\|\s*[^|]+?\s*\|\s*"
    r"(?P<classification>action-enforcing|syntax-enforcing)\s*\|"
)


def _classified_ids() -> set[str]:
    text = _DOC_PATH.read_text(encoding="utf-8")
    ids: set[str] = set()
    for line in text.splitlines():
        match = _TABLE_ROW_RE.match(line.strip())
        if not match:
            continue
        ids.add(match.group("id"))
    return ids


def test_classification_table_covers_every_registered_guard() -> None:
    roster_ids = {entry.id for entry in guard_roster()}
    classified_ids = _classified_ids()

    assert classified_ids, "no classification rows parsed from destructive-git-enforcement.md"
    assert classified_ids == roster_ids, (
        "docs/reference/destructive-git-enforcement.md's classification table "
        f"has drifted from guard_roster(): missing={roster_ids - classified_ids} "
        f"extra={classified_ids - roster_ids}"
    )


def test_classification_table_row_count_matches_guard_roster_length() -> None:
    assert len(_classified_ids()) == len(guard_roster())

"""Which major default-path or user-visible review findings lack a clearing answer.

Ledger rows may carry ``severity``, ``scope`` and ``answered_by``. A row with ``severity: major`` and
a scope in ``MAJOR_SCOPES`` is answered only by a fix row (an open/coded spine row). Any other answer,
a disclosure, a PM waiver or a ruling file included, or none, leaves it unanswered. Rows lacking either
tag are not judged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

MAJOR_SCOPES = frozenset({"default-path", "user-visible"})
CLEARING_KINDS = ("fix-row",)
LIVE_DISPOSITIONS = frozenset({"open", "coded"})


def _is_major(row: dict) -> bool:
    return (
        str(row.get("severity") or "").strip().lower() == "major"
        and str(row.get("scope") or "").strip().lower() in MAJOR_SCOPES
    )


def _fix_row_clears(answer: dict, spine_rows: Optional[list]) -> bool:
    ref = answer.get("row")
    if not isinstance(ref, str) or not ref.strip():
        return False
    return any(
        isinstance(r, dict)
        and str(r.get("id") or "").strip() == ref.strip()
        and str(r.get("disposition") or "").strip() in LIVE_DISPOSITIONS
        for r in spine_rows or []
    )


def _answered(row: dict, spine_rows: Optional[list]) -> bool:
    answer = row.get("answered_by")
    return isinstance(answer, dict) and answer.get("kind") == "fix-row" and _fix_row_clears(answer, spine_rows)


def unanswered(rows: list, spine_rows: Optional[list], repo_root: Optional[Path] = None) -> list:
    """The major default-path or user-visible rows in ``rows`` with no clearing answer.

    ``repo_root`` is accepted for caller compatibility and is unused.
    """
    return [
        row
        for row in rows or []
        if isinstance(row, dict) and _is_major(row) and not _answered(row, spine_rows)
    ]

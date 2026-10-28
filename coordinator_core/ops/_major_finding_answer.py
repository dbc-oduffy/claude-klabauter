"""Which major default-path or user-visible review findings lack a clearing answer.

Ledger rows may carry ``severity``, ``scope`` and ``answered_by``. A row with ``severity: major`` and
a scope in ``MAJOR_SCOPES`` is answered only by a fix row (an open/coded spine row) or a G-EM scope
ruling (an existing repo-relative file). Any other answer, a disclosure or a PM waiver included, or
none, leaves it unanswered. Rows lacking either tag are not judged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

MAJOR_SCOPES = frozenset({"default-path", "user-visible"})
CLEARING_KINDS = ("fix-row", "gem-scope-ruling")
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


def _ruling_clears(answer: dict, repo_root: Optional[Path]) -> bool:
    ref = answer.get("ref")
    if repo_root is None or not isinstance(ref, str) or not ref.strip():
        return False
    rel = Path(ref.strip())
    if rel.is_absolute() or rel.anchor or ".." in rel.parts:
        return False
    try:
        return (Path(repo_root) / rel).is_file()
    except OSError:
        return False


def _answered(row: dict, spine_rows: Optional[list], repo_root: Optional[Path]) -> bool:
    answer = row.get("answered_by")
    if not isinstance(answer, dict):
        return False
    kind = answer.get("kind")
    if kind == "fix-row":
        return _fix_row_clears(answer, spine_rows)
    if kind == "gem-scope-ruling":
        return _ruling_clears(answer, repo_root)
    return False


def unanswered(rows: list, spine_rows: Optional[list], repo_root: Optional[Path]) -> list:
    """The major default-path or user-visible rows in ``rows`` with no clearing answer."""
    return [
        row
        for row in rows or []
        if isinstance(row, dict) and _is_major(row) and not _answered(row, spine_rows, repo_root)
    ]

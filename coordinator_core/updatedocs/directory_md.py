"""
coordinator_core.updatedocs.directory_md — DIRECTORY.md count/refresh-date drift.

Purpose: pure detector for the audit's A8 row — whether a `DIRECTORY.md`'s asserted
per-directory file counts and "Last refreshed" date still match disk. Only the
mechanical half: per-directory counts the document explicitly states, and the refresh
date. The per-directory prose summaries are judgment and stay with a model — this
module never generates or rewrites any part of `DIRECTORY.md`.

Negative spec: this module never builds a `GateResult` and never writes to
`DIRECTORY.md` or any other file. An absent or unreadable target raises
`DirectoryMdUnavailable` — the gate layer (`coordinator_core.ops.updatedocs_gates`)
is the only place that maps that to UNAVAILABLE; collapsing "could not check" into
"found nothing" is exactly the defect the mechanization-boundary audit found failing
at nine of ten sites.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C2)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_LAST_REFRESHED_RE = re.compile(r"Last refreshed:\s*(\d{4}-\d{2}-\d{2})")

# Matches assertions of the shape "19 `conftest.py`/test-support files across the
# tree." — a leading integer, a backtick-quoted filename, then a "files" mention
# somewhere later on the same line. Only counts the document explicitly states are
# parsed; nothing here infers a count the document does not claim.
_COUNT_ASSERTION_RE = re.compile(r"(\d+)\s+`([\w.]+\.\w+)`[^\n]*?\bfiles\b")


class DirectoryMdUnavailable(Exception):
    """Raised when the target DIRECTORY.md-shaped file cannot be read.

    Carries the missing/unreadable path so the caller (a gate function) can convert
    this into an UNAVAILABLE verdict rather than swallowing it into a clean result.
    """

    def __init__(self, missing_path: Path) -> None:
        self.missing_path = missing_path
        # `path` retained as an alias: the attribute name across all four
        # sibling errors in this package is `missing_path`, and the gate layer
        # reads that one uniformly.
        self.path = missing_path
        super().__init__(
            f"DIRECTORY.md-shaped file not found or unreadable: {missing_path}"
        )


@dataclass(frozen=True)
class CountClaim:
    claim_site: str
    asserted: int
    actual: int
    matches: bool


@dataclass(frozen=True)
class DirectoryMdDrift:
    directory_md_path: Path
    refreshed_on: date | None
    age_days: int | None
    count_claims: list[CountClaim] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        if self.refreshed_on is None:
            return True
        return any(not claim.matches for claim in self.count_claims)


def _parse_refreshed_on(text: str) -> date | None:
    match = _LAST_REFRESHED_RE.search(text)
    if match is None:
        return None
    try:
        year, month, day = (int(part) for part in match.group(1).split("-"))
        return date(year, month, day)
    except ValueError:
        return None


def _parse_count_claims(text: str, corpus_root: Path) -> list[CountClaim]:
    claims: list[CountClaim] = []
    for match in _COUNT_ASSERTION_RE.finditer(text):
        asserted = int(match.group(1))
        filename = match.group(2)
        claim_site = match.group(0).strip()
        actual = sum(1 for _ in corpus_root.rglob(filename))
        claims.append(
            CountClaim(
                claim_site=claim_site,
                asserted=asserted,
                actual=actual,
                matches=(asserted == actual),
            )
        )
    return claims


def compute_directory_md_drift(directory_md_path: Path) -> DirectoryMdDrift:
    """Detect count and refresh-date drift in a DIRECTORY.md-shaped document.

    Parameters
    ----------
    directory_md_path:
        Path to the DIRECTORY.md-shaped file to check. Per-directory file counts are
        resolved relative to this file's parent directory (the corpus the document
        describes), never a hardcoded root.

    Raises
    ------
    DirectoryMdUnavailable
        If `directory_md_path` does not exist or cannot be read.
    """
    directory_md_path = Path(directory_md_path)
    if not directory_md_path.is_file():
        raise DirectoryMdUnavailable(directory_md_path)

    try:
        text = directory_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DirectoryMdUnavailable(directory_md_path) from exc

    refreshed_on = _parse_refreshed_on(text)
    age_days = (date.today() - refreshed_on).days if refreshed_on is not None else None

    corpus_root = directory_md_path.parent
    count_claims = _parse_count_claims(text, corpus_root)

    return DirectoryMdDrift(
        directory_md_path=directory_md_path,
        refreshed_on=refreshed_on,
        age_days=age_days,
        count_claims=count_claims,
    )

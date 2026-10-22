"""Contract constants and shapes for fleet.migrate_prose_queue (prose queue -> state/<family>/*.yaml)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

FAMILIES: Tuple[str, ...] = ("improvement-queue", "bug-backlog", "debt-backlog")

OUTCOME_PARSED = "parsed"
OUTCOME_FILE_UNPARSEABLE = "file-unparseable"
OUTCOME_SOURCE_ABSENT = "source-absent"
FILE_OUTCOMES: Tuple[str, ...] = (OUTCOME_PARSED, OUTCOME_FILE_UNPARSEABLE, OUTCOME_SOURCE_ABSENT)

BUCKET_TO_MIGRATE = "to-migrate"
BUCKET_AMBIGUOUS = "ambiguous"
BUCKET_MIGRATED_INCOMPLETE = "migrated-incomplete"
BUCKET_PARSE_ERROR = "unmigrated-parse-error"
BUCKET_UNIVERSAL_LEFTOVER = "unmigrated-universal-leftover"
BUCKET_SKIPPED = "skipped"

# Evaluation order per candidate entry; the order is part of the contract.
SKIP_REASONS: Tuple[str, ...] = (
    "closed-in-source",
    "already-migrated",
    "duplicate-source-segment",
    "title-match-unmarked",
)

NOT_RECORDED = "(not recorded in legacy source)"
MIGRATED_INCOMPLETE_TAG = "migrated-incomplete"

CLOSURE_TOKEN_RE = re.compile(r"^\**(CLOSED|RESOLVED|FIXED|DONE)\b\**", re.IGNORECASE)
STATUS_CLOSED_RE = re.compile(r"Status:\s*CLOSED", re.IGNORECASE)

# Max example rows reported per non-converting bucket.
EXAMPLE_CAP = 20


@dataclass
class RawRow:
    """One recognised source row: grammar, 1-based inclusive line span, verbatim segment, cells.

    cells is keyed by header name (table grammar) or by position index as str (pipe grammars).
    """

    grammar: str
    line_start: int
    line_end: int
    segment: str
    cells: Dict[str, str] = field(default_factory=dict)


@dataclass
class FileParse:
    """parse_file result: rows empty and outcome file-unparseable when no grammar recognises the file."""

    outcome: str
    rows: List[RawRow] = field(default_factory=list)
    schema_line: Optional[str] = None


@dataclass
class CandidateEntry:
    """A RawRow mapped into schema fields, with self-flags and the bucket the fill rule chose."""

    fields: Dict[str, object]
    flags: List[str] = field(default_factory=list)
    bucket_hint: str = BUCKET_TO_MIGRATE
    source_row: Optional[RawRow] = None


# Pipeline signatures:
#   _prose_queue_grammars.parse_file(text: str) -> FileParse
#   _prose_queue_fields.fill_fields(family: str, row: RawRow) -> CandidateEntry
#   _prose_queue_run.run_migration(repo_root: Path, families: list[str], dry_run: bool) -> dict
#
# Report shape of run_migration:
#   {"dry_run": bool,
#    "files": {family: {"outcome": FILE_OUTCOMES member,
#                       "to-migrate": int, "ambiguous": int, "migrated-incomplete": int,
#                       "unmigrated-parse-error": int, "unmigrated-universal-leftover": int,
#                       "skipped": {reason: int},
#                       "examples": {bucket: [row, ...]}  # capped at EXAMPLE_CAP
#                       }}}

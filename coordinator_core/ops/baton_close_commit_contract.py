"""Declarations for the mutating `baton.close_commit` op: op key, refusal enum, result shapes.

Pure declarations, no I/O. A row is `baton.close_report`'s closable item verbatim.
The op never commits: the caller lands `pathspec` (repo-relative POSIX paths this call
rewrote, sorted, deduplicated) with `commit_subject` (null exactly when pathspec is empty)
as one scoped commit.
"""

from enum import Enum
from typing import TypedDict

from coordinator_core.ops.baton_close_report_contract import ClosableItem, Evidence

OP_KEY = "baton.close_commit"
SCHEMA_VERSION = 1

CommitRow = ClosableItem


class RefusalReason(str, Enum):
    BAD_ROW = "bad_row"
    NOT_LIVE = "not_live"
    UNREADABLE_FRONTMATTER = "unreadable_frontmatter"
    CLAIMED = "claimed"
    CONTINUED = "continued"
    LIVE_SUCCESSOR = "live_successor"
    EVIDENCE_STALE = "evidence_stale"
    PREDICATE_REFUSED = "predicate_refused"
    NO_DELIVERABLE_ID = "no_deliverable_id"
    NO_EVIDENCE_SHA = "no_evidence_sha"
    WRITE_FAILED = "write_failed"


class FlipRequest(TypedDict):
    """The check-to-write hand-off: one baton to make terminal."""

    path: str
    deliverable_id: str
    shipped_in: str


class AppliedItem(TypedDict):
    handoff_path: str
    advanced_by: str
    advanced_at: str
    shipped_in: str


class AlreadyTerminalItem(TypedDict):
    handoff_path: str


class RefusedItem(TypedDict):
    handoff_path: str
    reason: str
    detail: str
    evidence: list[Evidence]


class CommitResult(TypedDict):
    exit_code: int
    schema_version: int
    op: str
    rows_received: int
    applied: list[AppliedItem]
    already_terminal: list[AlreadyTerminalItem]
    refused: list[RefusedItem]
    pathspec: list[str]
    commit_subject: str | None


RESULT_KEYS = (
    "exit_code",
    "schema_version",
    "op",
    "rows_received",
    "applied",
    "already_terminal",
    "refused",
    "pathspec",
    "commit_subject",
)
APPLIED_KEYS = ("handoff_path", "advanced_by", "advanced_at", "shipped_in")
ALREADY_TERMINAL_KEYS = ("handoff_path",)
REFUSED_KEYS = ("handoff_path", "reason", "detail", "evidence")
FLIP_REQUEST_KEYS = ("path", "deliverable_id", "shipped_in")

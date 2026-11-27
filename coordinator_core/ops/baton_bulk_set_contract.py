"""Declarations for the mutating `baton.bulk_set` op: op key, refusal enum, item and result shapes.

Pure declarations, no I/O. One call stamps `owner_session` and `eta` on many batons and may close
a baton on explicit evidence; `eta` is an ISO-8601 date or datetime string. The op lands every
change it makes as one commit: `commit_sha` is null exactly when nothing was written (dry_run,
or every item unchanged or refused).
"""

from enum import Enum
from typing import NotRequired, TypedDict

OP_KEY = "baton.bulk_set"
SCHEMA_VERSION = 1

CLOSE_VERDICTS = ("shipped",)


class RefusalReason(str, Enum):
    NOT_FOUND = "not_found"
    UNREADABLE_FRONTMATTER = "unreadable_frontmatter"
    BAD_ETA = "bad_eta"
    BAD_OWNER = "bad_owner"
    CLOSE_WITHOUT_EVIDENCE = "close_without_evidence"
    BAD_EVIDENCE = "bad_evidence"
    MISSING_DELIVERABLE_ID = "missing_deliverable_id"
    WRITE_FAILED = "write_failed"


class Close(TypedDict):
    """A terminal close request: `verdict` admits only "shipped"; `evidence` is the 40-hex sha."""

    verdict: str
    reason: str
    evidence: str


class Item(TypedDict):
    path: str
    owner_session: str
    eta: str
    close: NotRequired[Close]


class AppliedItem(TypedDict):
    handoff_path: str
    owner_session: str
    eta: str
    closed: bool


class UnchangedItem(TypedDict):
    handoff_path: str
    owner_session: str
    eta: str


class RefusedItem(TypedDict):
    handoff_path: str
    reason: str
    detail: str


class BulkSetResult(TypedDict):
    exit_code: int
    schema_version: int
    op: str
    applied: list[AppliedItem]
    unchanged: list[UnchangedItem]
    refused: list[RefusedItem]
    dry_run: bool
    commit_sha: str | None
    pathspec: list[str]


RESULT_KEYS = (
    "exit_code",
    "schema_version",
    "op",
    "applied",
    "unchanged",
    "refused",
    "dry_run",
    "commit_sha",
    "pathspec",
)
ITEM_KEYS = ("path", "owner_session", "eta", "close")
CLOSE_KEYS = ("verdict", "reason", "evidence")
APPLIED_KEYS = ("handoff_path", "owner_session", "eta", "closed")
UNCHANGED_KEYS = ("handoff_path", "owner_session", "eta")
REFUSED_KEYS = ("handoff_path", "reason", "detail")

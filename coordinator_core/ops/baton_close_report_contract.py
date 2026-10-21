"""Declarations for the read-only `baton.close_report` op: op key, enums, record shapes.

Pure declarations, no I/O. Readers, target readers, the decision function and the handler
all build against these names.
"""

from __future__ import annotations

from enum import Enum
from typing import TypedDict

OP_KEY = "baton.close_report"
SCHEMA_VERSION = 1


class EvidenceClass(str, Enum):
    PLAN = "plan"
    SIZING = "sizing"
    ROADMAP = "roadmap"
    GOAL = "goal"
    SUCCESSOR = "successor"


class ResidueReason(str, Enum):
    TARGET_UNREADABLE = "target_unreadable"
    TARGET_MISSING = "target_missing"
    LIVE_SUCCESSOR = "live_successor"
    CLAIMED = "claimed"
    EVIDENCE_CONFLICT = "evidence_conflict"
    UNREADABLE_FRONTMATTER = "unreadable_frontmatter"


class Verdict(str, Enum):
    CLOSABLE = "closable"
    RESIDUE = "residue"
    NO_EVIDENCE = "no_evidence"


# Functional form: "class" is a keyword and cannot be a class-body attribute.
Evidence = TypedDict(
    "Evidence",
    {
        "class": str,
        "field": str,
        "target_path": str,
        "target_status": str | None,
        "done": bool,
    },
)


class BatonHeader(TypedDict):
    """Keys extracted from one baton's frontmatter; absent keys are None or empty."""

    path: str
    status: str
    predecessor: str | None
    governing_plan: str | None
    origin_plan_id: str | None
    sizing_object: str | None
    roadmap_id: str | None
    origin_goal_id: list[str]


class ClosableItem(TypedDict):
    handoff_path: str
    evidence: list[Evidence]


class ResidueItem(TypedDict):
    handoff_path: str
    reason: str
    evidence: list[Evidence]


class CloseReport(TypedDict):
    exit_code: int
    schema_version: int
    op: str
    batons_scanned: int
    no_evidence: int
    closable: list[ClosableItem]
    residue: list[ResidueItem]
    scan_incomplete: bool


EVIDENCE_KEYS = ("class", "field", "target_path", "target_status", "done")
HEADER_KEYS = (
    "path",
    "status",
    "predecessor",
    "governing_plan",
    "origin_plan_id",
    "sizing_object",
    "roadmap_id",
    "origin_goal_id",
)
REPORT_KEYS = (
    "exit_code",
    "schema_version",
    "op",
    "batons_scanned",
    "no_evidence",
    "closable",
    "residue",
    "scan_incomplete",
)

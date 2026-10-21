"""Declarations for the read-only `baton.close_report` op: op key, enums, record shapes.

Pure declarations, no I/O. Readers, target readers, the decision function and the handler
all build against these names.

Run evidence is produced by the spawn-free reader in baton_close_report_readers:
`run_claims(worktree_root: str, headers: dict[str, BatonHeader]) -> dict[str, list[Evidence]]`,
keyed by baton path.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, TypedDict

OP_KEY = "baton.close_report"
SCHEMA_VERSION = 1


class EvidenceClass(str, Enum):
    PLAN = "plan"
    SIZING = "sizing"
    ROADMAP = "roadmap"
    GOAL = "goal"
    SUCCESSOR = "successor"
    RUN = "run"


class ResidueReason(str, Enum):
    TARGET_UNREADABLE = "target_unreadable"
    # Cited file was read but carries no top-level `status:` line (e.g. a pre-frontmatter
    # legacy plan); distinct from TARGET_UNREADABLE, where the file could not be read.
    TARGET_UNSTATUSED = "target_unstatused"
    TARGET_MISSING = "target_missing"
    LIVE_SUCCESSOR = "live_successor"
    CLAIMED = "claimed"
    EVIDENCE_CONFLICT = "evidence_conflict"
    UNREADABLE_FRONTMATTER = "unreadable_frontmatter"
    # Mirrors close_commit's leg (d) predicate_refused: a spinoff's deliverable_id is not its parent's.
    SPINOFF_KIND = "spinoff_kind"
    # Mirrors close_commit's no_deliverable_id refusal: no dlv- join key for advanced_by.
    NO_DELIVERABLE_ID = "no_deliverable_id"


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
    deliverable_id: str | None
    kind: str | None


class ConflictNote(TypedDict):
    """Done and contradicting evidence that coexist, and which side governs."""

    done: list[Evidence]
    contradicting: list[Evidence]
    winner: Literal["done", "contradicting"]  # CONFLICT_WINNERS
    rule: str


class ClosableItem(TypedDict):
    handoff_path: str
    evidence: list[Evidence]
    conflict: ConflictNote | None


class ResidueItem(TypedDict):
    handoff_path: str
    reason: str
    evidence: list[Evidence]
    conflict: ConflictNote | None


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
    "deliverable_id",
    "kind",
)
# Evidence `field` values for plan-to-baton (reverse) links.
REVERSE_FIELD_DELIVERABLE = "plan.deliverable_id"
REVERSE_FIELD_PREDECESSOR = "plan.predecessor_handoff"
REVERSE_FIELD_SIZING = "plan.sizing_object"
# Conflict rule: a done governing_plan outranks an inferred sizing-join leg.
CONFLICT_RULE_OWN_PLAN = "the baton's own governing_plan is done; a sizing-join leg is inferred and yields to it"
# Run-evidence `field` values: how a run was joined to the baton.
RUN_FIELD_BATON = "run.baton"
RUN_FIELD_SIZING = "run.sizing_rel"
RUN_FIELD_CLAIMED = "run.claimed_batons"
# Run-evidence `target_status` values.
RUN_COMMITTED = "committed"
RUN_ENDED = "ended"
RUN_LIVE = "live"
CONFLICT_WINNERS = ("done", "contradicting")
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

"""
Goal — the one net-new WRITABLE entity in the cockpit, modelled as an
append-only event record (the Data Science Reviewer P1-D6, R3). Pydantic port of DoE
`coordinator/cockpit-contract/src/entities/goal.ts` (Zod source).

Goals are mutable, low-frequency, single-author-per-edit but MULTI-MACHINE
(Machine-c + Machine-a) — the worst shape for "just put it in a file and
git-merge it." Append-only per-machine event records sidestep the
last-write-wins / merge-conflict hazard entirely and give goal history for
free. A goal is never mutated in place; it is superseded by a newer record.
Current goals are derived as the latest non-superseded record per
`(repo, coordinator_root_path, period, period_value)`.

R3: goals are DECLARED (not inferred), captured via enhanced existing
ceremonies (workweek-start, workday-start, HEADER.md per-repo) — tc-3 owns the
write; this contract owns the shape.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

GoalPeriod = Literal["day", "week", "repo", "quarter", "year"]

GoalStatus = Literal["active", "done", "superseded", "dropped"]


class GoalKeyResultStatus(BaseModel):
    """Per-KR status projected onto the wire (2.13.0, D24)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    kind: str
    status: str


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str
    """UUID or deterministic slug."""
    repo: str = Field(
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor. "
            "Empty string '' is the cross-repo-aggregate sentinel."
        )
    )
    """"" for cross-repo / global goals."""
    coordinator_root_path: str
    """Default "."; joins with the (repo, coordinator_root_path) keying used everywhere else."""
    period: GoalPeriod
    period_value: str
    """ISO date (YYYY-MM-DD) for day; ISO week (YYYY-Www) for week; "ongoing" for repo; ISO quarter (YYYY-Qn) for quarter; ISO year (YYYY) for year."""
    declared_by_machine: str
    """Which machine declared it — the append-only event's author."""
    declared_at: IsoDateTime
    text: str
    status: GoalStatus
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
    weekly_perceptible: bool | None = None
    """
    Scopes cockpit's CEO/weekly view to goals meant for that surface. Version-neutral
    optional — absent on all existing records (2.13.0, D24).
    """
    # present-as-null (no default — see provenance.py module docstring for the D9 gotcha).
    parent_goal_id: str | None
    """
    Weekly→OKR ladder edge — the parent goal this goal rolls up to. D9:
    present-as-null (never optional) — the key MUST be emitted, carrying `null`
    when this goal has no parent. Added 2.13.0 (D24).
    """
    key_results_status: list[GoalKeyResultStatus] | None = None
    """
    Per-KR status projected onto the wire (2.13.0, D24). Version-neutral optional —
    absent on all existing records.
    """

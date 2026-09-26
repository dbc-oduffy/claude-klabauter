"""
SnapshotEnvelope — the top-level shape of every emitted cockpit snapshot.
Pydantic port of DoE `coordinator/cockpit-contract/src/entities/snapshot-envelope.ts`
(Zod source).

Spec backlink: state/roadmap/cockpit-contract-ext-2026-06-22/COORDINATOR-RESOLUTIONS.md
§ R5 (the Staff Engineer F0): formalize the ad-hoc jq-assembled top-level object
(originally bin/emit-cockpit-snapshot.sh, since ported to claude-klabauter's
Python `artifact.emit` — DR-208/DR-210, the sole production emitter as of
2026-07-08) as a governed schema so adding new keys (`plans`, `lessons`) is a
governed schema change, not silent shape drift.

This is the breaking surface that justifies the 1.0 major bump (R1 amendment,
F7). Claude-klabauter's `artifact.emit` assembles this object; tc-6 / MCP / cockpit
consume it as the parse root.

Arrays new in 1.0 (`plans`, `lessons`) are present-but-can-be-empty (D9) so
the emitter can land them as `[]` until tc-2/tc-3 wire them up.

Arrays new in 2.1.0 (`roadmaps`, `trackers`, `health`) follow the same
additive pattern (D9) — present-but-empty until B3 emitter wires them up.

Arrays new in 2.16.0 (`competitor_summaries`, `intelligence_signals`) follow
the same additive present-but-empty (D9) pattern.

Nullability discipline: D9 — present-as-null for singular slots
(narrative_views); fleet arrays (`coordinator_roots`, `branches`) are
required and default to `[]`, never null. All other required arrays are
similarly never null (default to []).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDateTime
from coordinator_core.contract.cockpit_schema.entities.backlog_history import BacklogHistory
from coordinator_core.contract.cockpit_schema.entities.branch import Branch
from coordinator_core.contract.cockpit_schema.entities.competitor_summary import CompetitorSummary
from coordinator_core.contract.cockpit_schema.entities.coordinator_root import CoordinatorRoot
from coordinator_core.contract.cockpit_schema.entities.cross_repo_memo_summary import (
    CrossRepoMemoSummary,
)
from coordinator_core.contract.cockpit_schema.entities.decision_guide_summary import (
    DecisionGuideSummary,
)
from coordinator_core.contract.cockpit_schema.entities.exec_summary import ExecSummary
from coordinator_core.contract.cockpit_schema.entities.goal import Goal
from coordinator_core.contract.cockpit_schema.entities.health_status_summary import (
    HealthStatusSummary,
)
from coordinator_core.contract.cockpit_schema.entities.initiative_summary import InitiativeSummary
from coordinator_core.contract.cockpit_schema.entities.intelligence_signal import (
    IntelligenceSignal,
)
from coordinator_core.contract.cockpit_schema.entities.lesson_summary import LessonSummary
from coordinator_core.contract.cockpit_schema.entities.plan_summary import PlanSummary
from coordinator_core.contract.cockpit_schema.entities.roadmap_dag_edge import RoadmapDagEdge
from coordinator_core.contract.cockpit_schema.entities.roadmap_dag_node import RoadmapDagNode
from coordinator_core.contract.cockpit_schema.entities.roadmap_summary import RoadmapSummary
from coordinator_core.contract.cockpit_schema.entities.rollup import DayRollup, WeekRollup
from coordinator_core.contract.cockpit_schema.entities.routine_signal import RoutineSignal
from coordinator_core.contract.cockpit_schema.entities.session_hierarchy import SessionHierarchy
from coordinator_core.contract.cockpit_schema.entities.summaries import (
    BacklogItemSummary,
    HandoffSummary,
    ReviewTrail,
)
from coordinator_core.contract.cockpit_schema.entities.tracker_summary import TrackerSummary


class MalformedRecords(BaseModel):

    model_config = ConfigDict(extra="forbid")

    handoffs: list[dict[str, Any]]
    backlogs: list[dict[str, Any]]
    review_trail: list[dict[str, Any]]
    coordinator_roots: list[dict[str, Any]]
    plans: list[dict[str, Any]]
    lessons: list[dict[str, Any]]
    cross_repo_memos: list[dict[str, Any]]
    roadmaps: list[dict[str, Any]]
    trackers: list[dict[str, Any]]
    health: list[dict[str, Any]]
    decision_guides: list[dict[str, Any]]
    session_hierarchies: list[dict[str, Any]]
    initiatives: list[dict[str, Any]]
    exec_summaries: list[dict[str, Any]]
    roadmap_dag_nodes: list[dict[str, Any]]
    roadmap_dag_edges: list[dict[str, Any]]
    competitor_summaries: list[dict[str, Any]]
    intelligence_signals: list[dict[str, Any]]


class BacklogsEnvelope(BaseModel):

    model_config = ConfigDict(extra="forbid")

    bug: list[BacklogItemSummary]
    debt: list[BacklogItemSummary]
    improvement: list[BacklogItemSummary]


class CompletionRollups(BaseModel):

    model_config = ConfigDict(extra="forbid")

    day: list[DayRollup]
    week: list[WeekRollup]


class SnapshotEnvelope(BaseModel):

    model_config = ConfigDict(extra="forbid")

    # Sourced from CONTRACT_VERSION via the emitted bundle .version (the Staff Engineer
    # F1). Must be a semver string matching the exported CONTRACT_VERSION
    # ties schema_version to CONTRACT_VERSION shape.
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    # ISO-8601 UTC wall-clock when the snapshot was assembled.
    emitted_at: IsoDateTime
    emitted_by_machine: str
    coordinator_roots: list[CoordinatorRoot]
    branches: list[Branch]
    handoffs: list[HandoffSummary]
    completion_rollups: CompletionRollups
    backlogs: BacklogsEnvelope
    review_trail: list[ReviewTrail]
    routine_signals: list[RoutineSignal]
    goals_current: list[Goal]
    plans: list[PlanSummary]
    lessons: list[LessonSummary]
    cross_repo_memos: list[CrossRepoMemoSummary]
    roadmaps: list[RoadmapSummary]
    trackers: list[TrackerSummary]
    health: list[HealthStatusSummary]
    decision_guides: list[DecisionGuideSummary]
    session_hierarchies: list[SessionHierarchy]
    initiatives: list[InitiativeSummary]
    exec_summaries: list[ExecSummary]
    competitor_summaries: list[CompetitorSummary]
    intelligence_signals: list[IntelligenceSignal]
    roadmap_dag_nodes: list[RoadmapDagNode]
    roadmap_dag_edges: list[RoadmapDagEdge]
    backlog_history: BacklogHistory
    narrative_views: Any | None
    malformed_records: MalformedRecords

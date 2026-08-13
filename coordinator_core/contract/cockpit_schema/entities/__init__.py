"""
entities — the 24 cockpit-contract entity modules, aggregated.

Pydantic port of coordinator-claude `coordinator/cockpit-contract/src/index.ts`'s import +
re-export block (everything in that file EXCEPT the `ENTITY_SCHEMAS` map and
`CONTRACT_VERSION`, which live one level up at
`coordinator_core/contract/cockpit_schema/__init__.py` — the package-level
surface `emit_schema.py` imports `ENTITY_SCHEMAS` from). This module exists
so both that package `__init__.py` and any direct consumer can do
`from coordinator_core.contract.cockpit_schema.entities import Branch, Goal, ...`
without knowing the snake_case module each class actually lives in — same
flattening `index.ts`'s barrel re-export gives TS callers.

`deliverable_spine` (`WorkstreamType`, `DeliverableStatus`) is re-exported
here too, matching `index.ts`'s `export * from "./entities/deliverable-spine.js"`
— it carries no `ENTITY_SCHEMAS` entry (shared enums only, not an emittable
entity), same as the TS source.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
Negative-spec: no `ENTITY_SCHEMAS` / `CONTRACT_VERSION` here — those are
package-level (see above), not entities-level, to match where `emit_schema.py`
looks them up.
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema.entities.backlog_history import (
    BacklogHistory,
    DailyPoint,
    RepoSeries,
)
from coordinator_core.contract.cockpit_schema.entities.branch import Branch
from coordinator_core.contract.cockpit_schema.entities.competitor_summary import (
    CompetitorSummary,
)
from coordinator_core.contract.cockpit_schema.entities.coordinator_root import (
    CoordinatorRoot,
)
from coordinator_core.contract.cockpit_schema.entities.cross_repo_memo_summary import (
    CrossRepoMemoSummary,
)
from coordinator_core.contract.cockpit_schema.entities.decision_guide_summary import (
    DecisionGuideSummary,
)
from coordinator_core.contract.cockpit_schema.entities.deliverable_spine import (
    DeliverableStatus,
    WorkstreamType,
)
from coordinator_core.contract.cockpit_schema.entities.exec_summary import ExecSummary
from coordinator_core.contract.cockpit_schema.entities.file_attribution import (
    FileAttribution,
)
from coordinator_core.contract.cockpit_schema.entities.financial_metric_summary import (
    FinancialMetricSummary,
)
from coordinator_core.contract.cockpit_schema.entities.goal import (
    Goal,
    GoalKeyResultStatus,
)
from coordinator_core.contract.cockpit_schema.entities.health_status_summary import (
    HealthStatusSummary,
)
from coordinator_core.contract.cockpit_schema.entities.initiative_summary import (
    InitiativeSummary,
)
from coordinator_core.contract.cockpit_schema.entities.intelligence_signal import (
    IntelligenceSignal,
)
from coordinator_core.contract.cockpit_schema.entities.lesson_summary import (
    LessonSummary,
)
from coordinator_core.contract.cockpit_schema.entities.plan_summary import (
    PlanSummary,
)
from coordinator_core.contract.cockpit_schema.entities.priority_ledger_entry import (
    PriorityLedgerEntry,
)
from coordinator_core.contract.cockpit_schema.entities.roadmap_dag_edge import (
    RoadmapDagEdge,
)
from coordinator_core.contract.cockpit_schema.entities.roadmap_dag_node import (
    RoadmapDagNode,
)
from coordinator_core.contract.cockpit_schema.entities.roadmap_summary import (
    RoadmapSummary,
)
from coordinator_core.contract.cockpit_schema.entities.rollup import (
    DayRollup,
    RollupNarrative,
    RollupWatermark,
    WeekRollup,
)
from coordinator_core.contract.cockpit_schema.entities.routine_signal import (
    RoutineSignal,
)
from coordinator_core.contract.cockpit_schema.entities.session_hierarchy import (
    SessionHierarchy,
)
from coordinator_core.contract.cockpit_schema.entities.snapshot_envelope import (
    BacklogsEnvelope,
    CompletionRollups,
    MalformedRecords,
    SnapshotEnvelope,
)
from coordinator_core.contract.cockpit_schema.entities.summaries import (
    BacklogItemSummary,
    ForeignOriginTriple,
    HandoffSummary,
    ReviewTrail,
)
from coordinator_core.contract.cockpit_schema.entities.tracker_summary import (
    TrackerSummary,
)

__all__ = [
    "BacklogHistory",
    "DailyPoint",
    "RepoSeries",
    "Branch",
    "CompetitorSummary",
    "CoordinatorRoot",
    "CrossRepoMemoSummary",
    "DecisionGuideSummary",
    "DeliverableStatus",
    "WorkstreamType",
    "ExecSummary",
    "FileAttribution",
    "FinancialMetricSummary",
    "Goal",
    "GoalKeyResultStatus",
    "HealthStatusSummary",
    "InitiativeSummary",
    "IntelligenceSignal",
    "LessonSummary",
    "PlanSummary",
    "PriorityLedgerEntry",
    "RoadmapDagEdge",
    "RoadmapDagNode",
    "RoadmapSummary",
    "DayRollup",
    "RollupNarrative",
    "RollupWatermark",
    "WeekRollup",
    "RoutineSignal",
    "SessionHierarchy",
    "BacklogsEnvelope",
    "CompletionRollups",
    "MalformedRecords",
    "SnapshotEnvelope",
    "BacklogItemSummary",
    "ForeignOriginTriple",
    "HandoffSummary",
    "ReviewTrail",
    "TrackerSummary",
]

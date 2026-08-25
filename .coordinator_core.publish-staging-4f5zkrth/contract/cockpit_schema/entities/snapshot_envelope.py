"""
SnapshotEnvelope — the top-level shape of every emitted cockpit snapshot.
Pydantic port of DoE `coordinator/cockpit-contract/src/entities/snapshot-envelope.ts`
(Zod source).

Spec backlink: state/roadmap/cockpit-contract-ext-2026-06-22/COORDINATOR-RESOLUTIONS.md
§ R5 (Patrik F0): formalize the ad-hoc jq-assembled top-level object
(originally bin/emit-cockpit-snapshot.sh, since ported to makima's
Python `artifact.emit` — DR-208/DR-210, the sole production emitter as of
2026-07-08) as a governed schema so adding new keys (`plans`, `lessons`) is a
governed schema change, not silent shape drift.

This is the breaking surface that justifies the 1.0 major bump (R1 amendment,
F7). makima's `artifact.emit` assembles this object; tc-6 / MCP / opticon
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
from coordinator_core.contract.cockpit_schema.entities.file_attribution import FileAttribution
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
    """
    Malformed-record buckets — each bucket carries the raw JSON that failed
    parse. `plans` and `lessons` buckets added in 1.0; tc-3/tc-4 emit wiring
    requires quarantine slots or failed-record draining silently discards new
    entity arrays. `cross_repo_memos` bucket added in 1.0 for quarantine-slot
    consistency (C6-envelope).

    Sub-component of SnapshotEnvelope — NOT registered in the entity registry
    (not a top-level entity; no standalone fixture/schema emit required).
    Review: code-reviewer (F13).
    """

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
    file_attributions: list[dict[str, Any]]
    initiatives: list[dict[str, Any]]
    exec_summaries: list[dict[str, Any]]
    roadmap_dag_nodes: list[dict[str, Any]]
    roadmap_dag_edges: list[dict[str, Any]]
    competitor_summaries: list[dict[str, Any]]
    intelligence_signals: list[dict[str, Any]]


class BacklogsEnvelope(BaseModel):
    """
    The nested backlogs object, one array per queue type.
    Sub-component of SnapshotEnvelope — NOT registered in the entity registry
    (not a top-level entity; no standalone fixture/schema emit required).
    Review: code-reviewer (F13).
    """

    model_config = ConfigDict(extra="forbid")

    bug: list[BacklogItemSummary]
    debt: list[BacklogItemSummary]
    improvement: list[BacklogItemSummary]


class CompletionRollups(BaseModel):
    """
    Period-keyed completion rollup arrays (C5 — Patrik F0 resolution).

    Replaces the old single-object form (`{ week: WeekRollup | null, day:
    DayRollup | null }`). Keyed-object-of-arrays sidesteps the
    DayRollup/WeekRollup union discrimination footgun (structurally identical
    discriminant-less shapes confuse Zod union parsing). Each period bucket is
    an array so the snapshot can carry multiple rollup windows (e.g. current +
    previous week) as queryable entries rather than a single latest-slot.

    tc-3 emitter MUST emit the nested plural shape — not the old singular
    `completion_rollup`.
    """

    model_config = ConfigDict(extra="forbid")

    day: list[DayRollup]
    week: list[WeekRollup]


class SnapshotEnvelope(BaseModel):
    """The top-level shape of every emitted cockpit snapshot."""

    model_config = ConfigDict(extra="forbid")

    # Sourced from CONTRACT_VERSION via the emitted bundle .version (Patrik
    # F1). Must be a semver string matching the exported CONTRACT_VERSION
    # constant.
    # Review: code-reviewer — semver regex prevents silent version drift;
    # ties schema_version to CONTRACT_VERSION shape.
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    # ISO-8601 UTC wall-clock when the snapshot was assembled.
    emitted_at: IsoDateTime
    # Hostname of the machine that ran the emitter.
    emitted_by_machine: str
    # Fleet of coordinator roots observed in this snapshot (one-fleet-snapshot
    # shape). Each row carries `repo` + `coordinator_root_path` + `owner` +
    # `machine` for keying. Required array; emitter MUST emit `[]` when there
    # are no entries rather than omitting the key (D9). An absent key fails
    # parse.
    coordinator_roots: list[CoordinatorRoot]
    # Active git branches across the fleet. A single branch is incoherent in
    # a multi-root fleet snapshot — this replaces the singular `branch`
    # field. Required array; emitter MUST emit `[]` when there are no entries
    # rather than omitting the key (D9). An absent key fails parse.
    branches: list[Branch]
    handoffs: list[HandoffSummary]
    # Period-keyed queryable arrays of completion rollups; replaces the
    # former single latest-week/day object.
    completion_rollups: CompletionRollups
    backlogs: BacklogsEnvelope
    review_trail: list[ReviewTrail]
    routine_signals: list[RoutineSignal]
    goals_current: list[Goal]
    # Plans summary, present-but-empty until tc-3 wires the emit section (D9).
    plans: list[PlanSummary]
    # Lessons summary, present-but-empty until tc-3 wires the emit section (D9).
    lessons: list[LessonSummary]
    # Outstanding cross-repo memos, metadata-only (no memo bodies; C6-envelope).
    cross_repo_memos: list[CrossRepoMemoSummary]
    # Roadmap summaries, present-but-empty until B3 emitter wires the emit
    # section (D9).
    roadmaps: list[RoadmapSummary]
    # Tracker summaries, present-but-empty until B3 emitter wires the emit
    # section (D9).
    trackers: list[TrackerSummary]
    # Health-status summaries, present-but-empty until B3 emitter wires the
    # emit section (D9).
    health: list[HealthStatusSummary]
    # Decision-guide summaries, present-but-empty until the emit section
    # wires it (D9).
    decision_guides: list[DecisionGuideSummary]
    # Session-hierarchy summaries, present-but-empty until the emit section
    # wires it (D9).
    session_hierarchies: list[SessionHierarchy]
    # File-attribution records, present-but-empty until the emit section
    # wires it (D9).
    file_attributions: list[FileAttribution]
    # Initiative summaries — lightweight parent entities for work identity
    # (D2). Present-but-empty until C4 emitter wires the initiatives SECTION
    # (D9).
    # Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § D2, C1.
    initiatives: list[InitiativeSummary]
    # Executive summaries — per-repo "why this project matters" briefs (one
    # per repo). Present-but-empty until SECTION 8.17 emitter wires
    # exec_summaries (D9).
    # Spec backlink: docs/wiki/exec-summary-artifact.md
    exec_summaries: list[ExecSummary]
    # Competitor-summary facts (market-intelligence), present-but-empty until
    # the producer emit section wires it (D9). v2.16.0.
    competitor_summaries: list[CompetitorSummary]
    # Intelligence-signal facts (market-intelligence), present-but-empty
    # until the producer emit section wires it (D9). v2.16.0.
    intelligence_signals: list[IntelligenceSignal]
    # Roadmap DAG nodes — one record per stub per roadmap across the fleet.
    # Present-but-empty until makima wires the DAG emit section at Gate B
    # (D9).
    # Spec backlink: DoE-claude:pln-cockpit-contract-v2-6-0-initia-75ac93 § AC3
    roadmap_dag_nodes: list[RoadmapDagNode]
    # Roadmap DAG edges — directed "blocks" relationships between stubs.
    # Present-but-empty until makima wires the DAG emit section at Gate B
    # (D9).
    # Spec backlink: DoE-claude:pln-cockpit-contract-v2-6-0-initia-75ac93 § AC3
    roadmap_dag_edges: list[RoadmapDagEdge]
    # Backlog-trend time series (opticon panel). Required non-null block;
    # makima's presence probe reads
    # $defs['snapshot-envelope']['properties']['backlog_history'] and
    # self-activates on this concrete shape. DoE emits a stub
    # (generated_at:null, series:[]); makima populates real series
    # post-parity (D19 pattern).
    # No MalformedRecords bucket — singleton object block, not an
    # array-of-records (completion_rollups / backlogs precedent).
    backlog_history: BacklogHistory
    # Narrative / LLM-generated view layer; null when not yet generated.
    narrative_views: Any | None
    malformed_records: MalformedRecords

"""
cockpit_schema — pydantic port of coordinator-claude `coordinator/cockpit-contract` (Zod source).

Foundation modules (`provenance`, `common`, `emission_scope`) land first per the
T4e-a build recipe — `provenance` proves the hardest patterns (chained
superRefine → chained model_validator, present-as-null vs. optional, inline
non-$ref JSON-Schema emission) before the remaining 27 entity schemas (T4e-b)
and the `emit-schema.ts`-equivalent entrypoint (T4e-c) port against it.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
Freeze: coordinator-claude scratch/subagent-sandbox/bash-to-python-engine-migration/freeze-provenance-envelope.md
Negative-spec: this package holds pydantic MODELS only — it does not register
Claude-klabauter ops (no `register_op` call anywhere in this tree) and central-reg
deferral does not apply to it.

`CONTRACT_VERSION` and `ENTITY_SCHEMAS` below are the pydantic port of
`index.ts`'s aggregation tail (T4e-b/registry) — `emit_schema.py` lazily
imports `ENTITY_SCHEMAS` from exactly this module path (see that module's
`main()`), so the registry lives here rather than in `entities/__init__.py`.
"""
from __future__ import annotations

from typing import Any

from coordinator_core.contract.cockpit_schema.emission_scope import ScopedEmission
from coordinator_core.contract.cockpit_schema.entities import (
    BacklogHistory,
    BacklogItemSummary,
    Branch,
    CompetitorSummary,
    CoordinatorRoot,
    CrossRepoMemoSummary,
    DayRollup,
    DecisionGuideSummary,
    ExecSummary,
    FileAttribution,
    FinancialMetricSummary,
    Goal,
    HandoffSummary,
    HealthStatusSummary,
    InitiativeSummary,
    IntelligenceSignal,
    LessonSummary,
    PlanSummary,
    PriorityLedgerEntry,
    ReviewTrail,
    RoadmapDagEdge,
    RoadmapDagNode,
    RoadmapSummary,
    RoutineSignal,
    SessionHierarchy,
    SnapshotEnvelope,
    TrackerSummary,
    WeekRollup,
)
from coordinator_core.contract.cockpit_schema.provenance import ProvenanceEnvelope

# Contract version (semver) — MAJOR bump on any breaking field change, MINOR
# bump on additive/non-breaking changes (see DECISIONS.md two-axis scheme,
# D19+). Ported verbatim from `index.ts`'s `CONTRACT_VERSION`. Single literal
# source of truth is `emit_schema.py` — the dependency-free leaf module that
# must stay independently importable/runnable as the
# `coordinator-cockpit-emit-schema` console-script entrypoint regardless of
# this package's registry-wiring state (see that module's top-of-file
# comment on the still-open canonical re-home question). Re-exported here,
# not redeclared — but via PEP 562 module `__getattr__` (lazy), NOT a
# top-of-file `from .emit_schema import CONTRACT_VERSION`. An eager import
# pulls `emit_schema` into `sys.modules` as a side effect of importing THIS
# package, which collides with `python -m coordinator_core.contract.
# cockpit_schema.emit_schema` (coordinator-claude's `regen-cockpit-schema.py:208`
# invocation shape) — runpy finds the submodule already imported under its
# real name and emits a `RuntimeWarning` on coordinator-claude's release-critical
# regeneration console, straight past their "verify no unexpected drift"
# line. Lazy attribute access defers the import until `CONTRACT_VERSION` is
# actually read, so plain `import cockpit_schema` (what happens first during
# `-m emit_schema`) never touches `emit_schema` at all. A guard test
# (`tests/test_contract_version_single_source.py`) fails loud if a second
# literal is ever reintroduced.
def __getattr__(name: str) -> Any:
    if name == "CONTRACT_VERSION":
        from coordinator_core.contract.cockpit_schema.emit_schema import (
            CONTRACT_VERSION,
        )

        return CONTRACT_VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Every emittable entity, keyed by stable kebab-case name (used as the JSON
# Schema filename and the round-trip test label). ProvenanceEnvelope is
# included as a shared type so the emitted schema set is self-contained.
# Ported verbatim (key order + key set) from `index.ts`'s `ENTITY_SCHEMAS`.
ENTITY_SCHEMAS: dict[str, Any] = {
    "provenance-envelope": ProvenanceEnvelope,
    "coordinator-root": CoordinatorRoot,
    "branch": Branch,
    "goal": Goal,
    "routine-signal": RoutineSignal,
    "day-rollup": DayRollup,
    "week-rollup": WeekRollup,
    "handoff-summary": HandoffSummary,
    "backlog-item-summary": BacklogItemSummary,
    "review-trail": ReviewTrail,
    "lesson-summary": LessonSummary,
    "plan-summary": PlanSummary,
    "cross-repo-memo-summary": CrossRepoMemoSummary,
    "roadmap-summary": RoadmapSummary,
    "tracker-summary": TrackerSummary,
    "health-status-summary": HealthStatusSummary,
    "decision-guide-summary": DecisionGuideSummary,
    "session-hierarchy": SessionHierarchy,
    "file-attribution": FileAttribution,
    "initiative-summary": InitiativeSummary,
    "exec-summary": ExecSummary,
    "roadmap-dag-node": RoadmapDagNode,
    "roadmap-dag-edge": RoadmapDagEdge,
    "backlog-history": BacklogHistory,
    "competitor-summary": CompetitorSummary,
    "intelligence-signal": IntelligenceSignal,
    "financial-metric-summary": FinancialMetricSummary,
    "priority-ledger-entry": PriorityLedgerEntry,
    "snapshot-envelope": SnapshotEnvelope,
    "emission-scope": ScopedEmission,
}

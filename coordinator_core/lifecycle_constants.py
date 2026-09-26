"""Single source of truth for terminal-state predicate constants across entity axes.

Per DR-084 plan C3
(docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md). Leaf
module — imports nothing from coordinator_core — so every consumer can depend
on it without risking an import cycle.

Per-entity-axis-namespaced by design: HANDOFF_*, PLAN_*, and SPEC_* are
independent axes. The DR-084 handoff rename (C5/C6) touches ONLY HANDOFF_*
exports; PLAN_* and SPEC_* must never be touched by that migration.

HANDOFF_* values widened to dual-vocabulary at C4/P1 (old + new terms accepted
together) and STAY dual-vocabulary today — this is the current correct state,
not a pending-cleanup state. The C7/P4 narrow (5372260e, 169179b3) retired the
old terms on the premise that the corpus was fully migrated; that premise held
only for claude-klabauter's own corpus, not for the consumer repos (example-retrieval-repo,
Example-cockpit-repo, ...) whose on-disk handoff frontmatter these ops also read,
so a claude-klabauter-scoped retirement oracle is structurally insufficient. The narrow
was reverted at 9d00b459, the incident of record. These sets narrow
again only once every consumer repo's on-disk handoff corpus is migrated to
the new vocabulary AND a pre-flight consumer-corpus scan confirms zero
surviving old tokens — not on claude-klabauter's own corpus being clean.
"""

# Consolidated from: records_query._TERMINAL_STATUS,
# ceremony/renderers._TERMINAL_STATUS, frontmatter/consumed_marker.TERMINAL_STATUS
HANDOFF_TERMINAL_STATUS = frozenset({"consumed", "superseded", "claimed"})

# Consolidated from: handoff_reconcile._CLOSED_DEPLOYMENT_STATES,
# fleet/_common._TERMINAL_DEPLOYMENT_STATES, reconcile/gate_eval._TERMINAL_STATES,
# records_query._TERMINAL_DEPLOYMENT, consumed_marker.TERMINAL_DEPLOYMENT
HANDOFF_TERMINAL_DEPLOYMENT = frozenset({"shipped", "abandoned", "continued", "closed"})

# Consolidated from: archival._TERMINAL_STATUSES — a mixed status+deployment
# defensive set. Widens automatically with HANDOFF_TERMINAL_STATUS above.
HANDOFF_ARCHIVAL_TERMINAL_STATUSES = HANDOFF_TERMINAL_STATUS | frozenset({"abandoned"})

# Consolidated from: ops/commit_anchors._TERMINAL_STATUSES — reads the status
HANDOFF_ANCHOR_EXCLUDED_STATUSES = frozenset({"consumed", "archived", "abandoned", "claimed"})

# Consolidated from: ops/fleet/archive_plans._TERMINAL_STATUSES
#   - ops.plan_status_transition._FROZEN_STATUSES answers "is this status
# In particular 'deferred' is deliberately EXCLUDED from archivability (a
PLAN_ARCHIVABLE_STATUS = frozenset({"implemented", "closed_partial", "superseded", "abandoned"})

PLAN_TERMINAL_STATUS = PLAN_ARCHIVABLE_STATUS

SPEC_RIPE_STATUSES = frozenset({"implemented", "shipped"})
SPEC_SKIP_STATUSES = frozenset({"superseded", "abandoned", "partial"})

# PLAN_ARCHIVABLE_STATUS/PLAN_TERMINAL_STATUS above, ops.plan_status_transition
# ._FROZEN_STATUSES, or records_query.liveness()'s plan branch. Do not reuse
# coordinator/docs/wiki/coordinator-tripwires.md § PLAN-ORPHAN-OWNERSHIP.
# the plans the census exists to find — a silent false NEGATIVE, the
PLAN_ORPHAN_TERMINAL_STATUS = frozenset(
    {
        "implemented",
        "closed_partial",
        "shipped",
        "complete",
        "executed",
        "superseded",
        "abandoned",
        "deferred",
    }
)

# A sizing-object's status axis is independent of PLAN_TERMINAL_STATUS /
# HANDOFF_TERMINAL_STATUS above — deliberately not expected to agree with
# either, same convention as PLAN_ORPHAN_TERMINAL_STATUS's note.
SIZING_TERMINAL_STATUS = frozenset({"shipped", "superseded", "declined"})

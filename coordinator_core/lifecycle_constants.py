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
# DR-084 dual-vocabulary, intentionally permanent pending the module docstring's
# exit condition: 'consumed'/'superseded' are the old terms, 'claimed' is the
# new term. Both stay recognized (transitional tolerance, restored 9d00b459).
HANDOFF_TERMINAL_STATUS = frozenset({"consumed", "superseded", "claimed"})

# Consolidated from: handoff_reconcile._CLOSED_DEPLOYMENT_STATES,
# fleet/_common._TERMINAL_DEPLOYMENT_STATES, reconcile/gate_eval._TERMINAL_STATES,
# records_query._TERMINAL_DEPLOYMENT, consumed_marker.TERMINAL_DEPLOYMENT
# DR-084 dual-vocabulary, intentionally permanent pending the module docstring's
# exit condition: 'abandoned' is the old term, 'continued'/'closed' are the new
# terms. All stay recognized (transitional tolerance, restored 9d00b459).
# 'shipped' is terminal-with-resolvable-commit-evidence (shipped_in required),
# NOT 'released to users' — deliberately correct for docs/records/state batons
# that landed as commits. Ruled 2026-07-25 against a example-market-data-repo-em
# consult proposing 'complete'; see that memo in cross-repo/archive/.
HANDOFF_TERMINAL_DEPLOYMENT = frozenset({"shipped", "abandoned", "continued", "closed"})

# Consolidated from: archival._TERMINAL_STATUSES — a mixed status+deployment
# defensive set. Widens automatically with HANDOFF_TERMINAL_STATUS above.
HANDOFF_ARCHIVAL_TERMINAL_STATUSES = HANDOFF_TERMINAL_STATUS | frozenset({"abandoned"})

# Consolidated from: ops/commit_anchors._TERMINAL_STATUSES — reads the status
# axis but carries defensive non-schema tokens 'archived'/'abandoned'
# DR-084 dual-vocabulary, intentionally permanent pending the module docstring's
# exit condition: 'consumed' is the old term, 'claimed' is the new term.
HANDOFF_ANCHOR_EXCLUDED_STATUSES = frozenset({"consumed", "archived", "abandoned", "claimed"})

# Consolidated from: ops/fleet/archive_plans._TERMINAL_STATUSES
#
# Question answered: "can this plan's file be safely git-mv'd into archive/?"
# (archivability — ops/fleet/archive_plans.py's terminality predicate, contract
# §2.2). This name is NOT the same partition as the two other "terminal"-named
# sets elsewhere in the codebase, and the three are NOT expected to agree —
# each answers a genuinely different question about a plan's status:
#   - ops.plan_status_transition._FROZEN_STATUSES answers "is this status
#     frozen against the stamp-implemented flip?" (flippability).
#   - ops.records_query.liveness()'s plan branch answers "what LIVE/BLOCKED/
#     DONE cockpit bucket does this status fall into?" (liveness).
# In particular 'deferred' is deliberately EXCLUDED from archivability (a
# deferred plan stays in docs/plans/, revisitable, not archived) while it IS
# frozen/flippability-terminal at the plan_status_transition site and maps to
# BLOCKED (not DONE) at the liveness site — three defensible, independent
# answers, not a bug. Plan: 2026-07-27-plan-line-item-resolution-model.md § C8b.
PLAN_ARCHIVABLE_STATUS = frozenset({"implemented", "superseded", "abandoned"})

# Backward-compatible alias: ops/fleet/archive_plans.py imports this name
# directly and is out of this rename's write-scope (owned by a concurrent
# C8b-sibling dispatch) — do not remove or repoint without also updating that
# importer.
PLAN_TERMINAL_STATUS = PLAN_ARCHIVABLE_STATUS

# Consolidated from: distill/ripe_filter
SPEC_RIPE_STATUSES = frozenset({"implemented", "shipped"})
SPEC_SKIP_STATUSES = frozenset({"superseded", "abandoned", "partial"})

# Question answered: "which plans are excluded from the orphan census because
# their owning baton is expected to be archived rather than live" (AC3a,
# docs/plans/2026-07-31-plan-orphan-ownership-resolver.md § C1). This is a
# FOURTH, independent plan-status partition — deliberately not the same set as
# PLAN_ARCHIVABLE_STATUS/PLAN_TERMINAL_STATUS above, ops.plan_status_transition
# ._FROZEN_STATUSES, or records_query.liveness()'s plan branch. Do not reuse
# those names or assert this set equal to any of them; the module's
# established convention here is deliberately-disagreeing partitions, not one
# canonical terminal set. Spec of record: example-doctrine-repo
# coordinator/docs/wiki/coordinator-tripwires.md § PLAN-ORPHAN-OWNERSHIP.
#
# Negative-spec — "landed" is deliberately absent (struck 2026-08-06, example-doctrine-repo
# ruling 80b0b29fb adopting this repo's proposal); do not re-add it.
# plan.schema.json documents "landed" as explicitly NON-terminal: chunk code
# is on the branch, spine rows are still open. Excluding it dropped exactly
# the plans the census exists to find — a silent false NEGATIVE, the
# direction that fails quietly, where every other defect in this area
# inflated the population instead. "shipped"/"complete"/"executed" stay
# despite being absent from the schema enum: documented defensive tolerance,
# a separate question, not drift to reconcile here.
PLAN_ORPHAN_TERMINAL_STATUS = frozenset(
    {"implemented", "shipped", "complete", "executed", "superseded", "abandoned", "deferred"}
)

# Question answered: "can this sizing-object's file be safely git-mv'd into
# archive/sizings/?" (archivability — ops/fleet/archive_sizings.py's
# terminality predicate). DR-293 names this family's shape; this set is its
# ONLY terminality source — no literal status list appears in that module.
# A sizing-object's status axis is independent of PLAN_TERMINAL_STATUS /
# HANDOFF_TERMINAL_STATUS above — deliberately not expected to agree with
# either, same convention as PLAN_ORPHAN_TERMINAL_STATUS's note.
# Plan: docs/plans/2026-08-13-terminal-sizings-boot-sweep-family.md.
SIZING_TERMINAL_STATUS = frozenset({"shipped", "superseded", "declined"})

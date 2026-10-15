"""
coordinator_core.authz.dispatchable — per-assembler op admission control.

Purpose: `ASSEMBLER_DISPATCHABLE` restores the closed-construction property the
engine already committed to for `_CLI_DISPATCH` (25 hand-reviewed literals), on the
wider namespace an op-named directive opens (259 registered ops). It is a
DEFAULT-DENY mapping from assembler module name to the frozenset of op names that
module may dispatch: absence from the mapping, or absence from a module's own set,
is a refusal. It is a coupling and blast-radius control with a test behind it, NOT
an authz boundary — its principal (`assembler_name`) is a self-asserted string, and
any in-process caller can still `import` a handler directly and skip this seam
entirely. What it buys is that the *declared* dispatch surface stays enumerable and
reviewed, the same property `_CLI_DISPATCH` gave for free at 25 entries and does not
give for free at 259.

Sited in `coordinator_core/authz/`, the SAME package as `classification.py ::
OP_CLASSIFICATION`, deliberately — so drift is caught by the artifact that already
exists (`registration_quad`'s discovery walk, reused by this module's own test)
rather than only by a new bespoke test.

Negative spec: this mapping is HAND-EDITED and reviewed as such.
  - It is NEVER derived from `coordinator_core.ipc._REGISTRY` or any other
    registry/module enumeration.
  - It is NEVER widened by a loop, comprehension, or programmatic population of any
    kind — every entry is a literal, typed by a reviewer.
  - It is NEVER populated from caller-supplied or directive-supplied input.
  - Ship it EMPTY except for the op names a migration chunk (C4-C6 of
    docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md) actually migrates.
    An allowlist seeded "for convenience" with the whole registry is the failure
    this control exists to prevent.

Spec backlink: pln-directives-name-an-op-not-a-cl-e283a9 § C1
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md (shape precedent)
"""

from __future__ import annotations

import types

# hand-edited only — mutation raises TypeError rather than silently
# escalating an assembler's dispatch privilege at runtime. Copies
# coordinator_core.authz.classification.OP_CLASSIFICATION's MappingProxyType shape
# rather than inventing a new one. Keyed by assembler module name (e.g.
# "pickup_assemble"); each value is the frozenset of op names (assembler-family) or
# CONSUMES_MANIFEST script barewords (completion-family) that assembler may
# dispatch. See docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md
# § The discriminator for the mixed end state.
#
# 2026-08-25: removed six phantom rows (present here, absent from the
# assembler's own CONSUMES_MANIFEST — the oracle these entries are meant to
# mirror), all residue of the 2026-08-23 kill pass (`c07062c99`):
#   - "emit-cadence" (workday_complete, workstream_complete, workweek_complete)
#     — killed outright, state/kill-ledger.md K-056; DR-351
#     ("the emission is deleted, not halted" — PM: "I don't think we should
#     have an emit at all. Cut it."). Never resurrect.
#   - "wsc-tail" (workstream_complete) — state/kill-ledger.md K-046
#     (`ceremony.wsc_tail`), REBUILD CANDIDATE, not yet rebuilt.
#   - "reconcile-completion-commits" (workstream_complete) —
#     state/kill-ledger.md K-054 (`completion.reconcile_commits`), REBUILD
#     CANDIDATE, not yet rebuilt.
#   - "session-claim-cli" (workstream_complete) — the CLI itself
#     (`coordinator/bin/session-claim-cli.py`) is alive and directly
#     callable, but the workstream_complete DIRECTIVE that used to name it
#     (d-release-plan-claim / d-archive-session-claim) was removed as part of
#     the same K-046 wsc_tail kill — `directives_commit_tail.py`'s own
#     "Step 3.5 ... REMOVED" comment records this. workstream_complete no
#     longer EMITS this bareword; the script surviving elsewhere does not
#     make it dispatchable through this seam.
ASSEMBLER_DISPATCHABLE: "types.MappingProxyType[str, frozenset[str]]" = types.MappingProxyType({
    # Completion family (C7, plan § The discriminator for the mixed end
    # state) — script barewords, each a literal member of the named
    # module's own `CONSUMES_MANIFEST` (the single oracle for the set).
    # These three tables' UNIT does not change (still `cli`-named, still
    # in-process module load, never a registered op) — they gain only the
    # admission CONTROL, via `apply_base.assert_dispatchable`.
    "workday_complete": frozenset({
        "workday-complete-args-and-validate",
        "workday-complete-reconcile",
        "workday-complete-step2_5-dirty-tree",
        "reap-claims-for-repos",
        "handoff-housekeeping",
        "workday-complete-step3-consolidate",
        "workday-complete-backfill-scan",
        "workday-complete-backfill-anchor",
        "workday-complete-close",
        "standup",
        "query-completions",
        "coordinator-queue-append",
        "prune-closed-bugs",
        "workday-start-advisory-counters",
        "check-weekly-staleness",
        "goal-close-day",
        "coordinator-ceremony-hook",
    }),
    "workstream_complete": frozenset({
        "wsc-coverage-gate-runner",
        "check-workstream-complete-deletion-blocks",
        "coordinator-lesson-add",
        "coordinator-queue-append",
        "archive-stamp-cli",
        "coordinator-harvest-deferrals",
        "coordinator-complete-entry",
        "coordinator-fold-execution-record",
        "regenerate-orientation-cache",
        "check-machine-local-regeneratability",
        "sweep-terminal-handoffs",
        "sweep-terminal-sizings",
        "scip-rebuild-at-ceremony",
        "structural-index-refresh",
        "review-brightline-gate",
        "freeze-review-diff",
        "classify-dispatch-shape",
        # Plugin-local barewords (P036-T4,
        # docs/plans/2026-09-07-directive-resolution-reaches-a-plugin-local-
        # cli.md) — each also a literal member of
        # `workstream_complete.apply._PLUGIN_LOCAL_CLIS` and of
        # `workstream_complete.CONSUMES_MANIFEST` (T1b), never derived from
        # either.
        "baton-chain-closure",
        "plan-reversibility-eligibility",
        "record-superseding-review",
        "review-stamp",
    }),
    "workweek_complete": frozenset({
        "list-week-changelog",
        "backfill-week-changelog-gaps",
        "validate-fast-and-packageability",
        "lint-frontmatter",
        "workweek-complete-advisories",
        "query-records",
        "detect-initiative-candidates",
        "coordinator-initiative",
        "cruft-sweep",
        "reap-claims-for-repos",
        "handoff-housekeeping",
        "check-wsc-inline-budget",
        "reassess-goal-krs",
        "workweek-complete-drift-guards",
        "workweek-complete-reverse-drift-gate",
        "check-competitor-positioning-nudge",
        "check-no-illegal-paths",
        "workweek-trail-scope",
        "check-arch-audit-staleness",
        "check-atlas-watch-drift",
        "query-completions",
        "workweek-complete-close",
        "check-version-consistency",
        "coordinator-ceremony-hook",
        "workweek-complete-doc-staleness",
        "workweek-complete-doc-verify",
        "tier-u-grant-cli",
    }),
    # Assembler family (C5, plan § C5 / § The discriminator for the mixed
    # end state) — `baton_assemble`'s two op-shaped verbs that resolve to a
    # REGISTERED op (measured live against
    # `coordinator_core.authz.registration_quad._live_registry()`, C5's own
    # discriminator decision, confirmed not re-derived). The other five
    # verbs in `baton_assemble/apply.py`'s `_CLI_DISPATCH` — including the
    # op-shaped-but-NOT-registered `handoff.supersede_predecessor` — stay
    # `cli`-named and are therefore never dispatched through this seam.
    "baton_assemble": frozenset({
        "handoff.stamp_phase",
        "handoff.author_fork",
    }),
    # C5 (docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md)
    # — `learn_lessons_pipeline.apply.apply()` dispatches through ONE
    # `dispatch_table` covering both `cli`/`op` directive shapes
    # (`execute_directives`), so this entry names the package's full
    # dispatchable surface: its `CONSUMES_MANIFEST` cli barewords
    # (`extract-lessons`, `lessons-outbox-drain`, `age-sweep-lessons` — the
    # three distinct scripts backing the five `cli:` directives `brief()`
    # emits) plus the single `op:` verb, `stamp-run-complete`, that
    # `resolve_op` gates through `assert_dispatchable` (§ this module's
    # docstring; `resolve_cli` itself never consults this mapping, but the
    # `op` verb absolutely needs the entry present or every dispatch of
    # `d-stamp-run-complete` raises `UnrecognizedDirective`).
    "learn_lessons_pipeline": frozenset({
        "extract-lessons",
        "lessons-outbox-drain",
        "age-sweep-lessons",
        "stamp-run-complete",
    }),
    # Run-spec rows: script barewords a workflow run spec may dispatch; the
    # workday_complete_run row is a strict superset of workday_complete's manifest.
    "workday_complete_run": frozenset({
        "workday-complete-args-and-validate",
        "workday-complete-reconcile",
        "workday-complete-step2_5-dirty-tree",
        "reap-claims-for-repos",
        "handoff-housekeeping",
        "workday-complete-step3-consolidate",
        "workday-complete-backfill-scan",
        "workday-complete-backfill-anchor",
        "workday-complete-close",
        "standup",
        "query-completions",
        "coordinator-queue-append",
        "prune-closed-bugs",
        "workday-start-advisory-counters",
        "check-weekly-staleness",
        "goal-close-day",
        "coordinator-ceremony-hook",
        "tier-u-grant-cli",
        "workday-complete-assemble",
        "structural-index-refresh",
        "day-coverage-sweep",
        "baton-drift-sweep",
        "check-auto-memory-drained",
        "coordinator-invoke",
    }),
    # Typed by hand; pinned equal to `workday_start.manifest.CONSUMES_MANIFEST` by
    # `test_workday_start_row_equals_its_manifest`.
    "workday_start": frozenset({
        "agent-worktree-sweep",
        "append-goal-event",
        "central-run-due",
        "check-bin-forwarders",
        "check-deferral-orphan-memo",
        "check-deferral-partial-strangle",
        "check-engine-drift",
        "check-fleet-claude-md-handoff-convention",
        "check-forwarder-drift",
        "check-gitignore-template-drift",
        "check-global-doctrine-mirror",
        "check-harvest-debt",
        "check-mcp-namespace-registration",
        "check-plugin-drift",
        "check-provisional-expiry",
        "check-watch-state-gitignore-fleet",
        "coordinator-ceremony-hook",
        "coordinator-doctor-sentinel",
        "coordinator-ensure-hooks-fleet",
        "coordinator-git-maintenance",
        "coordinator-queue-append",
        "copy-currency-refresh",
        "corpus-currency-probe",
        "draft-plan-aging",
        "generate-repomap",
        "goal-coverage-scan",
        "group-em-nomination",
        "group-em-watch-cli",
        "handoff-housekeeping",
        "install-meta-repo-precommit-hook",
        "list-orphaned-plans",
        "memo-outbox-tracking-guard",
        "normalize-consumed-frontmatter",
        "orient-assemble",
        "orphan-branch-sweep",
        "promote-shipped-in-flight-stubs",
        "query-completions",
        "regenerate-orientation-cache",
        "scan-addon-health",
        "structural-index-refresh",
        "sweep-terminal-handoffs",
        "tier-last-run",
        "verify-snippet-sync",
        "whats-next",
        "workday-complete-backfill-scan",
        "workday-start-advisory-counters",
        "workday-start-cross-repo-memo-outbox-surface",
        "workday-start-day-branch-resolve",
        "workday-start-handoff-triage",
        "workday-start-health-probes",
        "workday-start-inbox-blitz-assemble",
        "workday-start-step0",
        "workweek-trail-scope",
        "write-workday-start-marker",
    }),
    "workweek_start_run": frozenset({
        "orient-assemble",
        "expired-plan-gates",
        "check-anchor-freshness",
        "generate-exec-summary",
        "check-competitor-positioning-nudge",
        "workweek-start-goal-and-priorities",
        "goal-coverage-scan",
        "coordinator-invoke",
        "workday-start-run",
    }),
})

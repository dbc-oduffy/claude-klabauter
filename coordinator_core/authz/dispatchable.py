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

# coordinator_core.authz.classification.OP_CLASSIFICATION's MappingProxyType shape
# CONSUMES_MANIFEST script barewords (completion-family) that assembler may
# assembler's own CONSUMES_MANIFEST — the oracle these entries are meant to
#     (`ceremony.wsc_tail`), REBUILD CANDIDATE, not yet rebuilt.
#     CANDIDATE, not yet rebuilt.
#     callable, but the workstream_complete DIRECTIVE that used to name it
ASSEMBLER_DISPATCHABLE: "types.MappingProxyType[str, frozenset[str]]" = types.MappingProxyType({
    # module's own `CONSUMES_MANIFEST` (the single oracle for the set).
    "workday_complete": frozenset({
        "workday-complete-args-and-validate",
        "workday-complete-reconcile",
        "workday-complete-step2_5-dirty-tree",
        "reap-orphaned-in-flight-handoffs",
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
        "review-brightline-gate",
        "freeze-review-diff",
        "classify-dispatch-shape",
        # `workstream_complete.apply._PLUGIN_LOCAL_CLIS` and of
        # `workstream_complete.CONSUMES_MANIFEST` (T1b), never derived from
        "baton-chain-closure",
        "plan-reversibility-eligibility",
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
    # REGISTERED op (measured live against
    # verbs in `baton_assemble/apply.py`'s `_CLI_DISPATCH` — including the
    "baton_assemble": frozenset({
        "handoff.stamp_phase",
        "handoff.author_fork",
    }),
    # dispatchable surface: its `CONSUMES_MANIFEST` cli barewords
    "learn_lessons_pipeline": frozenset({
        "extract-lessons",
        "lessons-outbox-drain",
        "age-sweep-lessons",
        "stamp-run-complete",
    }),
})

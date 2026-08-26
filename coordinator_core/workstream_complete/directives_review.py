"""
coordinator_core.workstream_complete.directives_review — the Step 2.9 /
2.9b review-dispatch builders for the `workstream-complete-assemble`
computed-skill engine.

Purpose: converts the ~130-line mechanical shell of Step 2.9 (Code Review
Consideration) and Step 2.9b (Dispatch-Shape Observation) into pure,
`__init__`-independent builder functions per D-3
(docs/plans/2026-07-26-workstream-complete-computed-frontage.md, chunk
C2d): the diff-shape row-selection table becomes a computed decision
(`decide_review_scale`) instead of a table the EM reads by eye, and the
quota-exhaustion regex/length-corroboration table becomes
`scan_dispatch_output(text) -> bool`. This module authors NO judgment
logic — the 8 review-side `judgment_points` named in D-3
(`review-partition-strategy`, `reviewer-count-on-oracle-disagreement`,
`shared-schema-touch-check`, `governing-spec-identification`,
`finding-tradeoff-escalation-check`, `shallow-row3-waive-check`,
`review-dispatch-vehicle-choice`, `quota-retry-vs-escalate`) belong to
`judgments.py` (C2f).

Source: DoE-claude coordinator/skills/workstream-complete/SKILL.md,
Step 2.9 (lines ~409-566) and Step 2.9b (lines ~568-587).

This module is one of seven siblings (directives_lessons_plan.py,
directives_completion.py, directives_memo_lifecycle.py,
directives_commit_tail.py, directives_session_hygiene.py, judgments.py)
built under the multi-module-assembler convention this plan sets: every
submodule exposes pure, side-effect-free builder functions; `__init__.py`
is retained as the assembly + CLI seam ONLY (D-4;
coordinator/docs/wiki/computed-skills-conversion-checklist.md registers
the convention).

Consumes (orchestrates, reimplements none):
    coordinator/bin/review-brightline-gate.py
        -> d-run-review-brightline-gate's directives[].cli (mid-chain).
    coordinator/bin/wsc-coverage-gate-runner.py (write-trail)
        -> d-write-review-trail's directives[].cli. `write-trail` and
        `claim-plan` are all that runner still exposes; its
        `brightline-gate` and `coverage-gate --from-handoff` subcommands
        were removed 2026-08-19 (state/kill-ledger.md K-007).
    coordinator/bin/freeze-review-diff.py
        -> d-freeze-and-dispatch-review-partition's per-slice
        directives[].cli.
    coordinator/bin/fan-out-integrator.py
        -> d-freeze-and-dispatch-review-partition's post-reviewer
        integrator directives[].cli.
    coordinator_core/ops/scan_unresolved_ubt_records.py
        -> d-run-ubt-pending-check's directives[].cli.
    coordinator/bin/classify-dispatch-shape.py
        -> d-classify-dispatch-shape's directives[].cli.

Design note — directives[] vs pure compute (same discriminator
`directives_session_hygiene.py` documents): a census row becomes a
`directives[].cli` entry only when a real, on-disk CLI exists for the
caller to invoke. Rows with no such CLI — the diff-shape row-selection
table (d-review-scale-decide), the mid-chain diff-scope resolution
algorithm (d-resolve-mid-chain-review-scope), the doc-fragile domain-lens
predicate (d-run-doc-fragile-gate-and-dispatch), the quota-exhaustion
scan (d-detect-quota-exhausted-dispatch), and the trail
range-termination disbelief predicate (d-verify-trail-range-termination,
the check half of Step 2.9's trail-write paragraph) — are exposed as
plain pure functions/NamedTuples instead. Modeling any of these as a
phantom `directives[].cli` value would fail this package's own
`test_directives_only_name_known_real_clis_and_never_invoke_them` guard
and AC2's manifest-membership contract test.

Negative-spec:
    - Does NOT decide `review-partition-strategy`,
      `reviewer-count-on-oracle-disagreement`, `shared-schema-touch-check`,
      `governing-spec-identification`, `finding-tradeoff-escalation-check`,
      `shallow-row3-waive-check`, `review-dispatch-vehicle-choice`, or
      `quota-retry-vs-escalate` — all eight are C2f's `judgments.py`.
      Every function here turns an ALREADY-DECIDED input (a resolved
      session id, a chosen slice list, a caller-supplied verdict) into
      directive/compute shape; none of them make the underlying call.
    - Does NOT dispatch a `code-reviewer` / `docs-checker` / `Agent` —
      background-dispatching an agent is an EM/harness action, never a
      `directives[].cli` entry (mirrors `directives_session_hygiene.py`'s
      own precedent for Step 2.95's cross-cutting question). This module
      only names the mechanical CLI calls that surround that dispatch
      (freeze-before, integrator-after) and the read-only predicate that
      decides whether the doc-fragile dispatch fires at all.
    - Does NOT invoke `list-review-trail-records.py` or any `git`
      subprocess itself. `resolve_mid_chain_review_scope` and
      `verify_trail_range_termination` take the already-fetched trail
      records and an injected `is_ancestor` callable — fetching the
      records and deciding `git merge-base --is-ancestor` are the
      caller's job, consistent with every other builder module in this
      package staying pure/IO-free (D-4). The `chain_attribution` import
      (C6a, docs/plans/2026-08-15-composition-invocation-budgets.md) is
      the SAME posture, not an exception to it: only `foreign_shas_from_
      window` is called, and that function is pure set math over an
      already-resolved window (`ChainAttributionWindow.commit_map`) —
      resolving the window itself (`bulk_commit_attribution_map`,
      `bulk_grep_attributed_shas`, both real `git log` spawns) stays the
      caller's job, injected via `ChainAttributionWindow` exactly like
      `resolve_range_shas`/`narrow_foreign_shas` already are. The live caller is
      `coordinator/bin/wsc-coverage-gate-runner.py`'s `coverage-gate`
      subcommand (not `workstream_complete.apply`, which never reads trail
      records at all) — it loads records via
      `coordinator_core.ops.list_review_trail_records`, resolves
      `chain_tip_sha` to the CHAIN'S OWN TIP (the newest substantive commit
      the coverage gate itself reasoned over, via
      `_resolve_chain_tip_sha`'s re-derivation of `coordinator_core.coverage.
      _derive_dag_chain_set` — NOT raw `git rev-parse HEAD`; see that
      function's docstring for why raw HEAD is structurally unsatisfiable on
      this fleet's shared `work/*` branches, fixed 2026-07-27), and supplies
      `is_ancestor` via `git merge-base --is-ancestor`, then uses this
      predicate's `False` to qualify (never demote-to-halt) an already-COVERED
      verdict.
    - Does NOT write `state/review-trail/*.json`, freeze a diff file, or
      run any mutating op in-process. Every mutation this module names is
      an existing CLI for the apply half (C4) to invoke, never invoked
      here.
    - Does NOT reconcile the `d-run-chain-coverage-gate` id here with
      `__init__.py`'s pre-existing `d-coverage-gate` directive (same
      underlying `wsc-coverage-gate-runner.py coverage-gate --from-handoff`
      call, distinct id) — that overlap is C3's assembly-seam concern
      (manifest wiring), not this pure-builder module's.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional

from coordinator_core import chain_attribution
from coordinator_core.commit_ledger.oracle import OracleReport
from coordinator_core.ops.ceremony.wsc_disposition import PREDECESSOR_CONSUMED, canonicalize
from coordinator_core.coverage import (
    SAFE_RANGE as _SAFE_RANGE,
    _FOREIGN_STRIPPED_SCOPES,
    _record_range_has_stored_head,
)

#: Generator-provenance declaration (coordinator_core/ops/generator_provenance.py).
#: record_gate_memo() below writes state/ceremony/wsc-gate-verdict-memo/<hash>.json
#: (hashed-key filename, one per distinct (gate_id, resolved-inputs) pair) -- a
#: data-dependent set of tracked artifacts, not a fixed one, so this is a
#: corpus-mutator declaration rather than GENERATES.
MUTATES = ["state/ceremony/wsc-gate-verdict-memo/*.json"]

# ---------------------------------------------------------------------------
# Shared directive-dict helper (same shape as __init__.py's private
# `_directive` — duplicated rather than imported to keep this module
# `__init__`-independent per D-4's pure-submodule convention).
# ---------------------------------------------------------------------------


def _directive(
    id_: str,
    cli: str,
    args: list[str],
    depends_on: Any = None,
    already_satisfied: bool = False,
) -> dict[str, Any]:
    return {"id": id_, "cli": cli, "args": args, "depends_on": depends_on, "already_satisfied": already_satisfied}


#: Unlike its six siblings, no builder in this module reads a `decisions`
#: mapping directly — each takes its inputs as explicit typed parameters
#: (`session_id`, `range_`, `slices`, `plan_file`, ...), resolved by the
#: caller (`__init__.py`'s `build_directives`) from ITS OWN `decisions`
#: keys (`review_partition`, `ubt_check`, `classify_dispatch_plan_file`).
#: Declared empty here — rather than omitted — so every `directives_*.py`
#: sibling carries the same `FREE_VALUE_KEYS` contract point per AC3
#: (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md),
#: and a future caller-side `decisions` param added to this module has one
#: obvious place to register its keys.
FREE_VALUE_KEYS: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# d-review-scale-decide — the diff-shape row-selection table
# (SKILL.md:413-426), a pure computed decision, no CLI.
# ---------------------------------------------------------------------------


class ReviewScaleDecision(NamedTuple):
    row: Optional[int]  # None iff resolved is False — never row 0/7/None-as-a-row-choice.
    scale: str  # "none" | "code-reviewer" | "partitioned" | "unresolved"
    partition_mandatory: bool
    commit_message_names_change: bool
    reason: str
    resolved: bool = True


_BRIGHTLINE_LOC = 500
_BRIGHTLINE_COMMITS = 5
_BRIGHTLINE_SURFACES = 4
_SMALL_FIX_LOC_CEILING = 50

#: Chain-wide arm ceiling (C7, restoring what K-007's row-6 removal lost).
#: Mirrors row 4's `_BRIGHTLINE_COMMITS` on the same unit -- an
#: `OracleReport` figure's `weight` accumulates at `_DEFAULT_BASELINE_
#: WEIGHT` (1.0, `commit_ledger/classify.py`) per non-noise commit absent
#: repo-specific elevation, so this ceiling reads as "the chain-wide
#: equivalent of row 4's 5-commit brightline" rather than an unrelated
#: figure. Scoped to this module only -- not exported, not reused by the
#: oracle itself (`oracle.py`'s own docstring: NO threshold comparison
#: there, that stays here).
_CHAIN_WEIGHT_CEILING = float(_BRIGHTLINE_COMMITS)

def _unresolved(reason: str) -> ReviewScaleDecision:
    return ReviewScaleDecision(
        row=None, scale="unresolved", partition_mandatory=False,
        commit_message_names_change=False, reason=reason, resolved=False,
    )


def _decide_review_scale_core(
    *,
    gross_loc: Optional[int],
    code_loc: Optional[int],
    commit_count: Optional[int],
    surface_count: Optional[int],
    executor_dispatched: Optional[bool],
    shared_schema_touched: Optional[bool],
    chain_disposition: str,
    baton_count: Optional[int] = None,
    commit_count_scope: Optional[str] = None,
    zero_diff_commit_count: Optional[int] = None,
) -> ReviewScaleDecision:
    """SKILL.md's diff-shape row-selection table (lines 415-424) plus its
    precedence rule (line 426, order 6 > 4 > 5 > 3 > 1 > 2): the big-diff
    brightline (row 4) and the chain-end rows (5, 6) override the
    per-session rows (1, 2, 3) when they apply.

    Every one of the seven row-4/5/6 inputs is `Optional` and independently
    represents "not yet resolved" as `None`, distinct from a resolved
    falsy value (`0` / `False`) — a caller that has not resolved an input
    can always be told apart from one that resolved it negatively. When an
    unresolved input cannot be ruled out of changing the selected row, this
    function returns the explicit unresolved outcome (`resolved=False`,
    `row=None`, `scale="unresolved"`, `partition_mandatory=False`) rather
    than defaulting toward a specific row — `partition_mandatory` stays
    `False` on that outcome because an unresolved decision must not
    manufacture a mandatory partition it has no evidence for. This
    replaces the prior behavior, where `chain_diff_trivial=None` silently
    resolved to "not chain-end, use the per-session rows" — the opposite
    of conservative on a chain terminal, not "the conservative per-session
    answer" the old docstring claimed here.

    Scope of each input (every input below is SESSION-scoped — this closing
    session's own diff. No CHAIN-scoped input survives: the chain-terminal
    brightline gate that walked the full chain DAG was removed 2026-08-19,
    state/kill-ledger.md K-007):
      - `commit_count`, `surface_count` — SESSION-scoped. Feed the row-4
        big-diff brightline predicate below alongside `code_loc`. A
        session-scoped brightline on a chain terminal means "this final
        session's own diff is big," NOT "the diff accumulated across the
        whole chain is big" — and since K-007 nothing answers the latter.
      - `code_loc` — SESSION-scoped, noise-excluded reviewable LOC. Feeds
        BOTH the row 1/2/3 code-vs-noise discrimination AND (2026-08-11,
        AC4) the row-4 big-diff brightline predicate — a gate measuring
        "is this diff big" must not count prose/lockfiles/bookkeeping as
        the reason to partition, any more than rows 1-3 count it as "code
        touched". `gross_loc` is accepted for backward compatibility with
        callers not yet producing `code_loc`, but no longer read by any row
        below.
      - `executor_dispatched`, `shared_schema_touched` — SESSION-scoped.
        Whether THIS session dispatched an executor / touched a shared
        schema or seam.
      - `baton_count` — SESSION-scoped (2026-08-04 sizing,
        `state/sizings/2026-08-04-mise-run-record-should-carry-baton-count.yaml`;
        source memo `cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-
        partition-mandatory-does-not-halt.md`). How many top-level batons
        `/mise-en-place` executed in the closing run. A resolved value
        `>= 2` MULTIPLIES the row-4 brightline's session-scoped metrics
        (`code_loc`/`commit_count`/`surface_count`) before the brightline
        predicate is evaluated — never forces the partitioned row outright;
        a trivial 2-baton run should not pay partition-mandatory's cost
        unconditionally. It ALSO FLOORS the outcome: a resolved
        `baton_count >= 2` may never resolve to the no-review rows (1/2) —
        a multi-baton mise close always gets at least a reviewer, which is
        the exact under-read the source memo reports (a 3-baton, 24-item,
        16-commit run resolving to a single reviewer because baton count
        was invisible to this function). `None` (every existing caller's
        implicit default) leaves every row selection byte-identical to
        before this input existed — the multiplier and the floor both
        no-op on `None`.
      - `commit_count_scope` — SESSION-scoped, cosmetic-only (2026-08-12,
        docs/plans/2026-08-12-review-mandate-guides-the-split.md C7,
        example-market-data-repo-em memo `cross-repo/inbox/2026-08-12-market-
        intelligence-em-brightline-peer-commit-count-second-instance.md`).
        Names the scope `commit_count` was resolved under when it came from
        a caller-supplied `decisions["commit_count"]` override rather than
        this engine's own trailer-scoped measurement
        (`_measure_session_review_scale_inputs`) — e.g. `"session-owned"`
        when the EM attests the number is already session-scoped, or a
        franker label when it is not. Never influences row SELECTION or the
        brightline predicate itself: an override still wins unconditionally
        over the measured value (that affordance is deliberately preserved,
        `workstream-complete` SKILL.md documents it as available EM hand-
        supply), and this parameter does not gate or validate the supplied
        `commit_count` in any way. Its only effect is threading into row 4's
        `reason` string (see `_row4_decision`) so an override's scope is
        RECORDED on the review-trail record rather than silently trusted —
        closing the reported failure mode where an EM read an unfiltered
        branch-range count off the gate's own stdout and passed it through
        with nothing on disk distinguishing that from a genuine session-
        scoped measurement. `None` (every pre-2026-08-12 caller, and every
        caller that left `commit_count` on the measured path) omits the
        clause entirely — byte-identical to before this parameter existed.
        An explicitly-supplied empty string collapses to the same
        `"unspecified"` the caller's outright omission produces (the
        resolution site's `... or "unspecified"` is falsy-coercing, not
        `is None`-checking) — a caller cannot distinguish "I attested and
        the scope is empty" from "I didn't attest" on the trail record.

    Row 6 (chain-scoped PARTITION-MANDATORY) is REMOVED along with the
    chain-terminal brightline gate that produced its verdict — state/kill-
    ledger.md K-007, 2026-08-19, PM ruling. A chain-terminal close now
    resolves on the session-scoped brightline alone: row 4 when it trips,
    row 5 when it resolves and does not. The accumulated-over-many-small-
    sessions case row 6 existed to catch is UNDETECTED until the PM
    specifies replacement coverage; row 4 still fires on a chain terminal
    whose own session diff hits the brightline. Precedence is now 4, then
    the row-4-inputs-unresolved return, then 5, 3, 1, 2.

    shell-doc-ok: the backticked comparisons above are Python boolean
    expressions quoted from this function's own code, not shell version
    constraints.
    """
    is_chain_terminal = canonicalize(chain_disposition) == PREDECESSOR_CONSUMED

    # `baton_count >= 2` MULTIPLIES the row-4 metrics (never forces the
    # partitioned row outright) — see the docstring's `baton_count` bullet.
    # `None`/`1` leaves `effective_*` identical to the raw measurement, so
    # every existing caller (which omits `baton_count`) sees byte-identical
    # row-4 behaviour. Hoisted above the `is_chain_terminal` branch (fix,
    # 2026-08-10): row 4 must be evaluable on a chain terminal too — see
    # this function's own docstring precedence paragraph.
    # shell-doc-ok: the backticked comparison above is a Python boolean
    # expression, not a shell version constraint.
    baton_multiplier = baton_count if (baton_count is not None and baton_count >= 2) else 1
    # 2026-08-11 (AC4): row 4 reads `code_loc` — the noise-excluded,
    # reviewable LOC rows 1-3 already discriminate on — not `gross_loc`'s
    # raw diff-stat sum. `gross_loc` stays an accepted parameter (unused by
    # this predicate now) for callers not yet threading `code_loc` through.
    effective_code_loc = code_loc if code_loc is None else code_loc * baton_multiplier
    # 2026-08-20 (same memo as the `code_loc_resolved_zero` note below): the
    # commit-count arm is a proxy for ACCUMULATED RISK, and a commit with a
    # zero-line diff carries none. `baton-assemble apply` scaffold commits
    # are the reported instance — nine of one session's sixteen, every one
    # at `diff_loc: 0`, pushing a doc-only close past the `>= 5` threshold.
    # Subtracted from the brightline's commit arm ONLY: `commit_count`
    # itself stays the honest count of this session's commits everywhere it
    # is REPORTED (row-4 reason string, review trail, commit slices), so
    # this narrows what the threshold reads without making a counter lie
    # about what it counted. `None` no-ops for every caller not supplying it.
    brightline_commit_count = commit_count
    if commit_count is not None and zero_diff_commit_count:
        brightline_commit_count = max(0, commit_count - zero_diff_commit_count)
    effective_commit_count = (
        brightline_commit_count if brightline_commit_count is None else brightline_commit_count * baton_multiplier
    )
    effective_surface_count = surface_count if surface_count is None else surface_count * baton_multiplier

    # 2026-08-20 (cross-repo/inbox/2026-08-20-example-retrieval-repo-em-review-gate-doc-
    # only-em-discretion.md, PM-endorsed): a RESOLVED `code_loc == 0` means
    # the reviewable-LOC oracle measured this session and found nothing to
    # review. The commit-count and surface-count arms are PROXIES for
    # accumulated code risk; letting a proxy mandate a partition over the
    # direct measurement's own zero is the reported defect — it converted a
    # doc-only close into one legal exit, a PM waiver, for work the EM can
    # obviously judge. The brightline stays fully armed for every session
    # with any code in it: this suppresses the proxies ONLY when the direct
    # measure is a resolved, honest zero (`None` is unresolvable and is
    # deliberately NOT treated as zero — that would fail toward less
    # review). Falls through to row 1 ("no code touched"), an EM-discretion
    # row, which is what `review-brightline-gate`'s own `VERDICT=single-
    # reviewer-ok` on the same range already said.
    code_loc_resolved_zero = code_loc is not None and code_loc == 0
    brightline_known_true = (not code_loc_resolved_zero) and (
        (effective_code_loc is not None and effective_code_loc >= _BRIGHTLINE_LOC)
        or (effective_commit_count is not None and effective_commit_count >= _BRIGHTLINE_COMMITS)
        or (effective_surface_count is not None and effective_surface_count >= _BRIGHTLINE_SURFACES)
    )
    brightline_resolved = code_loc is not None and commit_count is not None and surface_count is not None

    def _row4_decision() -> ReviewScaleDecision:
        multiplier_note = (
            f", baton_count={baton_count} multiplier applied" if baton_multiplier != 1 else ""
        )
        scope_note = f", commit_count_scope={commit_count_scope}" if commit_count_scope is not None else ""
        return ReviewScaleDecision(
            row=4, scale="partitioned", partition_mandatory=True, commit_message_names_change=False,
            reason=(
                f"big-diff brightline hit (code_loc={code_loc}, commits={commit_count}, "
                f"surfaces={surface_count}{multiplier_note}{scope_note})"
            ),
        )

    def _row4_inputs_unresolved() -> ReviewScaleDecision:
        missing = [
            name for name, value in (
                ("code_loc", code_loc), ("commit_count", commit_count), ("surface_count", surface_count),
            ) if value is None
        ]
        return _unresolved(
            f"row-4 big-diff brightline input(s) not yet resolved: {', '.join(missing)} "
            "(row 4 cannot be ruled out)"
        )

    if is_chain_terminal:
        # Row 6 (the chain-scoped PARTITION-MANDATORY verdict) is REMOVED with
        # the chain-terminal brightline gate that produced it — state/kill-
        # ledger.md K-007, 2026-08-19, PM ruling. A chain terminal now decides
        # on the session-scoped brightline alone: row 4 when it trips, row 5
        # otherwise. The accumulated-over-many-small-sessions case row 6 used
        # to catch has no detector until the PM specifies the replacement.
        if brightline_known_true:
            return _row4_decision()
        if not brightline_resolved:
            return _row4_inputs_unresolved()
        return ReviewScaleDecision(
            row=5, scale="code-reviewer", partition_mandatory=False, commit_message_names_change=False,
            reason="chain-terminal with the session-scoped brightline resolved and not tripped",
        )

    if brightline_known_true:
        return _row4_decision()

    # 2026-08-11 (reverted C7): row 4's unresolved inputs must block the
    # decision ahead of row 3, not the other way around. Row 3 is a
    # strictly smaller review obligation than row 4 -- resolving to row 3
    # while row 4's metrics (`code_loc`/`commit_count`/`surface_count`) are
    # genuinely unmeasured risks silently under-scoping a session whose
    # real diff would have tripped row 4's PARTITION-MANDATORY. The
    # module's own failure direction is toward asking, never toward a
    # smaller review, so an unresolved row-4 input keeps the whole
    # decision unresolved rather than falling through to row 3.
    if not brightline_resolved:
        return _row4_inputs_unresolved()

    row3_known_true = (
        executor_dispatched is True
        or (code_loc is not None and code_loc > _SMALL_FIX_LOC_CEILING)
        or shared_schema_touched is True
    )
    if row3_known_true:
        return ReviewScaleDecision(
            row=3, scale="code-reviewer", partition_mandatory=False, commit_message_names_change=False,
            reason="executor dispatched, or >50 LOC code change, or a shared schema/seam touched",
        )

    row3_resolved = executor_dispatched is not None and code_loc is not None and shared_schema_touched is not None
    if not row3_resolved:
        missing = [
            name for name, value in (
                ("executor_dispatched", executor_dispatched), ("code_loc", code_loc),
                ("shared_schema_touched", shared_schema_touched),
            ) if value is None
        ]
        return _unresolved(
            f"row-3 input(s) not yet resolved: {', '.join(missing)} (row 3 cannot be ruled out)"
        )

    if code_loc == 0:
        no_review_decision = ReviewScaleDecision(
            row=1, scale="none", partition_mandatory=False, commit_message_names_change=False,
            reason="doc-only edits / lesson capture, no executor dispatched, no code touched",
        )
    else:
        no_review_decision = ReviewScaleDecision(
            row=2, scale="none", partition_mandatory=False, commit_message_names_change=True,
            reason="single-file fix under 50 LOC, no shared schema touched, no executor",
        )

    # Floor (docstring's `baton_count` bullet): a resolved `baton_count >= 2`
    # may never resolve to a no-review row (1/2) — a multi-baton mise close
    # always gets at least a reviewer. `None` no-ops, leaving `no_review_
    # decision` unchanged for every existing caller.
    # shell-doc-ok: the backticked comparison above is a Python boolean
    # expression, not a shell version constraint.
    if baton_count is not None and baton_count >= 2:
        return ReviewScaleDecision(
            row=3, scale="code-reviewer", partition_mandatory=False, commit_message_names_change=False,
            reason=(
                f"{no_review_decision.reason}; floored to code-reviewer because "
                f"baton_count={baton_count} (a resolved multi-baton mise run may "
                "never resolve to a no-review row)"
            ),
        )
    return no_review_decision


def _apply_chain_wide_arm(
    decision: ReviewScaleDecision, oracle_report: Optional[OracleReport]
) -> ReviewScaleDecision:
    """The chain-wide arm (C7): folds `oracle_report` into `decision`'s
    `scale`/`reason` ONLY -- restores what K-007's row-6 removal lost, on
    the ledger substrate that makes it cheap (`commit_ledger/oracle.py`).

    HARD REQUIREMENT (AC11, B4): NEVER sets `partition_mandatory`. Row 4's
    own `partition_mandatory=True` (`_row4_decision`) is a SESSION-scoped
    verdict this arm must not touch either way -- `ops/ceremony/tail_ops.py`
    turns `partition_mandatory=True` plus incomplete review-trail metadata
    into `failed_critical[]`, the exact hard stop K-007 removed, and any
    path letting this arm influence that field rebuilds K-007 one call
    frame away. `decision.row` and `decision.partition_mandatory` are
    therefore always returned byte-identical to what `_decide_review_scale_
    core` computed.

    No-ops (returns `decision` unchanged) when:
      - `oracle_report` is `None` -- no caller has wired the oracle in yet,
        byte-identical to pre-C7 behaviour.
      - `oracle_report.resolved` is `False` -- "pending, no ledger yet"
        (`oracle.py`'s own docstring) carries no disposition to offer here
        either; this arm inherits the oracle's own no-verdict-on-pending
        stance rather than manufacturing one.
      - `decision.scale != "none"` -- row 3/4/5 already selected a
        reviewer or partition; the chain-wide arm only ever RAISES a
        no-review outcome, never re-scopes one that already has a
        reviewer, and never downgrades `"unresolved"`.
      - the oracle's `with_docs` weight is below `_CHAIN_WEIGHT_CEILING`
        (or unresolved/`None`) -- nothing to raise on.

    Otherwise, upgrades a `scale="none"` outcome to `"code-reviewer"`
    (never `"partitioned"` -- that stays row 4's call alone) and appends
    the chain-wide basis to `reason`, so the accumulated-small-sessions
    case row 6 used to catch is visible again without resurrecting its
    exit-code/hard-halt shape.
    """
    if oracle_report is None or not oracle_report.resolved:
        return decision
    if decision.scale != "none":
        return decision

    weight = oracle_report.with_docs.weight
    if weight is None or weight < _CHAIN_WEIGHT_CEILING:
        return decision

    return decision._replace(
        scale="code-reviewer",
        reason=(
            f"{decision.reason}; chain-wide arm raised to code-reviewer "
            f"(with_docs weight {weight:g} >= ceiling {_CHAIN_WEIGHT_CEILING:g}: "
            f"{oracle_report.with_docs.basis})"
        ),
    )


def decide_review_scale(
    *,
    gross_loc: Optional[int],
    code_loc: Optional[int],
    commit_count: Optional[int],
    surface_count: Optional[int],
    executor_dispatched: Optional[bool],
    shared_schema_touched: Optional[bool],
    chain_disposition: str,
    baton_count: Optional[int] = None,
    commit_count_scope: Optional[str] = None,
    zero_diff_commit_count: Optional[int] = None,
    oracle_report: Optional[OracleReport] = None,
) -> ReviewScaleDecision:
    """Wraps `_decide_review_scale_core` (the row-selection table, docstring
    there) with the chain-wide oracle arm (`_apply_chain_wide_arm`, C7).

    `oracle_report` is the only new input: an `Optional[commit_ledger.
    oracle.OracleReport]`, `None` by every existing caller until they wire
    C7's oracle in. `None` leaves this function byte-identical to the
    pre-C7 `_decide_review_scale_core` on every input combination -- the
    arm is purely additive and cannot change row selection, precedence, or
    `partition_mandatory` for a caller that has not adopted it yet.
    """
    decision = _decide_review_scale_core(
        gross_loc=gross_loc,
        code_loc=code_loc,
        commit_count=commit_count,
        surface_count=surface_count,
        executor_dispatched=executor_dispatched,
        shared_schema_touched=shared_schema_touched,
        chain_disposition=chain_disposition,
        baton_count=baton_count,
        commit_count_scope=commit_count_scope,
        zero_diff_commit_count=zero_diff_commit_count,
    )
    return _apply_chain_wide_arm(decision, oracle_report)


# ---------------------------------------------------------------------------
# Gate verdict memo — C4 (docs/plans/2026-08-10-commit-event-5s-cap-and-the-
# silent-tail.md, AC6). Multi-pass `apply` (the skill's own `next_move`:
# "resolve a subset and re-run to pick up the rest") re-invokes both gate
# builders with UNCHANGED inputs every pass, and until this memo existed
# neither builder had any way to tell `apply` the verdict was already
# walked — `already_satisfied` defaulted False and stayed False forever.
#
# Keyed on THE INPUTS EACH GATE WAS COMPUTED FROM, never on session id or
# wall-clock: `build_chain_coverage_gate_directive`'s only input is
# `consumed_handoff` (the sole value threaded into its
# `coverage-gate --from-handoff <consumed_handoff>` argv);
# `build_review_brightline_gate_directive`'s input is the FINAL resolved
# argv (`session_id` plus the optional trailing `<git-range>` this module's
# own `resolve_mid_chain_review_scope` derives from `trail_records`/
# `chain_tip_sha`/`is_ancestor`/`session_start_sha`) — the range string is
# what the underlying gate actually walks, so a caller supplying a
# DIFFERENT floor (new trail record landed, chain tip moved) mints a new
# key and misses, even with the same `session_id`. A key match means "this
# exact argv was already resolved for this gate before" — nothing narrower,
# nothing session-scoped, matching the stub's explicit instruction that a
# stale-input memo must MISS rather than serve a wrong verdict.
#
# Storage shape only borrows from `chain_partition_verdict_store.py` (that
# module was removed 2026-08-19, state/kill-ledger.md K-007; the SHAPE it
# established is what this still mirrors)
# (per-record JSON file under `state/ceremony/`, atomic mkstemp+replace,
# hashed filename) — that module's KEYING (session id) is explicitly the
# wrong key for this correctness-bearing skip (its own module docstring:
# "does NOT short-circuit gate execution"), so it is precedent for the
# shape, not reused directly. This memo stores a presence marker only — it
# never fabricates or reads back a verdict VALUE, it only tells `apply`
# "this exact input set was already resolved once," which is what flips
# `already_satisfied` so `_execute_directives` skips the re-walk.
#
# BUILD-TIME IS READ-ONLY; RECORDING IS EXECUTION-TIME ONLY (fix, C4 retry
# #3, docs/plans/2026-08-10-commit-event-5s-cap-and-the-silent-tail.md AC6).
# The first two attempts at this AC had the builders below call
# `record_gate_memo` unconditionally, INSIDE the builder, at directive-BUILD
# time — independent of whether the gate CLI ever actually dispatched, and
# independent of the verdict it returned. `build_directives` is called from
# `brief()`, which serves BOTH `apply()`'s mutating pass AND every read-only
# preview caller — so build-time recording poisoned the memo on a plain
# `brief()` preview before the gate ran even once, and cached a WARN/FAIL
# result as done. Both builders below now perform ONLY a read-only
# `gate_memo_hit` check (never a write) when `repo_root` is supplied; the
# WRITE happens exactly once, from `apply.py::_execute_directives`, via
# `record_gate_verdict_if_passed` below, called ONLY after the gate CLI
# actually dispatched this pass, gated on its captured exit code (and, for
# the coverage gate, its verdict line — a `VERDICT=WARN` exit is 0 but is
# NOT a confirmed pass and must not be memoized).
# ---------------------------------------------------------------------------

GATE_VERDICT_MEMO_RELDIR = "state/ceremony/wsc-gate-verdict-memo"


def _gate_memo_key(gate_id: str, *input_parts: str) -> str:
    """Deterministic filename-safe key over `gate_id` plus every input
    part, in order — order-sensitive by design (a range string and a
    session id are not interchangeable inputs)."""
    raw = "\x1f".join((gate_id, *input_parts))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _gate_memo_path(repo_root: Path, gate_id: str, *input_parts: str) -> Path:
    return Path(repo_root) / GATE_VERDICT_MEMO_RELDIR / f"{_gate_memo_key(gate_id, *input_parts)}.json"


def gate_memo_hit(repo_root: Path, gate_id: str, *input_parts: str) -> bool:
    """True iff a memo record exists for this EXACT `(gate_id, input_parts)`
    key — i.e. this gate was already resolved once for these exact inputs.
    Never raises; an unstattable path degrades to a miss (fail-closed: a
    miss costs a redundant walk, a false hit would skip a needed one)."""
    try:
        return _gate_memo_path(repo_root, gate_id, *input_parts).is_file()
    except OSError:
        return False


def record_gate_memo(repo_root: Path, gate_id: str, *input_parts: str) -> None:
    """Persist the "this exact input set was resolved" marker. Raises on
    I/O failure (mkdir/mkstemp/replace) — mirrors `chain_partition_verdict_
    store.write_verdict_record`'s fail-loud contract; a caller that wants
    memoisation to be best-effort catches this itself."""
    path = _gate_memo_path(repo_root, gate_id, *input_parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"gate_id": gate_id, "input_parts": list(input_parts)}
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.tmp.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_str, str(path))
    except BaseException:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


#: The one live gate directive id `record_gate_verdict_if_passed` knows how
#: to record a memo for: `d-run-review-brightline-gate`, this module's own
#: `build_review_brightline_gate_directive` id. `d-coverage-gate` (the
#: chain-end coverage-verdict directive built by `__init__.py`'s
#: `_build_legacy_coverage_and_trail_directives`) was removed here (K-001,
#: state/kill-ledger.md) along with the directive itself.
_LIVE_GATE_MEMO_DIRECTIVE_IDS = frozenset({"d-run-review-brightline-gate"})

#: C4 (docs/plans/2026-08-15-the-ceremony-tail-stops-lying-about-why-it-
#: failed.md): `__init__.py::build_write_trail_directives` emits ONE
#: `d-write-trail` directive for the single-dict `review` shape, or N
#: `d-write-trail-<index>` directives (index = position in the ORIGINAL
#: list, not a count of qualifying entries) for the list shape — see that
#: function's own docstring. `_LIVE_GATE_MEMO_DIRECTIVE_IDS` is a frozenset
#: of exact strings and therefore CANNOT match the indexed shape by simple
#: membership, so eligibility for a write-trail directive is a prefix test
#: (`_is_write_trail_directive_id`) checked ALONGSIDE, never inside, the
#: frozenset — see `record_gate_verdict_if_passed`'s combined check.
_WRITE_TRAIL_DIRECTIVE_ID_PREFIX = "d-write-trail"


def _is_write_trail_directive_id(gate_id: Optional[str]) -> bool:
    """True for the single-dict id (`d-write-trail`) and every indexed id
    (`d-write-trail-0`, `d-write-trail-1`, ...) `build_write_trail_
    directives` can emit. `gate_id` may be `None` or `""` (a directive
    missing or blanking its `id` key) — never raises, degrades to False
    like every other predicate in this module."""
    if not gate_id:
        return False
    return gate_id == _WRITE_TRAIL_DIRECTIVE_ID_PREFIX or gate_id.startswith(
        _WRITE_TRAIL_DIRECTIVE_ID_PREFIX + "-"
    )

#: Full-length git object id — 40 hex digits (sha1; this fleet has not
#: migrated to sha256 object ids). Deliberately strict (fullmatch, not
#: search): a bare ref name (`"HEAD"`, `"main"`, `"origin/main"`), an
#: abbreviated sha, or a range annotation (`"<sha>^"`) all fail this check
#: and correctly disqualify the range from being memoized — see
#: `record_gate_verdict_if_passed`'s KEY-STALENESS restriction paragraph.
_CONCRETE_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _is_concrete_sha(value: str) -> bool:
    return bool(_CONCRETE_SHA_RE.fullmatch(value))


def record_gate_verdict_if_passed(repo_root: Path, directive: Mapping[str, Any], exit_code: int, stdout: str) -> None:
    """Execution-time-only counterpart to the build-time `gate_memo_hit`
    read (C4 retry #3, docs/plans/2026-08-10-commit-event-5s-cap-and-the-
    silent-tail.md AC6). Called from `apply.py::_execute_directives` — and
    ONLY from there — exactly once per pass, after `directive` actually
    dispatched this pass and `_execute_directives` captured its real exit
    code and stdout. Never called at directive-build time; never called for
    a directive that was blocked, failed to dispatch, or was already
    `already_satisfied` (nothing ran this pass in that last case — there is
    no new verdict to record).

    Recording is CONFIRMED-PASS-ONLY, per directive id:
      - `d-run-review-brightline-gate` (the mid-chain session-scoped
        brightline gate): records on `exit_code == 0` alone. This gate's own
        CLI contract (`review-brightline-gate.py`) has no separate
        WARN/FAIL verdict shape distinct from its exit code — exit 0 means
        "a VERDICT= line was printed" (either `PARTITION-MANDATORY` or
        `single-reviewer-ok`, both fully-resolved answers), exit 1 is a
        usage/die-silent failure. There is nothing further to parse out of
        `stdout` to discriminate a "confirmed pass" from a partial one.

        KEY-STALENESS restriction (settling the question this stub raised;
        review-integrator finding P1, 2026-08-11, corrected the first
        attempt at this predicate — see below): this function records ONLY
        when `directive["args"]` carries the 3-element resolved-range shape
        (`["--session-id", sid, "<floor>..<chain_tip_sha>"]`) AND both halves
        of that range string are concrete, fully-resolved object ids —
        `_is_concrete_sha` below, a 40-hex-digit fullmatch — never a bare
        symbolic ref. The 2-element shape (`["--session-id", sid]`, "no
        floor resolved", the ordinary single-close path per `build_review_
        brightline_gate_directive`'s own docstring) is never recorded: the
        underlying gate falls back to ITS OWN symbolic default range
        (`merge-base(origin/main, HEAD)..HEAD`), which re-resolves against
        whatever commit is HEAD at the NEXT invocation's call time.

        The len-3 check ALONE is not a sound concreteness proxy: UNTIL
        2026-08-11 the ONLY production caller that supplies the four floor
        kwargs (`workstream_complete/__init__.py::_resolve_review_
        brightline_floor_kwargs`) unconditionally passed `chain_tip_sha=
        "HEAD"` — the literal string — so the real mid-chain argv was
        `["--session-id", sid, "<floor-sha>..HEAD"]`: 3 elements, but its
        tip half was exactly the moving-target symbolic ref the
        2-element-shape reasoning above calls disqualifying. A memo keyed on
        the literal string `"HEAD"` would have recorded "resolved" against
        whatever commit HEAD happened to be at record time, then silently
        served that same verdict as a hit against a LATER, different HEAD —
        the identical stale-key hazard this restriction exists to prevent,
        reopened one level down. Checking that both the floor and the tip
        are concrete shas (never a bare ref name) is the layer of defense
        that stayed correct regardless of what the caller did upstream.

        FIXED 2026-08-11 (docs backlink: the mid-chain gate memo was
        provably dead code on the floor-resolved path — this restriction's
        own `_is_concrete_sha(tip)` check NEVER passed, because the tip was
        never concrete): `_resolve_review_brightline_floor_kwargs` now
        resolves `HEAD` to a CONCRETE, frozen sha (`_resolve_head_sha`),
        LAZILY, only once the floor path is actually confirmed taken —
        never at that function's entry, never on the read-only preview path
        that never reaches the floor branch — falling back to the literal
        `"HEAD"` string on any resolution failure (never raising into the
        build path, never fabricating a sha). See that function's own
        `chain_tip_sha` docstring paragraph for the full trade this makes
        (a range anchored at BUILD time, not at gate-run time) and why it is
        the intended fix, not an incidental narrowing. This restriction's
        own behavior is UNCHANGED by that fix — it still requires both
        halves concrete before recording — but the floor-resolved path's
        memo can now actually hit: the mid-chain floor-resolved path pays
        the git-spawn cost of a git rev-parse-backed floor AND (now) a
        git-rev-parse-backed tip, and its memo records once both are
        concrete, exactly as this restriction was always designed to permit.
      - `d-coverage-gate` (the chain-end coverage-verdict directive) was
        removed here (K-001, state/kill-ledger.md) along with the directive
        itself — `_build_legacy_coverage_and_trail_directives` no longer
        builds it, so this function no longer has a verdict-parsing branch
        for it.

    Any other directive id is a no-op — this function is not a general
    dispatch-result hook, only the two live gates named above ever carry a
    memo. Best-effort from the CALLER's perspective (`apply.py` wraps this
    in a try/except so a memo-write I/O failure degrades to "next pass
    re-walks," never to a reported apply failure) — this function itself
    still raises on I/O failure per `record_gate_memo`'s own fail-loud
    contract; the try/except lives at the call site, not here, mirroring
    the same division of responsibility `record_gate_memo` already
    documents for its own callers."""
    gate_id = directive.get("id")
    write_trail = _is_write_trail_directive_id(gate_id)
    if (gate_id not in _LIVE_GATE_MEMO_DIRECTIVE_IDS and not write_trail) or exit_code != 0:
        return
    if write_trail:
        # C4: the write-trail memo key is (session_id, sha_range) — the
        # SAME identity C3 gave the on-disk trail record itself
        # (`ops/review_trail_write.py`) — never the directive's own id,
        # which differs per index for the list shape while the underlying
        # record identity does not. `_gate_memo_key_parts` is set only by
        # `__init__.py::build_write_trail_directives` (never by a bare
        # `_directive(...)` call elsewhere) — an absent/malformed value
        # means the caller didn't opt this directive in (no `session_id`/
        # `repo_root` supplied at build time), so there is nothing to
        # record.
        key_parts = directive.get("_gate_memo_key_parts")
        if not isinstance(key_parts, (list, tuple)) or len(key_parts) != 2:
            return
        record_gate_memo(repo_root, _WRITE_TRAIL_DIRECTIVE_ID_PREFIX, *[str(p) for p in key_parts])
        return
    args = list(directive.get("args") or [])
    if gate_id != "d-run-review-brightline-gate":
        return
    if len(args) != 3:
        return
    floor, sep, tip = args[2].partition("..")
    if not sep or not _is_concrete_sha(floor) or not _is_concrete_sha(tip):
        return
    record_gate_memo(repo_root, gate_id, *args)


# ---------------------------------------------------------------------------
# d-run-review-brightline-gate  (d-run-chain-plan-brightline-gate: removed, K-007)
# (SKILL.md:428-442) — mechanical CLI + verdict parse.
# ---------------------------------------------------------------------------

_REVIEW_BRIGHTLINE_CLI = "review-brightline-gate"
_COVERAGE_GATE_RUNNER_CLI = "wsc-coverage-gate-runner"

#: The review-trail writer, addressed DIRECTLY rather than through
#: `wsc-coverage-gate-runner write-trail`. That subcommand was REMOVED by PM
#: ruling 2026-08-23 (kill `review_trail.write`'s wrapper) and its argparse now
#: offers only `claim-plan`, so every emitted `d-write-trail-*` directive was
#: rejected at argv before the CLI ran -- which gates `d-run-wsc-tail`, i.e. the
#: ceremony's own commit step, on a partitioned close. This CLI is the same
#: native-op trampoline the removed subcommand shelled out to (see
#: `build_write_review_trail_directive`'s docstring), so addressing it directly
#: is the shortest path back to the behaviour the ruling intended to keep.
_REVIEW_TRAIL_WRITER_CLI = "coordinator-write-review-trail"


def build_review_brightline_gate_directive(
    session_id: str,
    *,
    trail_records: Optional[Iterable[Mapping[str, Any]]] = None,
    chain_tip_sha: Optional[str] = None,
    is_ancestor: Optional[Callable[[str, str], bool]] = None,
    session_start_sha: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Mid-chain brightline gate (`WSC_DISPOSITION != chain-terminal`,
    SKILL.md:428-434). `--session-id` scopes the gate's diff to this
    session's own trailer-matched commits — omitting it is the documented
    2026-06-15 multi-EM-brightline-noise failure mode; this builder makes
    it a required positional-keyword argument, not an optional flag, so a
    caller cannot construct the phantom-scope call by omission.

    Range floor (2026-08-08, `docs/plans/2026-08-08-the-second-close-
    re-measures-the-first-c.md`): without a floor, the gate falls back to
    its own default range — `merge-base(origin/main, HEAD)..HEAD` — which
    re-measures every commit a PRIOR close in this same session already
    reviewed (a session that closes two workstreams has its second close
    scored over both). `trail_records`/`chain_tip_sha`/`is_ancestor`/
    `session_start_sha` are all optional and independent of `session_id`:
    supplying all four floors the emitted range at the last-reviewed sha via
    `resolve_mid_chain_review_scope` (already defined in this module — not
    reimplemented here), appended as the gate's trailing positional
    `<git-range>` argument (`review_brightline_gate.py`'s own
    `[--session-id <id>] [<git-range>]` contract already accepts one; no new
    gate-side surface is added). This is ADDITIVE to `--session-id`, never a
    replacement — the floor bounds *where to look*, the trailer still
    decides *what counts*, so a range this wide but a trailer match of zero
    still resolves via the gate's own session-aware floor retry
    (`_resolve_session_floor`, `aff5b6efd`) exactly as it does today.

    Any one of the four kwargs omitted (the default) reproduces TODAY'S
    exact two-element `["--session-id", session_id]` argv — this is the
    ordinary single-close path (nearly every close), which must stay
    byte-identical. This module stays pure/IO-free (D-4): fetching trail
    records and deciding `is_ancestor` remain the caller's job, matching
    every other builder in this package and this module's own Negative-spec
    for `resolve_mid_chain_review_scope`'s existing callers.

    2026-08-08 caller-plumbing note: the production caller is
    `workstream_complete/__init__.py`'s `build_directives`, which supplies
    all four kwargs via `_resolve_review_brightline_floor_kwargs` whenever
    the closing session has at least one prior review-trail record of its
    own, and calls this builder with `session_id` alone otherwise. That
    helper also owns the on-disk field-shape adapter: real records under
    `state/review-trail/` carry `sha_range`, not the `sha_range_head`/`head`
    key `resolve_mid_chain_review_scope` reads, so the caller re-keys each
    record's tip (via `resolve_trail_range_tip`) before handing the list
    over. Both the fetch and that adaptation stay caller-side by this
    module's Negative-spec.

    `repo_root`, when supplied (C4, AC6), consults the gate verdict memo on
    the FINAL resolved argv (`session_id` plus the optional trailing range)
    — see the "Gate verdict memo" section above for the keying rationale —
    via a READ-ONLY `gate_memo_hit` lookup. A prior recorded hit for the
    identical resolved argv sets `already_satisfied=True` instead of
    leaving `apply` to re-walk; a call whose resolved argv differs (a new
    trail record moved the floor, a different session, a different chain
    tip) mints a new key and misses. This builder NEVER WRITES the memo
    itself (fixed, retry #3 — see the section-level docstring above): the
    write happens exactly once, from `apply.py::_execute_directives`, after
    the gate CLI actually dispatched and returned exit 0 this pass.
    Omitting `repo_root` (every pre-C4 caller) reproduces today's
    byte-identical directive."""
    args = ["--session-id", session_id]
    if (
        trail_records is not None
        and chain_tip_sha is not None
        and is_ancestor is not None
        and session_start_sha is not None
    ):
        floor = resolve_mid_chain_review_scope(trail_records, chain_tip_sha, is_ancestor, session_start_sha)
        args.append(f"{floor}..{chain_tip_sha}")
    directive = _directive("d-run-review-brightline-gate", _REVIEW_BRIGHTLINE_CLI, args)
    if repo_root is not None and gate_memo_hit(repo_root, directive["id"], *args):
        directive["already_satisfied"] = True
    return directive


# ---------------------------------------------------------------------------
# d-freeze-and-dispatch-review-partition (SKILL.md:446-459) — freeze +
# integrator mechanics. The reviewer/integrator Agent dispatch itself is
# never modeled here (Negative-spec above).
# ---------------------------------------------------------------------------

_FREEZE_REVIEW_DIFF_CLI = "freeze-review-diff"
_FAN_OUT_INTEGRATOR_CLI = "fan-out-integrator"


class ReviewSlice(NamedTuple):
    slice_id: str
    paths: tuple[str, ...]


def build_review_partition_freeze_directives(range_: str, slices: Iterable[ReviewSlice]) -> list[dict[str, Any]]:
    """One `freeze-review-diff` directive per slice (SKILL.md item 0),
    materializing the frozen per-slice diff BEFORE any reviewer dispatch.
    `range_` MUST be the same session-scoped range the brightline gate
    resolved (`--session-id`-scoped mid-chain, or the chain-end/mid-chain
    diff scope resolved by `resolve_mid_chain_review_scope` below) —
    never a naive `origin/main...HEAD`, per the SKILL's own explicit
    warning against re-pulling concurrent EMs' already-reviewed commits.
    Caller owns range resolution; this function only wires it in."""
    return [
        _directive(
            f"d-freeze-and-dispatch-review-partition-{s.slice_id}",
            _FREEZE_REVIEW_DIFF_CLI,
            ["--range", range_, "--slice-id", s.slice_id, "--paths", *s.paths],
        )
        for s in slices
    ]


def build_review_partition_integrator_directive(spec_tsv_path: str) -> dict[str, Any]:
    """The post-reviewer `fan-out-integrator` call (SKILL.md item 3):
    consumes a TSV of `<slice-id>\\t<reviewer-sidecar-path>\\t<scope-files>`
    rows (one row per slice, sidecar path per the reviewer's own `DONE:
    <path>` return) and emits N parallel `review-integrator` dispatch
    blocks. Composing that TSV from the reviewers' returned sidecar paths
    is the caller's job — this function only names the CLI call once the
    spec file exists on disk."""
    return _directive("d-freeze-and-dispatch-review-partition-integrator", _FAN_OUT_INTEGRATOR_CLI, ["--spec", spec_tsv_path])


def review_partition_resolves_ids(review_partition: dict[str, Any]) -> list[str]:
    """The per-slice `d-freeze-and-dispatch-review-partition-<slice-id>`
    ids (plus `-integrator` when an integrator spec is supplied) that the
    two builders above emit for the same `decisions["review_partition"]`
    — the exact list every review-partition judgment point's dispatching
    dispositions must name in `resolves`.

    `apply`'s gate matches EXACTLY, never by prefix, so the unsuffixed
    `d-freeze-and-dispatch-review-partition` base names nothing: the freeze
    directives never fire and the review partition is silently skipped.

    Mirrors the caller's own build condition (range AND slices both
    present) so an empty/partial partition yields `[]` — an honest "this
    disposition dispatches nothing", not a phantom id.
    """
    if not (review_partition.get("range") and review_partition.get("slices")):
        return []
    slices = [
        ReviewSlice(slice_id=str(s["slice_id"]), paths=tuple(str(p) for p in s["paths"]))
        for s in review_partition["slices"]
    ]
    ids = [d["id"] for d in build_review_partition_freeze_directives(str(review_partition["range"]), slices)]
    if review_partition.get("integrator_spec_tsv"):
        ids.append(build_review_partition_integrator_directive(str(review_partition["integrator_spec_tsv"]))["id"])
    return ids


# ---------------------------------------------------------------------------
# d-run-chain-coverage-gate (SKILL.md:476-486) — mechanical CLI + verdict
# branch. C10 (docs/plans/2026-08-05-coverage-gate-planning-artifact-class.md):
# the underlying `coordinator_core.coverage.run_coverage_gate` this CLI wraps
# no longer resolves a binary VERDICT=UNCOVERED — below the code-partition
# coverage-ratio threshold it resolves VERDICT=WARN, carrying a
# coordinator:review-code remediation OFFER rather than a halt token. This
# builder's own shape (a mechanical CLI directive, no VERDICT parsing) is
# unchanged by C10; it moves in lockstep only in the sense that its
# generated directive now runs a ratio/warn-aware gate underneath, never a
# binary-block one. Remediation-on-WARN (dispatch coordinator:review-code,
# then re-run this same directive) remains an EM Agent-dispatch decision,
# never modeled here. C10 DOES also update
# `coordinator/bin/wsc-coverage-gate-runner.py`'s own `cmd_coverage_gate`
# string-parse of the gate's stdout to match `VERDICT=WARN`; no gap remains
# there as of this commit.
# ---------------------------------------------------------------------------


# `build_chain_coverage_gate_directive` (the `d-run-chain-coverage-gate`
# dead-code twin of `__init__.py`'s LIVE `d-coverage-gate` directive) was
# removed here (K-001, state/kill-ledger.md) — it was already verified
# unreachable (no call site in `build_directives`) before this cut; see the
# kill ledger for the LIVE directive's own removal in `__init__.py`.

# ---------------------------------------------------------------------------
# d-resolve-mid-chain-review-scope (SKILL.md:494-498) — pure resolution
# algorithm, no CLI (Design note above).
# ---------------------------------------------------------------------------


def resolve_mid_chain_review_scope(
    trail_records: Iterable[Mapping[str, Any]],
    chain_tip_sha: str,
    is_ancestor: Callable[[str, str], bool],
    session_start_sha: str,
) -> str:
    """`$LAST_REVIEW_SHA` resolution (SKILL.md:496): iterate `trail_records`
    oldest-to-newest (the caller-supplied ordering — this function does
    not re-sort), keeping the LAST record whose range head passes
    `is_ancestor(head, chain_tip_sha)` — i.e. the most recent qualifying
    record, equivalent to `list-review-trail-records.py | tail -1`
    filtered through the ancestor check. Falls back to `session_start_sha`
    when no record qualifies. `trail_records` entries are read via the
    `sha_range_head` key (falling back to `head` for callers passing a
    narrower shape)."""
    resolved = session_start_sha
    for record in trail_records:
        head = record.get("sha_range_head") or record.get("head")
        if not head:
            continue
        if is_ancestor(str(head), chain_tip_sha):
            resolved = str(head)
    return resolved


# ---------------------------------------------------------------------------
# d-run-doc-fragile-gate-and-dispatch (SKILL.md:500-525) — table-match
# predicate, pure compute. The docs-checker Agent dispatch itself is never
# modeled here (Negative-spec above).
# ---------------------------------------------------------------------------

_DOC_FRAGILE_FILETYPES: Mapping[str, tuple[str, ...]] = {
    "unreal": (".cpp", ".h", ".hpp", ".uproject", ".uplugin", ".Build.cs", ".Target.cs"),
    "unity": (".cs", ".asmdef", "Packages/manifest.json"),
    "godot": (".gd", ".tscn", ".tres"),
}


class DocFragileGate(NamedTuple):
    applies: bool
    matched_subtype: Optional[str]
    matched_files: tuple[str, ...]


def _file_matches_fragile_pattern(path: str, pattern: str) -> bool:
    if pattern.startswith("."):
        return path.endswith(pattern)
    return path == pattern or path.endswith(f"/{pattern}")


def compute_doc_fragile_gate(project_subtypes: Iterable[str], touched_files: Iterable[str]) -> DocFragileGate:
    """Both-must-hold gate (SKILL.md:504-515): (1) `coordinator.local.md`
    declares a doc-fragile `project_subtypes` entry present in the table
    above, AND (2) the diff scope actually touches >=1 of that subtype's
    fragile filetypes. Absent declaration or zero filetype matches ⇒
    `applies=False` (silent skip, no false positives) — matches the
    SKILL's own two negative-spec lines. First matching declared subtype
    wins when a project declares more than one (order of
    `project_subtypes` as given by the caller)."""
    touched = list(touched_files)
    for subtype in project_subtypes:
        filetypes = _DOC_FRAGILE_FILETYPES.get(subtype)
        if not filetypes:
            continue
        matched = tuple(f for f in touched if any(_file_matches_fragile_pattern(f, ft) for ft in filetypes))
        if matched:
            return DocFragileGate(applies=True, matched_subtype=subtype, matched_files=matched)
    return DocFragileGate(applies=False, matched_subtype=None, matched_files=())


# ---------------------------------------------------------------------------
# d-detect-quota-exhausted-dispatch (SKILL.md:536-549) — the fixed
# regex/length-corroboration table, mechanized per D-3's explicit
# `scan_dispatch_output(text) -> bool` instruction.
# ---------------------------------------------------------------------------

_QUOTA_ENVELOPE_MARKER = "QUOTA-EXHAUSTED-DISPATCH:"
_QUOTA_TIME_SIGNATURE_RE = re.compile(r"resets [0-9][0-9]?:[0-9][0-9]", re.IGNORECASE)
_QUOTA_WEAK_PATTERNS = (
    re.compile(r"session limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
)
_QUOTA_WEAK_CORROBORATION_MAX_LEN = 1024


def scan_dispatch_output(text: str) -> bool:
    """SKILL.md's quota-exhausted dispatch detection table (lines
    540-547), mechanized. Definite (no corroboration needed):
    the `QUOTA-EXHAUSTED-DISPATCH:` self-detection envelope, or a
    `resets HH:MM`-shaped time signature (structurally unique to the
    quota-apology shape). Weak (needs `len(text) < 1024` corroboration):
    `session limit` / `rate limit` / `quota`, case-insensitive. Returns
    `True` iff this dispatch return body should be treated as a
    quota-exhaustion event rather than a genuine completed return."""
    if _QUOTA_ENVELOPE_MARKER in text:
        return True
    if _QUOTA_TIME_SIGNATURE_RE.search(text):
        return True
    if len(text) < _QUOTA_WEAK_CORROBORATION_MAX_LEN:
        return any(pattern.search(text) for pattern in _QUOTA_WEAK_PATTERNS)
    return False


# ---------------------------------------------------------------------------
# d-write-review-trail (SKILL.md:552-560) — mechanical CLI, including the
# concrete-SHA input-construction rule (line 554) and the trivial/waived
# negative-spec (lines 559-560, left to the caller: don't call this
# builder at all on row 1/2 sessions).
# ---------------------------------------------------------------------------

_HEX_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def build_write_review_trail_directive(
    sha_range: str,
    reviewer: str,
    scope: str,
    verdict: str,
    diff_loc: int,
    scope_kind: Optional[str] = None,
) -> dict[str, Any]:
    """`wsc-coverage-gate-runner.py write-trail`. `sha_range` MUST be
    `<start>..<end>` with both sides CONCRETE hex SHAs — SKILL.md:554's
    own rule, enforced here as a fail-loud `ValueError` rather than left
    as caller discipline, because a symbolic ref (`<sha>..HEAD`)
    re-resolves at *read* time and silently over-claims coverage (the
    known engine-side defect the SKILL cites,
    `state/improvement-queue/2026-06-30-review-coverage-gate-false-covered-on-tr.yaml`).
    Callers pass `reviewer='waived'` / `verdict='waived'` for the
    PM-waived negative-spec branch — this function does not special-case
    it, it is a valid call shape. Row 1/2 trivial sessions skip trail
    writes entirely per the SKILL's own negative-spec — the caller simply
    does not call this builder in that case; there is no `applies` flag
    to check here.

    `reviewer` and `verdict` MUST agree on whether this record is waived
    (docs/plans/2026-08-05-coverage-gate-planning-artifact-class.md § C14,
    folded from the doe-claude-em memo): `reviewer='waived'` requires
    `verdict='waived'`; any other verdict (including `pending`) requires a
    real, non-waived reviewer. Enforced here, fail-loud via `ValueError`
    mirroring the `sha_range` guard above — NOT coerced, because silently
    rewriting one field to match the other would mint a waiver nobody
    decided on. `verdict='pending'` stays writable with a real reviewer:
    an open review with a named reviewer on the hook closes when that
    reviewer reports back, unlike the incident shape
    (`reviewer='waived'`, `verdict='pending'`) which is a loop nothing
    will ever close. The seam BOTH this builder's callers and any direct
    CLI call cross is `coordinator/bin/coordinator-write-review-trail.py`
    (the sole native-op trampoline `wsc-coverage-gate-runner.py
    write-trail` itself shells out to) — this check is duplicated there
    rather than imported, per this package's own pure/`__init__`-independent
    convention, so a direct CLI call is rejected even when it bypasses this
    builder entirely."""
    if (reviewer == "waived") != (verdict == "waived"):
        raise ValueError(
            f"incoherent reviewer/verdict pair: reviewer={reviewer!r}, verdict={verdict!r} — "
            "reviewer='waived' requires verdict='waived', and verdict='pending' (or any "
            "non-waived verdict) requires a real, non-waived reviewer"
        )
    start, sep, end = sha_range.partition("..")
    if not sep or not _HEX_SHA_RE.match(start) or not _HEX_SHA_RE.match(end):
        raise ValueError(
            f"sha_range must be '<start>..<end>' with two concrete hex SHAs, got {sha_range!r} — "
            "never a symbolic ref like HEAD (SKILL.md:554)"
        )
    args = [
        "--sha-range", sha_range,
        "--reviewer", reviewer,
        "--scope", scope,
        "--verdict", verdict,
        "--diff-loc", str(diff_loc),
    ]
    if scope_kind:
        args += ["--scope-kind", scope_kind]
    return _directive("d-write-review-trail", _REVIEW_TRAIL_WRITER_CLI, args)


# ---------------------------------------------------------------------------
# d-verify-trail-range-termination (SKILL.md:556) — pure disbelief
# predicate, no CLI (Design note above).
# ---------------------------------------------------------------------------

#: A range-endpoint token whose BASE (before any ^/~N ops) is the literal
#: symbolic ref "HEAD" — case-sensitive, matching git's own ref spelling.
#: Mirrors `coordinator_core.coverage._STORED_HEAD_ENDPOINT_RE`: a stored
#: "HEAD" (with or without ^/~N suffixes) re-resolves against whatever
#: commit is HEAD at READ time, not write time, so it is never a fixed
#: anchor a disbelief predicate can trust.
_HEAD_TIP_RE = re.compile(r"^HEAD(?:[~^][0-9]*)*$")


def resolve_trail_range_tip(record: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Resolve the trustworthy commit-SHA tip of a single trail record's
    range, or `(None, reason)` naming why this record cannot corroborate a
    `COVERED` verdict.

    `sha_range_tip`/`tip` (a future, not-yet-observed record shape) are
    honored first if present — this is the forward-compat leg the original
    predicate already named. Every record actually on disk today
    (`state/review-trail/*.json`) carries neither key, only `sha_range`
    (format `"<sha>..<tip>"`, also seen as `"<sha>^..<tip>"`); that field is
    THE working path this function must parse, not a fallback. A `sha_range`
    whose tip is the literal symbolic ref `HEAD` (with or without `^`/`~N`
    suffixes) is rejected — it re-resolves against whatever commit is HEAD
    at gate-run time, not the commit that existed when the record was
    written, and is exactly the shape that produced the verified 2026-07-25
    `work/machine-a/2026-07-21` incident (8 stale `<sha>..HEAD` records
    reading as `VERDICT=COVERED` 12 commits past the newest concrete-range
    record). A `dag:`-prefixed or otherwise unparseable `sha_range` is
    likewise rejected rather than silently skipped with no explanation —
    `classify_untrusted_trail_ranges` below surfaces the reason to a caller
    building a fail-loud diagnostic.

    Returns `(tip, None)` on a trustworthy, concrete tip; `(None, reason)`
    otherwise.
    """
    explicit_tip = record.get("sha_range_tip") or record.get("tip")
    if explicit_tip:
        explicit_tip = str(explicit_tip)
        if _HEAD_TIP_RE.match(explicit_tip):
            return None, f"unterminated ..HEAD range (tip={explicit_tip!r})"
        return explicit_tip, None

    sha_range = record.get("sha_range")
    if not sha_range or not isinstance(sha_range, str):
        return None, "missing sha_range"
    if sha_range.startswith("dag:"):
        return None, f"unparseable dag-shaped range ({sha_range!r})"

    sep = "..." if "..." in sha_range else (".." if ".." in sha_range else None)
    if sep is None:
        return None, f"unparseable range — no '..' separator ({sha_range!r})"
    _start, _sep, end = sha_range.partition(sep)
    end = end.strip()
    if not end:
        return None, f"unparseable range — empty tip ({sha_range!r})"
    if _HEAD_TIP_RE.match(end):
        return None, f"unterminated ..HEAD range ({sha_range!r})"
    return end, None


def classify_untrusted_trail_ranges(
    trail_records: Iterable[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], str]]:
    """Returns `(record, reason)` for every record `verify_trail_range_
    termination` cannot trust — the same rejection set that function skips
    via `continue`, surfaced here so a caller (the coverage-gate CLI wiring)
    can build a fail-loud "N record(s) rejected: <reason>" diagnostic
    instead of silently downgrading trust with no explanation. Pure — reads
    only the records handed to it, no git subprocess of its own."""
    rejected: list[tuple[Mapping[str, Any], str]] = []
    for record in trail_records:
        tip, reason = resolve_trail_range_tip(record)
        if tip is None:
            rejected.append((record, reason or "unknown"))
    return rejected


def verify_trail_range_termination(
    trail_records: Iterable[Mapping[str, Any]],
    chain_tip_sha: str,
    is_ancestor: Callable[[str, str], bool],
) -> bool:
    """SKILL.md's disbelief predicate (line 556): a `COVERED` verdict is
    trustworthy only when at least one trail record's range-tip is at or
    after the chain's newest commit — record *presence* is not coverage
    (the verified 2026-07-25 `work/machine-a/2026-07-21` incident: 8 stale
    `<sha>..HEAD` records, `VERDICT=COVERED`, while the newest
    concrete-range record stopped 12 commits short). Returns `True` iff
    at least one record's tip is `chain_tip_sha` itself or an ancestor
    relationship confirms it is at/after the chain tip.

    Every record is resolved through `resolve_trail_range_tip` — an
    unterminated `..HEAD` range, a `dag:`-prefixed range, or any other
    unparseable `sha_range` all resolve to `(None, reason)` and are treated
    identically to "no tip": this predicate never trusts them, it never
    trusts an untrustworthy record merely because git failed to disprove
    it.
    """
    for record in trail_records:
        tip, _reason = resolve_trail_range_tip(record)
        if tip is None:
            continue
        if tip == chain_tip_sha or is_ancestor(chain_tip_sha, tip):
            return True
    return False


#: Verdict strings that do NOT discharge a mandatory review scale — a
#: `pending` record is a promise, not a completed review; a `waived`
#: record is a PM-authorized skip, not evidence a reviewer actually ran.
#: Case-insensitive on read (mirrors `coordinator-write-review-trail.py`'s
#: own `--verdict` values, which this module does not itself validate the
#: full set of — see docstring below for why).
_NON_DISCHARGING_VERDICTS = frozenset({"pending", "waived"})


#: Scope-kind values that never discharge ANYTHING in this chain-terminal
#: path, unconditionally — mirrors `coverage.build_reviewed_set`'s Phase 1
#: classification (review-integrator finding W1, 2026-08-06): "integration"
#: is skipped entirely, not reopened by this module.
#:
#: 2026-08-07 correction (audit `state/audits/2026-08-07-wsc-chain-gate-
#: counts-doc-only-commits.md`, Q4's "second gap"): "plan" is DELIBERATELY
#: NOT in this set any more. It used to be — a `scope_kind: "plan"` record
#: was rejected outright, exactly like "integration" — but that made the
#: chain-terminal discharge path structurally incapable of crediting a plan
#: review for a PLANNING-classified commit, even a session's own honest,
#: self-owned plan review of its own PLANNING commit (chain_code_shas keeps
#: PLANNING commits IN the obligation set per AC9 — see
#: `_record_membership_shas`'s own "membership-vs-coverage split" docstring
#: note). `_record_membership_shas` now credits a "plan" record, but
#: ONLY against `chain_planning_sha_set` (the PLANNING-classified subset of
#: `chain_code_shas`, supplied by the caller) — never against a plain CODE
#: commit, mirroring `coverage._credit_from_kind_partition`'s own kind-aware
#: crediting for the session-oracle path. A caller that omits
#: `chain_planning_sha_set` (`None`, the default) sees byte-identical
#: behavior to before this correction: a "plan" record still credits
#: nothing. A record with no `scope_kind` key at all is the legacy/absent
#: shape and is treated as an ordinary diff record (unchanged).
_NON_CODE_SCOPE_KINDS = frozenset({"integration"})


class ChainAttributionWindow(NamedTuple):
    """C6a (docs/plans/2026-08-15-composition-invocation-budgets.md): the
    zero-further-spawn accelerator for `_record_membership_shas`'s foreign-
    session narrowing — an OPTIONAL layer on top of `narrow_foreign_shas`,
    never a replacement for it (a caller that omits `chain_window` sees
    byte-identical behaviour to before this type existed; a record whose
    range escapes the window still falls back to `narrow_foreign_shas`).

    `commit_map` is the caller-resolved result of ONE
    `chain_attribution.bulk_commit_attribution_map(range_str, cwd, run)`
    walk over a single covering range (the intended shape is `merge-
    base..HEAD`, resolved once per close, not once per trail record) — a
    `Mapping[str, chain_attribution.CommitAttribution]` keyed on full
    40-char LOWERCASE hex sha, matching `git log --format=%H`'s own output
    case (no defensive re-lowering here; the same no-re-lowering posture
    `_record_membership_shas` already takes on `chain_dag_sha_set` /
    `chain_code_sha_set`, both resolved the same way).

    `grep_attributed_for_session` is `session_id -> iterable-of-shas`,
    the caller's closure over `chain_attribution.bulk_grep_attributed_shas`
    scoped to the SAME covering range `commit_map` was built from — never a
    different range, or the window-coverage precondition below is violated
    silently. `_collect_discharging_range_shas` wraps whatever this
    resolves to in ITS OWN per-call memo (keyed on `session_id`), so an
    unmemoized caller callable still pays at most once per DISTINCT
    session_id seen in one pass, not once per record — this is what turns
    the narrowing's git-spawn count from O(records) to O(distinct
    sessions).

    WINDOW-COVERAGE PRECONDITION (`chain_attribution.foreign_shas_from_
    window`'s own docstring: a sha absent from `window` reads as FOREIGN,
    proving coverage is the caller's job): `_record_membership_shas`
    discharges this itself, per record, BEFORE ever calling
    `foreign_shas_from_window` — a record's already-resolved `raw` sha set
    is checked as a subset of `commit_map`'s keys first. Only when every
    sha in `raw` is present does the window fast path fire; a record naming
    even one sha `commit_map` does not cover falls back to the per-record
    `narrow_foreign_shas` callable instead — never silently treated as
    foreign, and never a correctness change from the pre-window behaviour
    on that record (this is the escaping-range case C6a's AC5 fixture
    exercises)."""

    commit_map: Mapping[str, Any]
    grep_attributed_for_session: Callable[[Optional[str]], Any]



#: A trail record's `sha_range` spelled as the single commit `<sha>`: either
#: `<sha>^..<sha>` or `<sha>~1..<sha>` (the `~1` spelling exists because
#: cmd.exe eats a literal `^` in argv on Windows). Hex on BOTH sides and
#: identical, abbreviated (>=7) or full -- the live corpus writes both. A
#: symbolic or mismatched endpoint falls through to the resolver rather than
#: being reasoned about here.
_SINGLE_COMMIT_RANGE_RE = re.compile(
    r"^(?P<base>[0-9a-fA-F]{7,40})(?:\^|~1)\.\.(?P=base)$"
)


def _single_commit_range_base(sha_range: str) -> Optional[str]:
    """Return the lowercased sha a single-commit `sha_range` denotes, or `None`
    when the range is any other shape.

    Used only to decide whether a range's resolved sha set is knowable without
    calling the resolver (see `_record_membership_shas`). Deliberately narrow:
    it recognises exactly the two spellings the write path emits, both sides
    full-hex and identical. Anything else -- an abbreviated endpoint, a
    multi-commit range, a symbolic ref -- returns `None` and is resolved the
    ordinary way.

    Negative-spec:
      - Does NOT decide membership. It answers "what would `git rev-list` return
        for this range", nothing more; the caller still applies every membership
        and narrowing rule to the result.
      - Does NOT widen what counts as a single-commit range to make more records
        skippable. The skip is only sound because the answer is forced.
    """
    match = _SINGLE_COMMIT_RANGE_RE.match(sha_range)
    if match is None:
        return None
    return match.group("base").lower()


def _prefix_hits_chain_set(base: str, chain_dag_sha_set: set[str]) -> bool:
    """Whether any chain-DAG sha could be what `base` resolves to.

    `base` may be abbreviated, so this is a prefix test, not equality --
    `git rev-list <base>^..<base>` can only ever yield a sha carrying `base`
    as a prefix, which makes "no chain sha starts with `base`" equivalent to
    "the intersection this record needs is empty". Full-length `base` degrades
    to plain equality, since a 40-hex prefix match IS equality.
    """
    if len(base) == 40:
        return base in chain_dag_sha_set
    return any(sha.startswith(base) for sha in chain_dag_sha_set)


def _record_membership_shas(
    record: Mapping[str, Any],
    resolve_range_shas: Callable[[str], Any],
    chain_dag_sha_set: set[str],
    chain_code_sha_set: set[str],
    narrow_foreign_shas: Optional[Callable[[str, Optional[str]], Any]] = None,
    chain_planning_sha_set: Optional[set[str]] = None,
    chain_window: Optional[ChainAttributionWindow] = None,
) -> Optional[set[str]]:
    """Resolve one trail record's contribution to the chain-membership
    union, or `None` if this record contributes nothing.

    Two different sets answer two different questions:

    - MEMBERSHIP — is this record about THIS chain at all? — tests the
      record's raw resolved range against `chain_dag_sha_set`, the chain's
      UNFILTERED DAG sha set (every commit in the chain, ceremony
      bookkeeping and handoff-authoring commits included). This is an
      INTERSECTION test, not a subset test (2026-08-06 subset-to-
      intersection correction, review-integrator finding B4/F4): a record
      contributes iff it names AT LEAST ONE commit that is a DAG member of
      this chain — a subset requirement rejected every honest multi-commit
      review range that straddled even one concurrent PEER commit on this
      fleet's interleaving-sessions norm (91 of 1213 on-disk records, live
      measured). This does not reopen the original peer-leakage defect that
      motivated the (then-subset) membership check: that leakage came from
      the retired tip-reaching leg discharging on bare TIP REACHABILITY,
      with no range-containment test at all. A peer's own `X^..X` record
      over their own commit intersects `chain_dag_sha_set` in the EMPTY SET
      (their commit is not a DAG member of this chain at all) and still
      contributes nothing here.
    - COVERAGE — which obligations does a membership-passing record
      discharge? — the returned contribution is `raw & chain_code_sha_set`,
      the intersection with the FILTERED, code-bearing set — never more than
      the code-bearing commits the record actually names, regardless of how
      many non-chain or bookkeeping commits its raw range also spans.

    Trust filter applied BEFORE the resolver is ever consulted (review-
    integrator findings B1/B2/W1 — reuse the codebase's already-hardened
    validators rather than a second, narrower copy):

    1. `scope_kind` in `_NON_CODE_SCOPE_KINDS` (`"integration"` only, as of
       the 2026-08-07 correction — see that constant's own docstring) —
       rejected outright, mirrors `coverage.build_reviewed_set`'s own
       Phase-1 classification. `"plan"` is no longer in this set: it falls
       through the same membership machinery as `"diff"`, capped at
       `chain_planning_sha_set` rather than `chain_code_sha_set` (see the
       tail of this function).
    2. `coverage.SAFE_RANGE` — the shared argument-injection validator
       (blocks a leading-dash `sha_range` reaching `git rev-list` argv as a
       flag); also enforces the `..`/`...` separator shape, so a bare-sha
       `sha_range` is rejected here rather than becoming an unbounded
       ancestry walk.
    3. `coverage._record_range_has_stored_head` — the same stored-`HEAD`
       defence `resolve_trail_range_tip` and `coverage.build_reviewed_set`
       apply to these exact on-disk records, citing the verified 2026-07-25
       `work/machine-a/2026-07-21` incident (8 stale `<sha>..HEAD` records
       reading as COVERED). Checked on the RAW STRING, before
       `resolve_range_shas` is ever called — a caller can therefore assert
       rejection without needing a resolver double at all.

    `narrow_foreign_shas`, optional, is `(sha_range, session_id) ->
    iterable-of-shas` — applied when `record["scope"]` is one of
    `coverage._FOREIGN_STRIPPED_SCOPES` (`session`/`chain`/
    `workstream-close-auto`), mirroring `coverage.build_reviewed_set`'s own
    session-narrowing (review-integrator finding W2): a session/chain-scoped
    record only credits commits belonging to ITS OWN session, never a
    different session's commits its range happens to also span. Any failure
    resolving the foreign-set (the callable raises) rejects the record
    entirely — fail-closed, never a silent guess at what to strip. `None`
    (the default) skips this narrowing — every existing caller that omits it
    sees byte-identical behavior to before this parameter existed.

    No admission path re-credits a narrowed commit. A `reviewer_attestation`
    parameter briefly did (`attested_shas`, DR-321); it admitted nothing in
    ~761 records and was removed with the refusal apparatus it depended on
    (K-010, state/kill-ledger.md). The chain-ancestry-waiver mechanism it
    replaced is retired too (K-005, 2026-08-16). This plan's Anti-scope
    forbids reproducing either under a new name: a per-session store
    consulted by both writer and reader is wider than any one record's own
    evidence and uncountable for an admission ratchet.

    `chain_planning_sha_set`, optional, is the PLANNING-classified subset of
    `chain_code_sha_set` (2026-08-07 correction — see `_NON_CODE_SCOPE_KINDS`'s
    own docstring above). A `scope_kind: "plan"` record's contribution is
    `raw & chain_planning_sha_set` — never `raw & chain_code_sha_set` — so a
    plan review can discharge a PLANNING commit but never a plain CODE one,
    mirroring `coverage._credit_from_kind_partition`'s own kind-aware
    crediting. `None` (the default) means the caller has not supplied a
    planning set; a "plan" record then credits nothing, byte-identical to
    this function's behavior before this correction existed.
    coverage. `None` (the default) skips this entirely — every existing
    caller that omits it sees byte-identical behavior to before this
    parameter existed. This module performs no filesystem read of its own to
    resolve either store — both are the caller's (`wsc-coverage-gate-
    runner.py`) job to resolve and inject, per this package's pure/IO-free
    convention (D-4).

    `chain_window`, optional, is a `ChainAttributionWindow` (see that type's
    own docstring for the full contract) — a zero-further-spawn accelerator
    for the `narrow_foreign_shas` branch only, engaged solely when this
    record's already-resolved `raw` sha set is fully covered by `chain_
    window.commit_map`. `None` (the default, and every escaping-range
    record even when `chain_window` is supplied) falls back to calling
    `narrow_foreign_shas(sha_range, session_id)` exactly as before this
    parameter existed — byte-identical behavior, never a second, divergent
    narrowing rule.

    sha comparison is case-insensitive, full-hex only — `resolve_range_shas`
    (the live `git rev-list` injection) and both chain sha sets (resolved
    via `git log`) always emit full 40-char lowercase hex, so no
    abbreviated-prefix matching is needed here (contrast the retired
    `_sha_matches` helper this replaces, which existed only to bridge
    abbreviated-vs-full SHA spellings the tip-comparison path could see)."""
    scope_kind = record.get("scope_kind")
    if scope_kind in _NON_CODE_SCOPE_KINDS:
        return None
    sha_range = record.get("sha_range")
    if not sha_range or not isinstance(sha_range, str):
        return None
    if not _SAFE_RANGE.match(sha_range):
        return None
    if _record_range_has_stored_head(sha_range):
        return None
    if _single_commit_range_base(sha_range) is not None:
        # A `<sha>^..<sha>` / `<sha>~1..<sha>` record can only ever resolve to
        # {<sha>}, so the `raw & chain_dag_sha_set` test three lines below is
        # already decided before the resolver runs: outside the chain DAG set,
        # this record contributes nothing no matter what git would say. Skipping
        # the call is therefore behavior-preserving, not a heuristic -- the only
        # records it declines to resolve are ones whose membership would have
        # been discarded on the next line.
        #
        # This is a spawn-amplification fix, not a micro-optimization. The live
        # resolver is a `git rev-list` subprocess and this loop runs once per
        # trail record: measured 2026-08-18 on a chain-terminal close, 5392
        # spawns and 224s wall against a chain DAG set holding 9 commits. The
        # per-range memo above the resolver never helps here because every
        # single-commit range is a distinct cache key.
        _base = _single_commit_range_base(sha_range)
        if _base is not None and not _prefix_hits_chain_set(_base, chain_dag_sha_set):
            return None
    try:
        raw = {str(s).lower() for s in resolve_range_shas(sha_range)}
    except Exception:  # noqa: BLE001 - a broken resolver must reject, never crash
        return None
    if not raw:
        return None
    if not (raw & chain_dag_sha_set):
        return None
    if narrow_foreign_shas is not None:
        # Review: review-integrator — B1 (2026-08-06, brightline-discharge
        # round4). An unrecognized `scope` value (not one of
        # `_FOREIGN_STRIPPED_SCOPES`) used to fall through the narrowing
        # entirely and receive full-width credit — a fail-OPEN gap under
        # intersection membership, where narrowing IS the trust boundary.
        # The write path (`review_trail_write._VALID_SCOPES`) enforces the
        # closed three-value set, but `_load_trail_records` unions the
        # on-disk archive, which is not so guaranteed (e.g. an archived
        # record whose `scope` is a comma-joined file list). Fail-closed on
        # an unrecognized `scope`, mirroring `_NON_CODE_SCOPE_KINDS`'s own
        # posture, rather than silently skipping the narrowing.
        if record.get("scope") not in _FOREIGN_STRIPPED_SCOPES:
            return None
        session_id = record.get("session_id")
        # C6a window fast path (docs/plans/2026-08-15-composition-
        # invocation-budgets.md): fires only when the window provably
        # covers every sha this record could contribute — see
        # `ChainAttributionWindow`'s docstring for the precondition this
        # subset check discharges. Any other shape (no window supplied, or
        # an escaping range) falls straight through to the original
        # per-record `narrow_foreign_shas` spawn, unchanged.
        if chain_window is not None and raw <= chain_window.commit_map.keys():
            try:
                grep_attributed = frozenset(
                    str(s).lower() for s in chain_window.grep_attributed_for_session(session_id)
                )
                foreign = {
                    str(s).lower()
                    for s in chain_attribution.foreign_shas_from_window(
                        raw, session_id, chain_window.commit_map, grep_attributed,
                    )
                }
            except Exception:  # noqa: BLE001 - a broken window resolver must reject, never crash
                return None
        else:
            try:
                foreign = {str(s).lower() for s in narrow_foreign_shas(sha_range, session_id)}
            except Exception:  # noqa: BLE001 - a broken narrowing must reject, never crash
                return None
        raw = raw - foreign
        if not (raw & chain_dag_sha_set):
            return None
    if scope_kind == "plan":
        if chain_planning_sha_set is None:
            return None
        return raw & chain_planning_sha_set
    return raw & chain_code_sha_set


# ---------------------------------------------------------------------------
# d-run-ubt-pending-check (SKILL.md:564-566) — predicate + single CLI
# call, applies-to-cwd shape.
# ---------------------------------------------------------------------------

_UBT_PENDING_CHECK_CLI = "scan_unresolved_ubt_records"


def build_ubt_pending_check_directive(applies: bool, since_sha: str) -> Optional[dict[str, Any]]:
    """No-op for non-UE repos — `applies` is the caller-resolved
    applies-to-cwd predicate (whether
    `coordinator_core/ops/scan_unresolved_ubt_records.py` is reachable
    for this repo), mirroring every other applies-to-cwd conditional in
    this module family (`build_ubt_pending_check_directive` does not
    probe the filesystem itself). `since_sha` is the already-resolved
    `git merge-base origin/main HEAD` (or `HEAD~1` fallback) SHA — this
    function performs no git resolution of its own."""
    if not applies:
        return None
    return _directive("d-run-ubt-pending-check", _UBT_PENDING_CHECK_CLI, ["--mode", "pending", "--since", since_sha])


# ---------------------------------------------------------------------------
# d-classify-dispatch-shape (SKILL.md:568-587, Step 2.9b) — mechanical
# CLI call + pass-through, never blocks.
# ---------------------------------------------------------------------------

_CLASSIFY_DISPATCH_SHAPE_CLI = "classify-dispatch-shape"


def build_classify_dispatch_shape_directive(plan_file: Optional[str]) -> Optional[dict[str, Any]]:
    """Step 2.9b: fires only when a governing plan with a `## Tasks`
    spine (or fan-out TSV) is known — `plan_file` absent/falsy means "no
    plan governs this session," the SKILL's own skip-silently condition,
    and this builder returns `None` rather than a directive naming an
    empty path. Any offer text on stderr is pass-through surfaced in the
    Step 4 summary by the caller — this builder does not read or
    interpret the CLI's output, only names the call."""
    if not plan_file:
        return None
    return _directive("d-classify-dispatch-shape", _CLASSIFY_DISPATCH_SHAPE_CLI, ["--plan-file", plan_file])

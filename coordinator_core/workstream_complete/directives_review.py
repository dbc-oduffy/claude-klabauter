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

Source: coordinator-content-repo coordinator/skills/workstream-complete/SKILL.md,
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
    coordinator/bin/freeze-review-diff.py
        -> d-freeze-and-dispatch-review-partition's per-slice
        directives[].cli.
    coordinator/bin/classify-dispatch-shape.py
        -> d-classify-dispatch-shape's directives[].cli.

Design note — directives[] vs pure compute (same discriminator
`directives_session_hygiene.py` documents): a census row becomes a
`directives[].cli` entry only when a real, on-disk CLI exists for the
caller to invoke. Rows with no such CLI — the diff-shape row-selection
table (d-review-scale-decide), the mid-chain diff-scope resolution
algorithm (d-resolve-mid-chain-review-scope), the doc-fragile domain-lens
predicate (d-run-doc-fragile-gate-and-dispatch), the quota-exhaustion
scan (d-detect-quota-exhausted-dispatch) — are exposed as
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
      subprocess itself. `resolve_mid_chain_review_scope` takes the
      already-fetched trail records and an injected `is_ancestor`
      callable — fetching the records and deciding `git merge-base
      --is-ancestor` are the caller's job, consistent with every other
      builder module in this package staying pure/IO-free (D-4).
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
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional

from coordinator_core.commit_ledger.oracle import OracleReport
from coordinator_core.ops.ceremony.wsc_disposition import PREDECESSOR_CONSUMED, canonicalize

#: Generator-provenance declaration (coordinator_core/ops/generator_census).
#: record_gate_memo() below writes state/ceremony/wsc-gate-verdict-memo/<hash>.json
#: (hashed-key filename, one per distinct (gate_id, resolved-inputs) pair) -- a
#: data-dependent set of tracked artifacts, not a fixed one, so this is a
#: corpus-mutator declaration rather than GENERATES.
MUTATES = ["state/ceremony/wsc-gate-verdict-memo/*.json"]


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
#: keys (`review_partition`, `classify_dispatch_plan_file`).
#: Declared empty here — rather than omitted — so every `directives_*.py`
#: sibling carries the same `FREE_VALUE_KEYS` contract point per AC3
#: (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md),
#: and a future caller-side `decisions` param added to this module has one
#: obvious place to register its keys.
FREE_VALUE_KEYS: tuple[str, ...] = ("superseding_review",)


class ReviewScaleDecision(NamedTuple):
    row: Optional[int]
    scale: str
    #: `None` iff `resolved is False` — the same "not measured" state `row`
    #: already carries, on the field a caller is most likely to read alone.
    #: `False` here means MEASURED and not mandatory; a reader that saw only
    #: `False` could not tell a genuinely small diff from an unmeasured
    #: 976-LOC/26-commit one, and the arm is silent (rc=0), so the gate the
    #: skill body calls mandatory failed open. Reported by
    #: example-retrieval-repo-ue-addon-em, 2026-08-31. `None` is falsy, so every
    #: truthiness test downstream is unchanged; only a reader that
    #: distinguishes it — or a JSON consumer, which now sees `null` beside
    #: `scale: "unresolved"` — sees any difference.
    partition_mandatory: Optional[bool]
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
        row=None, scale="unresolved", partition_mandatory=None,
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
    `row=None`, `scale="unresolved"`, `partition_mandatory=None`) rather
    than defaulting toward a specific row. `partition_mandatory` is `None`,
    not `False`, on that outcome: an unresolved decision must not
    manufacture a mandatory partition it has no evidence for, AND it must
    not report the absence as a measured negative. `False` said both at
    once, and a caller reading that field alone could not tell an unmeasured
    976-LOC/26-commit diff from a small one. This
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
    #
    # HOW BOTH ARE MEASURED, cited here because the definition crosses a repo
    # boundary and the divergence was accruing with no citation at either end.
    # coordinator-content-repo-em asked us to adopt or counter their C5 ruling (cross-repo/
    # archive/2026-08-29-coordinator-content-repo-em-review-scale-ships-no-brightline-
    # inputs.md, ask (b)): sum PER-OWNED-COMMIT diffs (`<sha>~1..<sha>` each),
    # never a range over oldest..newest. ADOPTED, 2026-08-31 — and this engine
    # already computed it that way independently, for the same reason. See
    # `__init__.py :: _measure_session_review_scale_inputs`, whose own
    # Negative-spec is "never widen either leg back to a branch-scoped range":
    # on a shared branch a range spans every peer commit interleaved between
    # base and HEAD. Their measured cost of getting it wrong by hand was 33,246
    # gross LOC reported where the per-owned-commit sum is 16,037.
    #
    # So the two planes agree, and the citation is the deliverable — a reader
    # who finds a number that disagrees with these should suspect a RANGE
    # measurement before suspecting either engine.
    effective_code_loc = code_loc if code_loc is None else code_loc * baton_multiplier
    # 2026-08-20 (same memo as the `code_loc_resolved_zero` note below): the
    # commit-count arm is a proxy for ACCUMULATED RISK, and a commit with a
    # zero-line diff carries none. `baton-assemble apply` scaffold commits
    # are the reported instance — nine of one session's sixteen, every one
    # at `diff_loc: 0`, pushing a doc-only close past the five-line threshold.
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
        # THE REASON STRING NAMES WHICH ARM TRIPPED, AND SAYS SO WHEN AN INPUT
        # WAS NEVER MEASURED. `brightline_known_true` is an OR of three
        # independently sufficient arms, so row 4 is legitimately reachable
        # with one input still `None` -- a resolved proxy that trips is
        # dispositive, and no later measurement can un-trip it. That verdict is
        # sound. What was NOT sound was printing the unmeasured input as though
        # it were a measurement: `code_loc=None` inside "big-diff brightline
        # hit" reads as a measurement that came back empty -- see this
        # function's own "HOW BOTH ARE MEASURED" comment above for the
        # 33,246-vs-16,037 incident that cost (not restated here). Naming the
        # tripped arm tells the reader the verdict does not depend on what is
        # missing.
        tripped = [
            name
            for name, value, floor in (
                ("code_loc", effective_code_loc, _BRIGHTLINE_LOC),
                ("commits", effective_commit_count, _BRIGHTLINE_COMMITS),
                ("surfaces", effective_surface_count, _BRIGHTLINE_SURFACES),
            )
            if value is not None and value >= floor
        ]
        unmeasured = [
            name
            for name, value in (
                ("code_loc", code_loc), ("commits", commit_count), ("surfaces", surface_count),
            )
            if value is None
        ]
        measured_note = ", ".join(
            f"{name}={value}"
            for name, value in (
                ("code_loc", code_loc), ("commits", commit_count), ("surfaces", surface_count),
            )
            if value is not None
        )
        unmeasured_fixed = [name for name in unmeasured if name != "code_loc"]
        unmeasured_parts = []
        if unmeasured_fixed:
            unmeasured_parts.append(
                f"{', '.join(unmeasured_fixed)} not measured, and cannot change this verdict"
            )
        if "code_loc" in unmeasured:
            unmeasured_parts.append(
                "code_loc not measured; a later code_loc==0 measurement could still suppress this verdict"
            )
        unmeasured_note = f"; {'; '.join(unmeasured_parts)}" if unmeasured_parts else ""
        return ReviewScaleDecision(
            row=4, scale="partitioned", partition_mandatory=True, commit_message_names_change=False,
            reason=(
                f"big-diff brightline hit on {'+'.join(tripped)} "
                f"({measured_note}{multiplier_note}{scope_note}){unmeasured_note}"
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
    raw = "\x1f".join((gate_id, *input_parts))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _gate_memo_path(repo_root: Path, gate_id: str, *input_parts: str) -> Path:
    return Path(repo_root) / GATE_VERDICT_MEMO_RELDIR / f"{_gate_memo_key(gate_id, *input_parts)}.json"


def gate_memo_hit(repo_root: Path, gate_id: str, *input_parts: str) -> bool:
    try:
        return _gate_memo_path(repo_root, gate_id, *input_parts).is_file()
    except OSError:
        return False


def record_gate_memo(repo_root: Path, gate_id: str, *input_parts: str) -> None:
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


_LIVE_GATE_MEMO_DIRECTIVE_IDS = frozenset({"d-run-review-brightline-gate"})

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
    dispatch-result hook, only the live gate named above ever carries a
    memo. Best-effort from the CALLER's perspective (`apply.py` wraps this
    in a try/except so a memo-write I/O failure degrades to "next pass
    re-walks," never to a reported apply failure) — this function itself
    still raises on I/O failure per `record_gate_memo`'s own fail-loud
    contract; the try/except lives at the call site, not here, mirroring
    the same division of responsibility `record_gate_memo` already
    documents for its own callers."""
    gate_id = directive.get("id")
    if gate_id not in _LIVE_GATE_MEMO_DIRECTIVE_IDS or exit_code != 0:
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


_REVIEW_BRIGHTLINE_CLI = "review-brightline-gate"
_COVERAGE_GATE_RUNNER_CLI = "wsc-coverage-gate-runner"


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


_FREEZE_REVIEW_DIFF_CLI = "freeze-review-diff"


class ReviewSlice(NamedTuple):
    slice_id: str
    paths: tuple[str, ...]


def build_review_partition_freeze_directives(range_: str, slices: Iterable[ReviewSlice]) -> list[dict[str, Any]]:
    return [
        _directive(
            f"d-freeze-and-dispatch-review-partition-{s.slice_id}",
            _FREEZE_REVIEW_DIFF_CLI,
            ["--range", range_, "--slice-id", s.slice_id, "--paths", *s.paths],
        )
        for s in slices
    ]


def review_partition_resolves_ids(review_partition: dict[str, Any]) -> list[str]:
    if not (review_partition.get("range") and review_partition.get("slices")):
        return []
    slices = [
        ReviewSlice(slice_id=str(s["slice_id"]), paths=tuple(str(p) for p in s["paths"]))
        for s in review_partition["slices"]
    ]
    ids = [d["id"] for d in build_review_partition_freeze_directives(str(review_partition["range"]), slices)]
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
    touched = list(touched_files)
    for subtype in project_subtypes:
        filetypes = _DOC_FRAGILE_FILETYPES.get(subtype)
        if not filetypes:
            continue
        matched = tuple(f for f in touched if any(_file_matches_fragile_pattern(f, ft) for ft in filetypes))
        if matched:
            return DocFragileGate(applies=True, matched_subtype=subtype, matched_files=matched)
    return DocFragileGate(applies=False, matched_subtype=None, matched_files=())


_QUOTA_ENVELOPE_MARKER = "QUOTA-EXHAUSTED-DISPATCH:"
_QUOTA_TIME_SIGNATURE_RE = re.compile(r"resets [0-9][0-9]?:[0-9][0-9]", re.IGNORECASE)
_QUOTA_WEAK_PATTERNS = (
    re.compile(r"session limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
)
_QUOTA_WEAK_CORROBORATION_MAX_LEN = 1024


def scan_dispatch_output(text: str) -> bool:
    """SKILL.md's quota-exhausted dispatch detection table (lines
    540-547), mechanized. Definite (no corroboration needed):
    the `QUOTA-EXHAUSTED-DISPATCH:` self-detection envelope anchored at
    the start of the return body (a dispatch that genuinely exhausted
    quota emits the envelope as its own leading output; a reviewer that
    quotes or reports the marker mid-prose while flagging an injection
    attempt does not anchor it, so that case is a mention, not a use),
    or a `resets HH:MM`-shaped time signature (structurally unique to the
    quota-apology shape, and not a string an attacker gains anything by
    quoting). Weak (needs `len(text) < 1024` corroboration): `session
    limit` / `rate limit`, case-insensitive. Returns `True` iff this
    dispatch return body should be treated as a quota-exhaustion event
    rather than a genuine completed return."""
    if text.lstrip().startswith(_QUOTA_ENVELOPE_MARKER):
        return True
    if _QUOTA_TIME_SIGNATURE_RE.search(text):
        return True
    if len(text) < _QUOTA_WEAK_CORROBORATION_MAX_LEN:
        return any(pattern.search(text) for pattern in _QUOTA_WEAK_PATTERNS)
    return False


#: A range-endpoint token whose BASE (before any ^/~N ops) is the literal
#: symbolic ref "HEAD" — case-sensitive, matching git's own ref spelling.
#: Mirrors `coordinator_core.coverage._STORED_HEAD_ENDPOINT_RE`: a stored
#: "HEAD" (with or without ^/~N suffixes) re-resolves against whatever
#: commit is HEAD at READ time, not write time, so it is never a fixed
#: anchor a disbelief predicate can trust.
_HEAD_TIP_RE = re.compile(r"^HEAD(?:[~^][0-9]*)*$")


def resolve_trail_range_tip(record: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
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


_CLASSIFY_DISPATCH_SHAPE_CLI = "classify-dispatch-shape"


def build_classify_dispatch_shape_directive(plan_file: Optional[str]) -> Optional[dict[str, Any]]:
    if not plan_file:
        return None
    return _directive("d-classify-dispatch-shape", _CLASSIFY_DISPATCH_SHAPE_CLI, ["--plan-file", plan_file])


# direction, never added to `_LIVE_GATE_MEMO_DIRECTIVE_IDS`, and always
# D2 -- failure is silence, not a message: `UNAVAILABLE`, an exception, or
# `_review_dimension_check`'s own `UNAVAILABLE` leg before any subprocess

_CLOSE_COVERAGE_ADVISORY_ID = "d-close-coverage-advisory"

#: genuinely new `CONSUMES_MANIFEST` member would need a real
_CLOSE_COVERAGE_ADVISORY_INERT_CLI = _REVIEW_BRIGHTLINE_CLI

_CLOSE_COVERAGE_ADVISORY_PREFIX = "review coverage: "


def _render_close_coverage_advisory_message(detail: str) -> Optional[str]:
    """D4: reuses `_message_size.MESSAGE_PROSE_CAP_BYTES` (220 bytes) as the
    byte cap for this advisory's rendered string -- one register, one cap,
    never a second measurement path. Register (docs/wiki/guard-messaging.md
    § Register): one fact, stated once, declaratively; no B1-B6 move, no
    override key, no unlock pointer -- `detail` is `_review_dimension_
    check`'s own plain uncovered-count/example-sha prose, carrying none of
    those. Returns `None` (never a truncated/lossy render) when the
    composed string would exceed the cap -- an over-cap render is treated
    exactly like D2's other unavailable arms: silence, not a degraded
    message."""
    from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES

    message = f"{_CLOSE_COVERAGE_ADVISORY_PREFIX}{detail}"
    if len(message.encode("utf-8")) > MESSAGE_PROSE_CAP_BYTES:
        return None
    return message


def _emit_close_coverage_advisory(
    changed_files: list[str], diff_base: Optional[str], repo_root: Optional[Path]
) -> None:
    """Computes and (on a real gap) prints the advisory. Never raises --
    every arm of D2 (UNAVAILABLE, an exception, a missing store) degrades
    to silence, not a message, matching the dimension's own fail-closed-to-
    UNAVAILABLE contract plus this advisory's own additional never-raise
    guarantee (D2 names "an exception" as its own arm, distinct from the
    dimension's internal UNAVAILABLE verdict)."""
    if not changed_files or not diff_base or repo_root is None:
        return
    try:
        from coordinator_core.ops.gate_dimension_review import _review_dimension_check
        from coordinator_core.ops.gate_validate_invocable import Verdict

        result = _review_dimension_check(list(changed_files), diff_base, repo_root)
    except Exception:
        return
    if result.verdict != Verdict.FAIL:
        return
    message = _render_close_coverage_advisory_message(result.detail)
    if message is None:
        return
    # DIAGNOSTIC, therefore stderr: this op's stdout contract is its JSON, and
    # UNCHANGED by the stream move: the message is still deliberately not
    print(message, file=sys.stderr)


def build_close_coverage_advisory_directive(
    changed_files: list[str], diff_base: Optional[str], repo_root: Optional[Path]
) -> dict[str, Any]:
    """Builds (and, on a real coverage gap, prints) the warn-only close
    coverage advisory -- see this section's module-level docstring for the
    full D1-D4 contract. `changed_files`/`diff_base` are resolved by the
    CALLER (never here, never widening `_review_dimension_check`'s own
    signature) from facts already paid for elsewhere in the close; `None`/
    empty inputs take the silent UNAVAILABLE path with no subprocess spawn
    at all.

    Always returns an `already_satisfied=True` directive with no
    `depends_on` edge -- `apply.py::_execute_directives` lands it in
    `report["landed"]` immediately, on every pass, never dispatching its
    (inert, reused) `cli` and never able to block, fail, or gate the run
    (D3, AC2)."""
    _emit_close_coverage_advisory(changed_files, diff_base, repo_root)
    return _directive(
        _CLOSE_COVERAGE_ADVISORY_ID,
        _CLOSE_COVERAGE_ADVISORY_INERT_CLI,
        [],
        depends_on=None,
        already_satisfied=True,
    )


# ---------------------------------------------------------------------------
# Stranded-run superseding review (docs/plans/2026-10-01-completion-receipts.md,
# C10). A plan that was run (a `<stem>.workflow.mjs.emitted.json` sits beside
# it) but never review-stamped can be stamped against a superseding record
# the EM writes over the run's commit range. `decisions["superseding_review"]`
# carries the record's inputs; `decisions["jp-stranded-run-superseding-review"]`
# = `record` fires the two directives below.
# ---------------------------------------------------------------------------

SUPERSEDING_RECORD_DIRECTIVE_ID = "d-record-superseding-review"
SUPERSEDING_STAMP_DIRECTIVE_ID = "d-mint-superseding-stamp"
STRANDED_RUN_JUDGMENT_POINT_ID = "jp-stranded-run-superseding-review"
_SUPERSEDING_REVIEW_KEY = "superseding_review"


def is_stranded_plan(plan_path: Path) -> bool:
    """True when the plan carries no `review_stamp`, is not `implemented`, and
    has a `<stem>.workflow.mjs.emitted.json` beside it. An unreadable plan is
    not stranded."""
    import yaml

    from coordinator_core.frontmatter.primitives import split_frontmatter

    emitted = plan_path.with_name(plan_path.stem + ".workflow.mjs.emitted.json")
    try:
        if not emitted.is_file():
            return False
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return False
    split = split_frontmatter(text)
    try:
        fm = (yaml.safe_load(split.fm_text) or {}) if split is not None else {}
    except yaml.YAMLError:
        return False
    if not isinstance(fm, dict):
        return False
    return not fm.get("review_stamp") and str(fm.get("status") or "") != "implemented"


def _superseding_slice(decisions: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The validated `decisions["superseding_review"]` mapping, or `None` when
    the caller has not supplied one. Raises `ValueError` on a malformed one."""
    raw = decisions.get(_SUPERSEDING_REVIEW_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"decisions[{_SUPERSEDING_REVIEW_KEY!r}] must be a mapping")
    commit_range = raw.get("commit_range")
    if not (
        isinstance(commit_range, dict)
        and commit_range.get("base")
        and commit_range.get("head")
    ):
        raise ValueError(
            f"decisions[{_SUPERSEDING_REVIEW_KEY!r}].commit_range needs both base and head"
        )
    sidecars = raw.get("wave_sidecar_paths")
    if not isinstance(sidecars, list) or not sidecars:
        raise ValueError(
            f"decisions[{_SUPERSEDING_REVIEW_KEY!r}].wave_sidecar_paths must be a non-empty list"
        )
    return raw


def superseding_review_resolves_ids(decisions: Mapping[str, Any]) -> list[str]:
    """The directive ids a `record` disposition resolves: both ids once
    `decisions["superseding_review"]` is supplied, else `[]`."""
    if _superseding_slice(decisions) is None:
        return []
    return [SUPERSEDING_RECORD_DIRECTIVE_ID, SUPERSEDING_STAMP_DIRECTIVE_ID]


def build_superseding_review_directives(
    *, plan_rel: str, sid: str, repo_root: Path, decisions: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """`d-record-superseding-review` then `d-mint-superseding-stamp`, the
    second reading the first's printed record path through the
    `{d-record-superseding-review.entry_path}` token. `[]` when
    `decisions["superseding_review"]` is absent."""
    slice_ = _superseding_slice(decisions)
    if slice_ is None:
        return []
    commit_range = slice_["commit_range"]
    record_args = [
        "--plan", plan_rel,
        "--session-id", sid,
        "--base", str(commit_range["base"]),
        "--head", str(commit_range["head"]),
        "--repo-root", str(repo_root),
    ]
    for sidecar in slice_["wave_sidecar_paths"]:
        record_args += ["--wave-sidecar", str(sidecar)]
    if slice_.get("prep_sidecar"):
        record_args += ["--prep-sidecar", str(slice_["prep_sidecar"])]
    if slice_.get("stage_returns") is not None:
        record_args += ["--stage-returns-json", json.dumps(slice_["stage_returns"])]
    if slice_.get("supersedes"):
        record_args += ["--supersedes", str(slice_["supersedes"])]
    return [
        _directive(
            SUPERSEDING_RECORD_DIRECTIVE_ID,
            "record-superseding-review",
            record_args,
            depends_on=STRANDED_RUN_JUDGMENT_POINT_ID,
        ),
        _directive(
            SUPERSEDING_STAMP_DIRECTIVE_ID,
            "review-stamp",
            [
                "mint",
                "--plan", plan_rel,
                "--superseding-record", f"{{{SUPERSEDING_RECORD_DIRECTIVE_ID}.entry_path}}",
                "--repo-root", str(repo_root),
            ],
            depends_on=STRANDED_RUN_JUDGMENT_POINT_ID,
        ),
    ]


def wire_stranded_run_superseding_review(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    *,
    plan_path: Optional[Path],
    plan_rel: Optional[str],
    sid: str,
    repo_root: Path,
    decisions: Mapping[str, Any],
) -> None:
    """Appends the stranded-run judgment point (and, once the EM supplies
    `decisions["superseding_review"]`, its two directives) when the governing
    plan is stranded. A no-op for any other plan."""
    if plan_path is None or not plan_rel or not is_stranded_plan(plan_path):
        return
    from coordinator_core.workstream_complete.judgments import (
        build_stranded_run_superseding_review_judgment_point,
    )

    judgment_points.append(
        build_stranded_run_superseding_review_judgment_point(
            superseding_review_resolves_ids(decisions)
        )
    )
    directives.extend(
        build_superseding_review_directives(
            plan_rel=plan_rel, sid=sid, repo_root=repo_root, decisions=decisions
        )
    )

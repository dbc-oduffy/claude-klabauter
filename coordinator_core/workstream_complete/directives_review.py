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

Source: coordinator-claude coordinator/skills/workstream-complete/SKILL.md,
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
    coordinator/bin/wsc-coverage-gate-runner.py
        (brightline-gate --from-handoff / coverage-gate --from-handoff /
        write-trail) -> d-run-chain-plan-brightline-gate's,
        d-run-chain-coverage-gate's, and d-write-review-trail's
        directives[].cli.
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
      package staying pure/IO-free (D-4). The live caller is
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

from coordinator_core.ops.ceremony.wsc_disposition import PREDECESSOR_CONSUMED, canonicalize
from coordinator_core.coverage import (
    SAFE_RANGE as _SAFE_RANGE,
    _FOREIGN_STRIPPED_SCOPES,
    _record_range_has_stored_head,
)

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

#: The two literal `verdict=` values `review_brightline_gate.py::_from_handoff_main`
#: prints on its `BRIGHTLINE ...` stdout line (see that function's tail,
#: `verdict = "PARTITION-MANDATORY" if reviewers_required >= 2 else
#: "single-reviewer-ok"`). `chain_partition_verdict` below is that string,
#: consumed verbatim — never re-derived from raw chain metrics here (see
#: `decide_review_scale`'s docstring).
_CHAIN_VERDICT_PARTITION_MANDATORY = "PARTITION-MANDATORY"
_CHAIN_VERDICT_SINGLE_REVIEWER_OK = "single-reviewer-ok"

#: Public alias of `_CHAIN_VERDICT_PARTITION_MANDATORY` (review-integrator
#: finding, P3, 2026-08-06) — `wsc-coverage-gate-runner.py` duplicated this
#: literal as its own `_CHAIN_VERDICT_PARTITION_MANDATORY` because this
#: module's copy was private; now that `chain_partition_verdict_discharged`
#: is itself a public export of this module, the verdict string it gates on
#: is exported alongside it so a future rename only has one string to catch,
#: not two. The private name above is untouched — this is an additive alias,
#: not a rename.
CHAIN_VERDICT_PARTITION_MANDATORY = _CHAIN_VERDICT_PARTITION_MANDATORY


def _unresolved(reason: str) -> ReviewScaleDecision:
    return ReviewScaleDecision(
        row=None, scale="unresolved", partition_mandatory=False,
        commit_message_names_change=False, reason=reason, resolved=False,
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
    chain_partition_verdict: Optional[str] = None,
    baton_count: Optional[int] = None,
    commit_count_scope: Optional[str] = None,
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

    Scope of each input (SESSION-scoped = this closing session's own diff;
    CHAIN-scoped = the full chain DAG a chain-terminal close accumulates
    across, walked once by the brightline gate, not re-walked here):
      - `commit_count`, `surface_count` — SESSION-scoped. Feed the row-4
        big-diff brightline predicate below alongside `code_loc`. A
        session-scoped brightline on a chain terminal means "this final
        session's own diff is big," not "the diff accumulated across the
        whole chain is big" — deliberately: see `chain_partition_verdict`
        for the chain-scoped question row 6 actually answers.
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
      - `chain_partition_verdict` — CHAIN-scoped. Consulted only when
        `chain_disposition` canonicalizes to a chain terminal
        (`wsc_disposition.PREDECESSOR_CONSUMED`). This is the verbatim
        `verdict=` value already emitted by
        `review_brightline_gate.py::_from_handoff_main`'s
        `BRIGHTLINE ...` stdout line — `"PARTITION-MANDATORY"` or
        `"single-reviewer-ok"` — which that directive derives via
        `max(plan_oracle, chain_oracle, session_oracle)` capped at 4 over
        the full chain DAG. This function does NOT re-derive that verdict
        from raw chain metrics (no `chain_loc`/`chain_commits`/
        `chain_surfaces` params here) — doing so would ship a second
        oracle with a different composition rule and no reconciliation
        with the one `review_brightline_gate.py` already computes. `None`
        means "not yet resolved," never "trivial."
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

    Producer/consumer seam (C5, 2026-08-03 plan
    docs/plans/2026-08-03-chain-end-review-scale-wiring.md, the Staff Engineer finding 2):
    this function is the CONSUMER of `chain_partition_verdict`; the PRODUCER
    is `coordinator/bin/wsc-coverage-gate-runner.py::cmd_brightline_gate`,
    which already parses the `BRIGHTLINE ...` line via `_parse_brightline_line`
    / `_BRIGHTLINE_RE` and now prints an `ACTION:` stderr line naming the
    exact `decisions["chain_partition_verdict"]` key the EM must carry into
    the NEXT `wsc.brief()`/`wsc.apply()` call. Before this producer-side edit,
    every test exercising rows 5/6 was satisfiable via a hand-authored
    `decisions=` fixture with zero real traffic ever reaching this branch in
    production — a green suite proving self-consistency with its own
    fixtures, not that it matches what the real producer emits. See
    `state/lessons/0000-00-00-green-tests-can-encode-the-bug-verify-producer-
    consumer-key.yaml` (scope: universal): "grep a corpus of real producer
    artifacts for the exact key the consumer reads before trusting any test
    that exercises that path" — done here by locating the ALREADY-EXISTING
    `_parse_brightline_line` parser rather than authoring a second one.

    Row 6 fires on `chain_partition_verdict ==
    _CHAIN_VERDICT_PARTITION_MANDATORY` alone (chain-scoped, independent
    of the session-scoped row-4 brightline) — the accumulated-over-many-
    small-sessions case the source memo describes would never trip a
    session-scoped predicate. Row 5 fires when the chain terminal's
    verdict resolved but is NOT mandatory. Row 4 fires on the
    session-scoped brightline alone, regardless of chain-terminal status
    — including on a chain terminal whose `chain_partition_verdict` is
    still `None` or unrecognized: the row-4 brightline check is evaluated
    BEFORE those chain-terminal-unresolved returns (fixed 2026-08-10; the
    prior code returned unresolved on `chain_partition_verdict is None`
    ahead of ever evaluating row 4, silently under-reporting scale to
    `partition_mandatory=False` on a chain-terminal close whose own
    session-scoped diff emphatically hit the brightline). Precedence is
    checked in row order 6, 4, then the chain-terminal-unresolved returns
    (`chain_partition_verdict` not yet resolved, or resolved to something
    other than `single-reviewer-ok`/`PARTITION-MANDATORY`), then the
    row-4-inputs-unresolved return, then 5, 3, 1, 2 — the chain-terminal
    verdict's own unresolved reason is surfaced ahead of a *simultaneously*
    unresolved row-4 input set, because "the chain-scoped verdict this
    terminal close needs is missing" is the more actionable message for a
    chain terminal than "some session-scoped LOC/commit/surface count is
    missing," and matches the pre-fix reporting shape for that specific
    combination. Any branch whose own predicate needs an unresolved input
    to be ruled out returns the unresolved outcome rather than falling
    through to a lower-precedence row.

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
    effective_commit_count = commit_count if commit_count is None else commit_count * baton_multiplier
    effective_surface_count = surface_count if surface_count is None else surface_count * baton_multiplier

    brightline_known_true = (
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
        if chain_partition_verdict == _CHAIN_VERDICT_PARTITION_MANDATORY:
            return ReviewScaleDecision(
                row=6, scale="partitioned", partition_mandatory=True, commit_message_names_change=False,
                reason="chain-terminal AND the chain-scoped brightline gate verdict is PARTITION-MANDATORY",
            )
        if brightline_known_true:
            return _row4_decision()
        if chain_partition_verdict is None:
            return _unresolved(
                "chain-terminal close but chain_partition_verdict is not yet resolved "
                "(rows 5/6 cannot be ruled out)"
            )
        if chain_partition_verdict != _CHAIN_VERDICT_SINGLE_REVIEWER_OK:
            return _unresolved(
                f"chain-terminal close with an unrecognized chain_partition_verdict "
                f"{chain_partition_verdict!r} (expected "
                f"{_CHAIN_VERDICT_PARTITION_MANDATORY!r} or {_CHAIN_VERDICT_SINGLE_REVIEWER_OK!r})"
            )
        if not brightline_resolved:
            return _row4_inputs_unresolved()
        # chain_partition_verdict == _CHAIN_VERDICT_SINGLE_REVIEWER_OK, row 4 ruled out.
        return ReviewScaleDecision(
            row=5, scale="code-reviewer", partition_mandatory=False, commit_message_names_change=False,
            reason="chain-terminal with a resolved, non-mandatory chain-scoped brightline verdict",
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
# Storage shape only borrows from `chain_partition_verdict_store.py`
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
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_str, str(path))
    except BaseException:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


#: The two live gate directive ids `record_gate_verdict_if_passed` knows how
#: to record a memo for. `d-run-review-brightline-gate` is this module's own
#: `build_review_brightline_gate_directive` id; `d-coverage-gate` is
#: `__init__.py`'s LIVE inline chain-end coverage-gate directive id (see
#: `build_chain_coverage_gate_directive`'s "DEAD CODE, VERIFIED" docstring
#: paragraph for why the memo lives on this id and not
#: `d-run-chain-coverage-gate`).
_LIVE_GATE_MEMO_DIRECTIVE_IDS = frozenset({"d-run-review-brightline-gate", "d-coverage-gate"})

#: The verdict token `wsc-coverage-gate-runner.py::cmd_coverage_gate` prints
#: on its own `VERDICT=...` stdout line when the underlying gate resolved
#: below the code-partition coverage-ratio threshold. `cmd_coverage_gate`
#: exits 0 on THIS token too (C10: WARN is an offer, never a refusal) — so
#: exit code alone cannot discriminate a confirmed COVERED pass from a WARN
#: that still owes remediation. A WARN must never be memoized (stub's own
#: correctness bar): the listed commits are still owed a real review, and a
#: cached "already resolved" here would silently swallow that debt on the
#: next pass.
_COVERAGE_GATE_WARN_TOKEN = "VERDICT=WARN"

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
      - `d-coverage-gate` (the live chain-end coverage gate, keyed on
        `consumed_handoff` — see `build_chain_coverage_gate_directive`'s
        "DEAD CODE, VERIFIED" paragraph): records on `exit_code == 0` AND
        `stdout` does NOT contain `VERDICT=WARN`. `cmd_coverage_gate` exits
        0 on both `VERDICT=COVERED` and `VERDICT=WARN` (C10) — only the
        stdout token discriminates a confirmed pass from an offer-only
        WARN. `exit_code == 2` (INDETERMINATE) and any other non-zero exit
        never reach this function at all (see `_execute_directives`: a
        non-zero exit lands the directive in `failed`/`degraded`, not
        `landed`, and this function is only ever called from the `landed`
        branch).

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
    if gate_id not in _LIVE_GATE_MEMO_DIRECTIVE_IDS or exit_code != 0:
        return
    args = list(directive.get("args") or [])
    if gate_id == "d-run-review-brightline-gate":
        if len(args) != 3:
            return
        floor, sep, tip = args[2].partition("..")
        if not sep or not _is_concrete_sha(floor) or not _is_concrete_sha(tip):
            return
        record_gate_memo(repo_root, gate_id, *args)
        return
    # gate_id == "d-coverage-gate"
    if _COVERAGE_GATE_WARN_TOKEN in (stdout or ""):
        return
    if len(args) < 3 or args[1] != "--from-handoff":
        return
    record_gate_memo(repo_root, gate_id, args[2])


# ---------------------------------------------------------------------------
# d-run-review-brightline-gate / d-run-chain-plan-brightline-gate
# (SKILL.md:428-442) — mechanical CLI + verdict parse.
# ---------------------------------------------------------------------------

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


def build_chain_plan_brightline_gate_directive(consumed_handoff: str) -> dict[str, Any]:
    """Chain-terminus two-oracle brightline gate (`WSC_DISPOSITION ==
    chain-terminal`, SKILL.md:436-442) — the two-oracle chain+plan
    enforcement path over the closing handoff, wrapped by
    `wsc-coverage-gate-runner.py brightline-gate --from-handoff`."""
    return _directive(
        "d-run-chain-plan-brightline-gate",
        _COVERAGE_GATE_RUNNER_CLI,
        ["brightline-gate", "--from-handoff", consumed_handoff],
    )


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


def build_chain_coverage_gate_directive(
    consumed_handoff: str, *, repo_root: Optional[Path] = None
) -> dict[str, Any]:
    """Chain-end coverage gate, DAG mode, wrapped by
    `wsc-coverage-gate-runner.py coverage-gate --from-handoff`. C10: the
    gate this CLI wraps now resolves VERDICT=WARN (never VERDICT=UNCOVERED)
    below the code-partition coverage-ratio threshold. Re-running after WARN
    remediation is simply calling this builder again with the same
    `consumed_handoff` — no separate directive id for the re-check.

    DEAD CODE, VERIFIED (C4 retry #3, docs/plans/2026-08-10-commit-event-5s-
    cap-and-the-silent-tail.md AC6): `workstream_complete/__init__.py`'s
    `build_directives` never calls this builder — greped every call site.
    The LIVE chain-end coverage gate is `__init__.py`'s own
    `_build_legacy_coverage_and_trail_directives`, which builds an inline
    `d-coverage-gate` directive with the byte-identical CLI/argv under a
    DIFFERENT id (see that module's own Negative-spec: wiring this builder
    too would double-dispatch the same call under two ids). The gate
    verdict memo therefore lives on `d-coverage-gate` in `__init__.py`
    (read-only hit check threaded through `_build_legacy_coverage_and_trail_
    directives`'s own `repo_root` param; the write happens in
    `apply.py::_execute_directives` via `record_gate_verdict_if_passed`
    below, keyed the same way — `("d-coverage-gate", consumed_handoff)`).
    This builder's own `repo_root` support below is kept only so its
    exported shape stays self-consistent with its sibling
    `build_review_brightline_gate_directive` and so the pre-existing regression
    tests for it keep exercising the primitive; no production caller reaches
    it with `repo_root` supplied.

    `repo_root`, when supplied, consults the gate verdict memo (READ-ONLY,
    via `gate_memo_hit`) keyed on `consumed_handoff` — this builder's sole
    input, and the exact value threaded into the CLI argv above. A prior
    recorded hit for the SAME `consumed_handoff` sets `already_satisfied=
    True`; a different `consumed_handoff` mints a new key and misses. This
    builder never writes the memo itself. Omitting `repo_root` (every
    pre-C4 caller) reproduces today's byte-identical directive."""
    directive = _directive(
        "d-run-chain-coverage-gate",
        _COVERAGE_GATE_RUNNER_CLI,
        ["coverage-gate", "--from-handoff", consumed_handoff],
    )
    if repo_root is not None and gate_memo_hit(repo_root, directive["id"], consumed_handoff):
        directive["already_satisfied"] = True
    return directive


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
    folded from the coordinator-claude-em memo): `reviewer='waived'` requires
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
        "write-trail",
        "--sha-range", sha_range,
        "--reviewer", reviewer,
        "--scope", scope,
        "--verdict", verdict,
        "--diff-loc", str(diff_loc),
    ]
    if scope_kind:
        args += ["--scope-kind", scope_kind]
    return _directive("d-write-review-trail", _COVERAGE_GATE_RUNNER_CLI, args)


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


# ---------------------------------------------------------------------------
# chain_partition_verdict_discharged — C13
# (docs/plans/2026-08-05-coverage-gate-planning-artifact-class.md, AC20/AC21,
# folded from cross-repo/inbox/2026-08-06-coordinator-claude-em-partition-mandatory-
# verdict-does-not-refuse-the-cap.md). Pure predicate, no CLI/IO of its own
# (D-4) — the caller (`wsc-coverage-gate-runner.py::cmd_brightline_gate`)
# resolves `trail_records` via its own `_load_trail_records()` (live+archive
# union, `coordinator_core.ops.list_review_trail_records`) and supplies them
# here already parsed.
#
# 2026-08-06 chain-scoping correction (review-integrator finding, P1,
# 2026-08-06 chain re-verification): the ORIGINAL two-leg design ("legacy
# tip-reaching leg (a)" plus "union-coverage leg (b)") scoped BOTH legs by
# `tip == chain_tip_sha or is_ancestor(chain_tip_sha, tip)` — a record's
# range-tip is *somewhere later on the branch than* the chain tip. Live
# instrumentation against `state/handoffs/2026-08-06-eliminate-claude-klabauter-s-
# non-test-subprocess-spawn-population.md` (chain tip `72eee33c6`, 15 chain
# code shas) proved that condition is not a chain-scoping check at all on
# this fleet's ONE SHARED `work/{machine}/{date}` branch: every record any
# CONCURRENT PEER session wrote after this chain's tip also satisfies it,
# regardless of whether that peer's range ever touched a single commit of
# this chain. All 11 records that discharged leg (a) belonged to two
# unrelated peer sessions; zero belonged to this chain. The same condition
# was also UNSATISFIABLE for the predecessor session purely because no peer
# happened to have written a later record yet — same chain, same evidence,
# opposite verdict, decided by peer timing. Tip-reaching is therefore
# neither necessary nor sufficient for "this record reviewed this chain."
#
# Both legs are replaced by ONE within-chain-membership leg — see
# `chain_partition_verdict_discharged`'s own docstring below.
#
# 2026-08-06 membership-vs-coverage split (review-integrator finding, P1,
# second chain re-verification): the subset test above, in its FIRST cut,
# tested a record's raw resolved range against `chain_code_shas` itself —
# the same set `_resolve_chain_code_shas` derives by taking the chain's DAG
# set MINUS `exhaust_set` (ceremony bookkeeping) MINUS handoff-authoring-
# only commits. But `resolve_range_shas` (`git rev-list <sha_range>` over a
# reviewer's raw, un-filtered range string) returns EVERY commit in the
# span, bookkeeping and handoff-authoring included. An honest, ordinary
# whole-chain review range (`base..tip`, the documented "union-of-one" norm)
# that happens to span even one ceremony commit therefore failed that
# subset test and was rejected WHOLESALE — recreating, one level down, the
# exact unsatisfiability this rewrite exists to close, just relocated from
# "no peer wrote a later record yet" to "your own honest range touched a
# housekeeping commit". Membership and coverage are different questions and
# now use different sets: MEMBERSHIP ("is this record about THIS chain at
# all?") tests the record's raw range against `chain_dag_shas` — the
# chain's UNFILTERED DAG sha set, bookkeeping and handoff-authoring commits
# included (`_resolve_chain_dag_shas`, reusing `_resolve_dag_candidates`'s
# same DAG derivation before its exhaust/handoff filtering is applied). A
# peer session's commits are never in this chain's DAG set at all, so peer
# leakage stays closed; an honest in-chain range spanning a ceremony commit
# now passes membership. COVERAGE ("which obligations does it discharge?")
# still intersects the record's raw range with `chain_code_shas` — the
# filtered, code-bearing set — so a bookkeeping commit in the record's range
# confers no coverage credit, it merely no longer poisons an otherwise-
# legitimate record's membership.
#
# 2026-08-06 subset-to-intersection correction (review-integrator finding,
# B4/F4, third chain re-verification): even the MEMBERSHIP test above, as a
# raw-range SUBSET-of-`chain_dag_shas` test, remained too strict on this
# fleet's one-shared-branch-many-sessions norm — six concurrent sessions
# interleave every 1-3 commits, so an honest multi-commit `base..tip` review
# range routinely straddles a PEER'S commit (never a member of this chain's
# DAG at all) and was rejected wholesale even though every one of ITS OWN
# reviewed commits belonged to this chain. Live check: 91 of 1213 on-disk
# records are multi-commit ranges; under the subset rule, all 91 failed.
# MEMBERSHIP is now an INTERSECTION test, not a subset test: a record
# contributes iff its raw resolved range shares AT LEAST ONE commit with
# `chain_dag_shas` — never partially credited beyond that, since the actual
# coverage contribution is still `raw & chain_code_shas` (below), the
# intersection with the FILTERED, code-bearing set. This does not reopen
# the original peer-leakage defect: that leakage came from the retired leg
# (a) discharging on bare TIP REACHABILITY, with no range-containment test
# at all — any record postdating the chain tip on the shared branch
# qualified, whether or not it named a single chain commit. A peer's own
# `X^..X` record over their own commit intersects `chain_dag_shas` in the
# EMPTY SET (their commit is not a DAG member of this chain) and still
# contributes nothing under the intersection rule. Reviewer-count/
# scope/session-id narrowing (`narrow_foreign_shas` below) is applied on
# TOP of this as defense-in-depth, matching `coverage.build_reviewed_set`'s
# own foreign-session stripping for session/chain-scoped records — never
# relied upon alone to keep this rule sound.
# ---------------------------------------------------------------------------

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
#: PLANNING commits IN the obligation set per AC9 — see this module's
#: "membership-vs-coverage split" note above `chain_partition_verdict_
#: discharged`). `_record_membership_shas` now credits a "plan" record, but
#: ONLY against `chain_planning_sha_set` (the PLANNING-classified subset of
#: `chain_code_shas`, supplied by the caller) — never against a plain CODE
#: commit, mirroring `coverage._credit_from_kind_partition`'s own kind-aware
#: crediting for the session-oracle path. A caller that omits
#: `chain_planning_sha_set` (`None`, the default) sees byte-identical
#: behavior to before this correction: a "plan" record still credits
#: nothing. A record with no `scope_kind` key at all is the legacy/absent
#: shape and is treated as an ordinary diff record (unchanged).
_NON_CODE_SCOPE_KINDS = frozenset({"integration"})


def _record_membership_shas(
    record: Mapping[str, Any],
    resolve_range_shas: Callable[[str], Any],
    chain_dag_sha_set: set[str],
    chain_code_sha_set: set[str],
    narrow_foreign_shas: Optional[Callable[[str, Optional[str]], Any]] = None,
    vouched_shas: Optional[Callable[[Optional[str]], Any]] = None,
    chain_planning_sha_set: Optional[set[str]] = None,
) -> Optional[set[str]]:
    """Resolve one trail record's contribution to the chain-membership
    union, or `None` if this record contributes nothing.

    Two different sets answer two different questions (see this module's
    "membership-vs-coverage split" note above `chain_partition_verdict_
    discharged`):

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

    `vouched_shas`, optional, is `(session_id) -> iterable-of-shas` — the
    read side of the write side's `ForeignSessionRangeRefused` escape hatch
    (`coordinator_core.ops.review_trail_write._guard_foreign_session_range`'s
    chain-ancestry-waiver relaxation), sourced from the gate-minted
    chain-ancestry waiver store (`chain_ancestry_waivers.py`). 2026-08-08
    (docs/plans/2026-08-08-vouch-free-review-coverage-gates.md § C2): the
    PM-vouch evidence source this parameter formerly also carried
    (`review_trail_write._PM_VOUCH_WAIVER_DIRNAME`,
    `coordinator_core/session/review_trail_vouch.py`) is deleted outright —
    this parameter's NAME and CALLABLE SIGNATURE are unchanged, it now
    carries only chain-ancestry waivers. A sha this callable names is
    subtracted OUT of the foreign-strip set BEFORE it is removed from
    `raw` — i.e. a foreign-attributed commit carrying a matching
    chain-ancestry waiver is NOT narrowed away. Any failure resolving the
    waived set (the callable raises) is treated as "no waiver" — fail-safe
    toward narrowing, never toward silently manufacturing coverage.

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
        try:
            foreign = {str(s).lower() for s in narrow_foreign_shas(sha_range, record.get("session_id"))}
        except Exception:  # noqa: BLE001 - a broken narrowing must reject, never crash
            return None
        if vouched_shas is not None and foreign:
            try:
                vouched = {str(s).lower() for s in vouched_shas(record.get("session_id"))}
            except Exception:  # noqa: BLE001 - a broken vouch lookup must narrow, never crash
                vouched = set()
            foreign = foreign - vouched
        raw = raw - foreign
        if not (raw & chain_dag_sha_set):
            return None
    if scope_kind == "plan":
        if chain_planning_sha_set is None:
            return None
        return raw & chain_planning_sha_set
    return raw & chain_code_sha_set


def _collect_discharging_range_shas(
    trail_records: Iterable[Mapping[str, Any]],
    resolve_range_shas: Callable[[str], Any],
    chain_dag_shas: Iterable[str],
    chain_code_shas: Iterable[str],
    narrow_foreign_shas: Optional[Callable[[str, Optional[str]], Any]] = None,
    vouched_shas: Optional[Callable[[Optional[str]], Any]] = None,
    chain_planning_shas: Optional[Iterable[str]] = None,
) -> set[str]:
    """The union's raw coverage set: every code-obligation sha named by a
    trail record's resolved range, restricted to records this module trusts
    as evidence — non-`pending`/non-`waived` verdict AND within-chain
    membership (`_record_membership_shas`, non-empty INTERSECTION with
    `chain_dag_shas`, coverage credit capped at `chain_code_shas`).
    `chain_dag_shas` gates MEMBERSHIP (is this record about this chain at
    all — unfiltered, bookkeeping included); `chain_code_shas` gates what a
    membership-passing record actually CONTRIBUTES (filtered, code-bearing
    only) — see `_record_membership_shas`'s own docstring. A record failing
    either check contributes nothing — never a KeyError, never a silent
    guess at what it might have covered.

    Short-circuits (review-integrator finding B3/F3 — spawn-budget fix) as
    soon as `covered` already names every entry of `chain_code_sha_set`:
    further records cannot add anything once the ceiling is reached, so
    every remaining `resolve_range_shas` call (a `git rev-list` spawn in
    production) is skipped rather than paid for no additional information —
    this is the dominant lever on a chain whose code-obligation set is small
    relative to its on-disk trail-record count.

    `vouched_shas` is threaded straight through to `_record_membership_shas`
    — see that function's own docstring for its shape and fail-safe
    posture.

    `chain_planning_shas`, optional, is threaded straight through to
    `_record_membership_shas` as `chain_planning_sha_set` — the PLANNING-
    classified subset of `chain_code_shas` a `scope_kind: "plan"` record's
    contribution is capped at. `None` (the default) means every existing
    caller that omits it sees byte-identical behavior to before this
    parameter existed: a "plan" record credits nothing."""
    chain_dag_sha_set = {str(s).lower() for s in chain_dag_shas}
    chain_code_sha_set = {str(s).lower() for s in chain_code_shas}
    chain_planning_sha_set = (
        {str(s).lower() for s in chain_planning_shas} if chain_planning_shas is not None else None
    )
    covered: set[str] = set()
    for record in trail_records:
        if chain_code_sha_set and covered >= chain_code_sha_set:
            break
        raw = record.get("verdict")
        if raw is None:
            continue
        verdict = str(raw).strip().lower()
        if not verdict or verdict in _NON_DISCHARGING_VERDICTS:
            continue
        membership = _record_membership_shas(
            record, resolve_range_shas, chain_dag_sha_set, chain_code_sha_set,
            narrow_foreign_shas=narrow_foreign_shas,
            vouched_shas=vouched_shas,
            chain_planning_sha_set=chain_planning_sha_set,
        )
        if membership is None:
            continue
        covered.update(membership)
    return covered


def chain_partition_uncovered_shas(
    trail_records: Iterable[Mapping[str, Any]],
    chain_code_shas: Optional[Iterable[str]],
    chain_dag_shas: Optional[Iterable[str]],
    resolve_range_shas: Optional[Callable[[str], Any]],
    narrow_foreign_shas: Optional[Callable[[str, Optional[str]], Any]] = None,
    vouched_shas: Optional[Callable[[Optional[str]], Any]] = None,
    chain_planning_shas: Optional[Iterable[str]] = None,
) -> list[str]:
    """The union's diagnostic sibling: which `chain_code_shas` entries no
    discharging trail record's resolved range names, in input order.
    Deterministic given its inputs, no module-level state mutation of its
    own — but NOT side-effect-free: it drives the injected
    `resolve_range_shas` callable, which in production shells out to `git
    rev-list` per record. Reuses `_collect_discharging_range_shas` for the
    exact same
    membership-scoped trust filter `chain_partition_verdict_discharged`
    applies, so this function's output and that predicate's verdict can
    never diverge on what counts as covered. `chain_dag_shas` (the chain's
    UNFILTERED DAG sha set) gates which records are even in-chain
    membership candidates; `chain_code_shas` (the filtered code-obligation
    set) is both the coverage credit ceiling and the set this function's
    return value is drawn from — see `_record_membership_shas`'s docstring
    for why the two must differ. The caller
    (`wsc-coverage-gate-runner.py::cmd_brightline_gate`) prints this list as
    the refusal's "what would satisfy it" diagnostic.

    `vouched_shas` is threaded straight through to `_collect_discharging_
    range_shas` / `_record_membership_shas` — see the latter's docstring for
    its shape and fail-safe posture.

    `chain_planning_shas`, optional, is threaded straight through to
    `_collect_discharging_range_shas` — the PLANNING-classified subset of
    `chain_code_shas` a `scope_kind: "plan"` record may discharge. `None`
    (the default) sees byte-identical behavior to before this parameter
    existed.

    Fail-safe (review-integrator finding N1): `chain_code_shas`,
    `chain_dag_shas`, or `resolve_range_shas` being `None` returns `[]`
    rather than raising `TypeError` on `list(None)` — symmetric with
    `chain_partition_verdict_discharged`'s own `None`-guard rather than
    trusting every caller to pre-check before calling this sibling."""
    if chain_code_shas is None or chain_dag_shas is None or resolve_range_shas is None:
        return []
    chain_code_shas = list(chain_code_shas)
    covered = _collect_discharging_range_shas(
        trail_records, resolve_range_shas, chain_dag_shas, chain_code_shas,
        narrow_foreign_shas=narrow_foreign_shas,
        vouched_shas=vouched_shas,
        chain_planning_shas=chain_planning_shas,
    )
    return [sha for sha in chain_code_shas if str(sha).lower() not in covered]


#: Reporting-layer label for a discharging record whose `execution_basis`
#: key is absent — 2026-08-11 (`docs/plans/2026-08-11-review-trail-carries-
#: execution-basis.md`, C4). Absence means unknown (C1's own contract: the
#: stored enum stays two-valued, `executed` | `read-only`; this is NOT a
#: third stored value, only a bucket label this reporting function assigns
#: when the key is missing). Every one of the 1804 pre-C1 records lacks the
#: key, so this bucket dominates the output today — expected, not a defect
#: to design around.
EXECUTION_BASIS_NOT_RECORDED = "not recorded"

#: The two stored `execution_basis` values C1 writes — reused here rather
#: than re-declared so a future rename of C1's enum has one string to
#: catch, not two.
_KNOWN_EXECUTION_BASES = frozenset({"executed", "read-only"})


def chain_partition_execution_basis_report(
    trail_records: Iterable[Mapping[str, Any]],
    chain_code_shas: Optional[Iterable[str]],
    chain_dag_shas: Optional[Iterable[str]],
    resolve_range_shas: Optional[Callable[[str], Any]],
    narrow_foreign_shas: Optional[Callable[[str, Optional[str]], Any]] = None,
    vouched_shas: Optional[Callable[[Optional[str]], Any]] = None,
    chain_planning_shas: Optional[Iterable[str]] = None,
) -> dict[str, int]:
    """Read-only reporting companion to `chain_partition_verdict_discharged`
    (C4, 2026-08-11, `docs/plans/2026-08-11-review-trail-carries-execution-
    basis.md`). Reports, per DISCHARGING trail record (same trust filter
    `_collect_discharging_range_shas` applies — non-`pending`/non-`waived`
    verdict AND within-chain membership, i.e. a non-empty contribution from
    `_record_membership_shas`), a count of how many discharging records
    carried each `execution_basis` value, plus how many carried none at all
    (`EXECUTION_BASIS_NOT_RECORDED`).

    THIS FUNCTION NEVER REFUSES ANYTHING. It is a VOICE, not a veto — it
    does not gate, does not return a bool, and its output feeds only
    close-ceremony narration (e.g. "this chain discharged on 4 records, 3
    of them read-only"). `chain_partition_verdict_discharged` itself is
    UNTOUCHED by this chunk and keeps returning exactly what it returns
    today; nothing here can change a discharge outcome for any input.

    Every parameter mirrors `chain_partition_verdict_discharged`'s own
    signature exactly, with the same fail-safe posture: `chain_code_shas`,
    `chain_dag_shas`, or `resolve_range_shas` being `None` returns `{}`
    (an empty report — no discharging records can be identified without
    them) rather than raising.

    Membership-only, not coverage-completeness: unlike
    `chain_partition_verdict_discharged`, this function does not require
    `chain_code_shas` to be fully covered — it reports on EVERY record
    that passes the discharge trust filter and contributes at least one
    sha, regardless of whether the union across all such records happens
    to cover every chain code sha. A caller wanting "and did the chain
    actually discharge" still calls `chain_partition_verdict_discharged`
    separately; this function answers a different question ("of the
    records that would count as evidence, what execution basis backed
    them") and is meaningful to report even when the chain has NOT fully
    discharged.
    """
    if chain_code_shas is None or chain_dag_shas is None or resolve_range_shas is None:
        return {}
    chain_code_sha_set = {str(s).lower() for s in chain_code_shas}
    chain_dag_sha_set = {str(s).lower() for s in chain_dag_shas}
    chain_planning_sha_set = (
        {str(s).lower() for s in chain_planning_shas} if chain_planning_shas is not None else None
    )
    counts: dict[str, int] = {
        "executed": 0,
        "read-only": 0,
        EXECUTION_BASIS_NOT_RECORDED: 0,
    }
    for record in trail_records:
        raw_verdict = record.get("verdict")
        if raw_verdict is None:
            continue
        verdict = str(raw_verdict).strip().lower()
        if not verdict or verdict in _NON_DISCHARGING_VERDICTS:
            continue
        membership = _record_membership_shas(
            record, resolve_range_shas, chain_dag_sha_set, chain_code_sha_set,
            narrow_foreign_shas=narrow_foreign_shas,
            vouched_shas=vouched_shas,
            chain_planning_sha_set=chain_planning_sha_set,
        )
        if not membership:
            continue
        basis = record.get("execution_basis")
        if basis in _KNOWN_EXECUTION_BASES:
            counts[basis] += 1
        else:
            counts[EXECUTION_BASIS_NOT_RECORDED] += 1
    return counts


def chain_partition_verdict_discharged(
    trail_records: Iterable[Mapping[str, Any]],
    chain_code_shas: Optional[Iterable[str]],
    chain_dag_shas: Optional[Iterable[str]],
    resolve_range_shas: Optional[Callable[[str], Any]],
    narrow_foreign_shas: Optional[Callable[[str, Optional[str]], Any]] = None,
    vouched_shas: Optional[Callable[[Optional[str]], Any]] = None,
    chain_planning_shas: Optional[Iterable[str]] = None,
) -> bool:
    """True iff `chain_code_shas` is a non-empty set AND every sha in it is
    named by the resolved, within-chain-membership range of at least one
    discharging trail record (`chain_partition_uncovered_shas` returns
    `[]`) — a real (non-`pending`, non-`waived`) reviewer verdict whose
    range shares at least one commit with `chain_dag_shas` (membership,
    intersection not subset — see `_record_membership_shas`'s docstring)
    and whose `raw & chain_code_shas` intersection, unioned across every
    such record, names every one of this chain's code shas.

    `chain_code_shas` and `chain_dag_shas` answer different questions and
    must not be conflated (see the "membership-vs-coverage split" note
    above this section, and `_record_membership_shas`'s own docstring):
    `chain_dag_shas` (unfiltered — every chain DAG commit, bookkeeping and
    handoff-authoring included) gates whether a record's raw range is even
    IN this chain at all; `chain_code_shas` (filtered — code-obligation
    commits only) is both the coverage-credit ceiling and the set this
    predicate demands full coverage of. Passing the same set for both
    reintroduces the exact bug the membership-vs-coverage split fixes: an
    honest whole-chain range spanning one ceremony commit would fail a
    same-set test and be rejected wholesale.

    `narrow_foreign_shas`, optional, is `(sha_range, session_id) ->
    iterable-of-shas` — the same session/scope narrowing
    `coverage.build_reviewed_set` applies to session/chain-scoped records
    (`_FOREIGN_STRIPPED_SCOPES`), injected here as defense-in-depth on top
    of the DAG-membership intersection test (see the 2026-08-06
    subset-to-intersection correction note above for why this predicate
    does not rely on the intersection test alone). `None` (the default)
    skips this narrowing entirely — every existing caller that omits it
    sees byte-identical behavior to before this parameter existed.

    `vouched_shas`, optional, is threaded straight through to
    `chain_partition_uncovered_shas` / `_record_membership_shas` — see the
    latter's docstring for its shape and fail-safe posture (a sha carrying a
    matching gate-minted chain-ancestry waiver is not narrowed out of a
    discharging record's contribution).

    `chain_planning_shas`, optional, is threaded straight through to
    `chain_partition_uncovered_shas` — the PLANNING-classified subset of
    `chain_code_shas` a `scope_kind: "plan"` record may discharge (2026-08-07
    correction, see `_NON_CODE_SCOPE_KINDS`'s docstring). `None` (the
    default) sees byte-identical behavior to before this parameter existed.

    History (2026-08-06, PM directive "new session can make the guard less
    brutal", cross-repo/inbox review of the
    `2026-08-06-eliminate-claude-klabauter-s-non-test-subprocess-spawn-population`
    chain): this predicate originally shipped as TWO legs — a "legacy
    tip-reaching leg (a)" (one record whose range-tip reaches/postdates the
    chain tip) plus this union-coverage "leg (b)". Both legs shared the
    SAME scoping condition, `tip == chain_tip_sha or is_ancestor(chain_tip_
    sha, tip)`, which live re-verification the same day proved is not a
    chain-scoping check at all on this fleet's ONE SHARED
    `work/{machine}/{date}` branch: a concurrent peer session's record,
    written later on the shared branch, satisfies "postdates the chain tip"
    regardless of whether it reviewed one commit of this chain. Against the
    live chain named above (tip `72eee33c6`, 15 chain code shas), EVERY
    record that discharged leg (a) belonged to two unrelated peer sessions;
    ZERO belonged to this chain's own 17 review-trail records. The same
    condition was also unsatisfiable for the immediately preceding session
    purely because no peer had yet written a later record — same chain,
    same evidence, opposite verdict, decided by peer timing, not by what
    was actually reviewed. Because leg (b) was built to reuse leg (a)'s
    exact scoping condition, leg (b) was ALSO, independently, a
    mathematical subset of leg (a) at that time — vacuous, it could never
    discharge anything leg (a) would not already discharge on its own.

    Both legs are replaced here by ONE within-chain-membership check
    (`chain_partition_uncovered_shas` / `_collect_discharging_range_shas` /
    `_record_membership_shas`): a record contributes iff its resolved
    range's sha set shares AT LEAST ONE commit with `chain_dag_shas`
    (INTERSECTION, not subset — see the 2026-08-06 subset-to-intersection
    correction note above `chain_partition_verdict_discharged`'s own
    section header for why a subset requirement was still too strict on
    this fleet's interleaving-peer-commits norm), and its actual coverage
    contribution is `raw & chain_code_shas` — never more than the
    code-bearing commits it actually names. The former leg (a) is
    REDUNDANT under this scoping, not merely renamed away: a single record
    whose range names every entry in `chain_code_shas` already satisfies
    this function on its own (the union-of-one case), so there is no
    discharge leg (a) could contribute that this single leg does not
    already contribute. Keeping a second, textually distinct leg here
    would only reproduce the same collapse-into-redundancy this history
    section documents — one honest leg, not two where one is decorative.

    Fail-safe toward refusal, unconditionally: `chain_code_shas`,
    `chain_dag_shas`, or `resolve_range_shas` being `None`, or
    `chain_code_shas` resolving to an empty collection (e.g. the chain's own
    code-obligation set could not be derived), never discharges — this
    function has no fallback path that
    manufactures a verdict from partial or unresolvable evidence. A record
    `_record_membership_shas` cannot resolve (missing/unparseable
    `sha_range`, a resolver that raises or returns nothing) or that
    straddles chain and non-chain commits contributes nothing, never a
    partial credit.

    Deliberately NOT a reviewer-count check: `COORDINATOR_BRIGHTLINE_
    REVIEWER_COUNT` (consulted elsewhere in the tier=B/none brightline path,
    `wsc-coverage-gate-runner.py::cmd_brightline_gate`) is recorded EM
    intent, not evidence a review ran — that function's own existing
    cross-check already says it aloud: "discharge is disk findings, not the
    recorded count alone." This predicate reads the review-trail's own
    verdict field, the same disk-truth surface, not a count.

    Deliberately permissive on verdict spelling: any record whose `verdict`
    key is present and, lower-cased, is not literally `"pending"` or
    `"waived"` counts as discharging (e.g. `"ok"`, `"warn"`, `"blocked"` all
    discharge — a `blocked` verdict means a reviewer ran and found a
    problem, which is still evidence the mandated review scale was
    exercised, distinct from nobody having reviewed at all). This function
    does NOT reject an out-of-set verdict string the way the cockpit-emit
    quarantine filter (`ops/emit/sections/_shared.py::_validate_review_
    trail_file`) does — that filter's stricter, `ok|warn|blocked|waived`-only
    acceptance set exists for a different purpose (cockpit-snapshot record
    hygiene) and would silently quarantine (and thus hide) a genuinely
    unusual-but-real verdict from this refusal's evidence, the opposite of
    this predicate's fail-safe-toward-refusal posture.

    A record missing a `verdict` key, or with an empty/whitespace-only
    verdict, is skipped (treated as non-discharging, never as an error) —
    consistent with every other pure predicate in this module tolerating a
    partially-shaped record rather than raising over it.
    """
    if chain_code_shas is None or chain_dag_shas is None or resolve_range_shas is None:
        return False
    chain_code_shas = list(chain_code_shas)
    if not chain_code_shas:
        return False
    uncovered = chain_partition_uncovered_shas(
        list(trail_records), chain_code_shas, chain_dag_shas, resolve_range_shas,
        narrow_foreign_shas=narrow_foreign_shas,
        vouched_shas=vouched_shas,
        chain_planning_shas=chain_planning_shas,
    )
    return not uncovered


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

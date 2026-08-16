"""
coordinator_core.workstream_complete.judgments — the 29 preserved
judgment_point builders for the `workstream-complete-assemble` computed
engine.

Purpose: the D-3/census conversion of `coordinator/skills/workstream-
complete/SKILL.md` (DoE-claude) extracts every genuinely mechanical branch
into `directives_*.py` submodules and leaves a narrow residue that the
source text itself names as non-computable — an open-ended qualitative
call, an authorial-prose act, or a classification the SKILL delegates to a
model (EM or sub-dispatch) rather than a fixed predicate. This module is
the SHAPE layer for that residue: one `build_<id>_judgment_point() -> dict`
per surviving judgment_point id, each a pure, zero-argument constructor
call against `coordinator_core.contract.decision_object.judgment`. Nothing
here computes live session state, reads disk, or decides whether a point
actually fires for a given run — that is `__init__.py`'s (C3's) assembly
job, exactly as `build_session_shape_judgment_point` in this package
already does for the one pre-existing judgment point this module does NOT
duplicate (see Negative-spec). `build_coverage_judgment_point` and the
`jp-coverage-verdict` judgment point it built were removed by K-001
(`state/kill-ledger.md`) along with `coverage.gate`'s review-coverage
verdict and every consumer — this module's residue posture is unaffected.

29 distinct ids (collapsing every repeated in-source trigger of the same
id into one builder — e.g. `completion-entry-prose` recurs across Steps
2.6/2.6.8/2.6b in the source SKILL but is ONE judgment_point here, per the
governing plan's explicit instruction). D-3's 8 review-side ids
(`review-partition-strategy`, `reviewer-count-on-oracle-disagreement`,
`shared-schema-touch-check`, `governing-spec-identification`,
`finding-tradeoff-escalation-check`, `shallow-row3-waive-check`,
`review-dispatch-vehicle-choice`, `quota-retry-vs-escalate`) plus the
Step-2.95/2.96 pair (`cross-cutting-check`, `inline-waiver-recognition`,
folded in per plan F1) plus 19 others spanning lessons, plan reconciliation,
completion, memo lifecycle, scratch disposition, predecessor distill-fate,
session hygiene, and commit/tail — see `JUDGMENT_POINT_BUILDERS` at the
bottom for the full roster in one place.

Tier discipline (`coordinator/docs/wiki/computed-skills.md` § The
three-tier model, DoE-claude): every builder below carries a
`recommendation` (tier 2, `build_judgment_point`) UNLESS its evidence is
content this engine did not itself compute — another session's memo
prose, another session's git activity, or a classification explicitly
delegated to a sub-dispatch this module never sees the output of. Those
three (`completion-nature-classification`, `do-now-memo-violation-check`,
`concurrent-peer-attribution`) are tier 3: built via `build_untrusted_gate_
judgment_point`, which has no `recommendation` parameter for a caller to
fill — a recommendation computed over evidence this engine cannot verify
is itself the attacker/peer-influenceable surface the wiki's
recommendation-forbidden class exists to close. `memo-resolution-
attribution` moved from tier 3 to tier 2 on 2026-07-30 (see that builder's
own docstring) once its evidence became this engine's own computation over
fields it itself writes, rather than another session's memo prose — it is
no longer counted among the untrusted-gate four. Every other 26 carry a
recommendation.

Round-trip classification (`§ Round-trip classification`, same wiki):
independently re-derived here, NOT assumed from `pickup_assemble`'s
empty-round-trip finding (per the governing plan's explicit instruction —
that finding was scoped to pickup's own inventory). All 29 points are
`round_trip="terminal"` on the SHAPE dimension: every disposition's
`resolves` list is a fixed, fully-enumerable directive-id set known at
authoring time, so no disposition here requires a second `brief()` call to
compute a *new* directive shape. One point is freshness-sensitive on the
orthogonal dimension: `concurrent-peer-attribution`'s evidence (another
session's recent commits / unrecognized paths on a shared branch) can go
stale between brief-compute time and dispatch time exactly as pickup's own
positive-liveness predicate does, so it alone carries
`revalidate_at_dispatch=True`; every other point defaults to `False`.

Negative-spec:
  - Does NOT define `jp-session-shape` — that is the pre-existing,
    already-green judgment point this same package's `__init__.py` builds
    (`build_session_shape_judgment_point`). Rebuilding it here would be
    exactly the context-less re-derivation the plan's Anti-scope section
    warns against. `jp-coverage-verdict`/`build_coverage_judgment_point`
    were removed by K-001 and no longer exist anywhere in this package —
    not a case this module needs to avoid duplicating any more.
  - Does NOT decide whether a given builder's judgment_point should appear
    in a particular `brief()` call. Every function here is unconditional —
    conditional inclusion (e.g. "only offer `commit-significance-filter`
    when this session has commits at all") is an assembly-time (`__init__.py`
    / C3) concern, not a shape concern.
  - Does NOT read disk, call `subprocess`, or touch `git` — purely
    declarative dict construction, same posture as this package's other
    judgment-point builder.
  - Does NOT import or extend `coordinator_core.contract.apply_base` — not
    this module's concern at any layer (D-1's separate baton).

Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md,
chunk C2f. Census: DoE-claude
state/plan-sidecars/2026-07-26-workstream-complete-computed-frontage.census-steps.md
(rows classified JUDGMENT for Steps 1, 1.2, 2, 2.4, 2.4b, 2.6, 2.6.7,
2.6.8, 2.6b, 2.65, 2.67, 2.7, 2.8, 2.9, 2.95, 2.96, 3.0, 3, 4).
"""

from __future__ import annotations

from typing import Any, Callable

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
    build_untrusted_gate_judgment_point,
)


# ---------------------------------------------------------------------------
# Lessons / governing-plan reconciliation (census Steps 1, 1.2, 2, 2.4, 2.4b)
# ---------------------------------------------------------------------------


def build_lesson_worth_capturing_judgment_point(
    capture_resolves_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Step 1's qualifying scan: does anything from this session warrant a
    lesson? Explicitly qualitative — "will this save time in ~4 weeks?"
    has no fixed predicate.

    `capture_resolves_ids` MUST be the caller's `directives_lessons_plan.
    lesson_capture_resolves_ids(decisions)` — the per-lesson SUFFIXED ids
    (`d-add-lesson-1`, ...) the directive builder actually emits. `apply`'s
    gate matches a `resolves` entry against a directive id exactly, never by
    prefix, so the unsuffixed base this once passed named nothing: the gate
    stayed shut, no directive reported an error, `apply` returned success,
    and the captured lesson was silently never written to disk."""
    return build_judgment_point(
        {
            "disposition": "skip",
            "rationale": (
                "absent a specific recurring failure or non-obvious fix demonstrated "
                "this session, defaulting to skip avoids lesson-corpus noise -- capture "
                "is the exception a demonstrated pattern earns, not the default"
            ),
        },
        id="lesson-worth-capturing",
        question="Does this session's work contain an event/pattern worth capturing as a lesson?",
        dispositions=[
            build_disposition("capture", resolves=list(capture_resolves_ids or [])),
            build_disposition("skip", resolves=[]),
        ],
        evidence="session transcript + this session's touched-paths diff",
        reason=(
            "capture-worthiness ('will this save time in ~4 weeks?') is an open-ended "
            "qualitative call over session memory, not computable from a fixed predicate"
        ),
        revalidate_at_dispatch=False,
        # `resolves` is resolver-populated: an empty list here means the
        # `decisions` slice is not filled in yet, not that this point gates
        # nothing. Exempts it from the `_emit` unclassified-scan; it is not a
        # `reportable` classification and must never be used as one.
        resolves_computed=True,
    )


def build_lesson_scope_classification_judgment_point() -> dict[str, Any]:
    """Step 1.2: universal-vs-project-specific + `--change-kind` selection.
    Both require reading the lesson's own substance."""
    return build_judgment_point(
        {
            "disposition": "project-specific",
            "rationale": (
                "most lessons are grounded in this repo's concrete substrate; universal "
                "scope is the narrower claim and should be earned by evidence the lesson "
                "generalizes, not assumed by default"
            ),
        },
        id="lesson-scope-classification",
        question=(
            "Does this lesson apply to any project type (tag [universal]), or is it "
            "project-specific -- and which --change-kind enum value applies?"
        ),
        dispositions=[
            build_disposition("universal", resolves=[]),
            build_disposition("project-specific", resolves=[]),
        ],
        evidence="the lesson's own drafted content",
        reason=(
            "the universal/project-specific call and the change-kind selection both "
            "require reading the lesson's substance, not a disk-computable fact"
        ),
        revalidate_at_dispatch=False,
        # (review-integration slice B) action-class: the question asks
        # "and which --change-kind enum value applies?" -- the EM must
        # produce a value it then supplies to `lesson-add`, an input the EM
        # generates rather than a label the engine records.
        reportable=False,
    )


def build_plan_doc_content_update_judgment_point() -> dict[str, Any]:
    """Step 2: which plan sections read as stale and need a review/outcomes
    update. "Clearly stale" and the outcomes summary are both authorial."""
    return build_judgment_point(
        {
            "disposition": "update-now",
            "rationale": (
                "a plan doc left unreconciled against shipped reality actively misleads "
                "the next reader; updating is the safer default over leaving it silent"
            ),
        },
        id="plan-doc-content-update",
        question="Which governing-plan sections are now stale and need a review/outcomes update?",
        dispositions=[
            build_disposition("update-now", resolves=[]),
            build_disposition("no-stale-content", resolves=[]),
        ],
        evidence="the governing plan doc's own content vs this session's diff",
        reason=(
            "deciding what counts as 'clearly stale' and summarizing decisions/outcomes "
            "in prose is authorial, not a fixed predicate"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo, same plan) action-class: `update-now` means the EM
        # literally edits the governing plan doc on disk -- a concrete write,
        # no directive gates it, so the answer must keep asking rather than
        # be silently narrated away.
        reportable=False,
    )


def build_plan_vs_reality_reconcile_judgment_point() -> dict[str, Any]:
    """Step 2.4's ALLOWLIST reconcile (Decisions Made / API Contracts / AC
    table) -- also re-triggered by Step 2.4's soft-ordering note if Step
    2.9 surfaces additional drift, per the same judgment_point."""
    return build_judgment_point(
        {
            "disposition": "annotate-divergence",
            "rationale": (
                "checking for a SHIPPED: X (was: Y) divergence costs little; the failure "
                "mode of silently assuming the plan already matches reality is worse than "
                "an unnecessary reconcile pass that finds nothing"
            ),
        },
        id="plan-vs-reality-reconcile",
        question=(
            "Where did shipped reality diverge from the plan's Decisions Made / API "
            "Contracts / AC-table forecast, and how should the SHIPPED annotation read?"
        ),
        dispositions=[
            build_disposition("annotate-divergence", resolves=[]),
            build_disposition("matches-forecast", resolves=[]),
        ],
        evidence="plan's Decisions Made/API Contracts/AC sections vs the actual diff",
        reason=(
            "requires comparing what was actually implemented against the plan's "
            "forecast and composing the delta in prose -- genuine authorial/analytical "
            "judgment"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: `annotate-divergence` means the EM writes a
        # SHIPPED annotation into the plan doc -- a concrete disk edit with
        # no directive behind it.
        reportable=False,
    )


def build_enablement_vs_opportunistic_deferral_judgment_point() -> dict[str, Any]:
    """Step 2.4b: classify each improvement-queue entry this session
    created as opportunistic-improvement vs load-bearing-feature-
    completion."""
    return build_judgment_point(
        {
            "disposition": "opportunistic-defer",
            "rationale": (
                "most queued improvements are genuinely opportunistic; promotion to "
                "load-bearing should require a specific demonstrated end-to-end gap, "
                "not be the default read of an improvement-queue entry"
            ),
        },
        id="enablement-vs-opportunistic-deferral",
        question=(
            "Is this improvement-queue entry opportunistic, or does it gate a "
            "load-bearing feature this session claims is complete end-to-end?"
        ),
        dispositions=[
            build_disposition("opportunistic-defer", resolves=[]),
            build_disposition("load-bearing-promote", resolves=[]),
        ],
        evidence=(
            "this session's improvement-queue entries + what the completed feature "
            "claims to deliver end-to-end"
        ),
        reason=(
            "the load-bearing judgment ('does the feature do anything end-to-end "
            "without the queued item?') is explicitly qualitative -- the source text's "
            "own tells are illustrative, not diagnostic"
        ),
        revalidate_at_dispatch=False,
        # (review-integration slice B) action-class: `load-bearing-promote`
        # obligates an action, not a relabel -- it means this queued item
        # gates the feature this session is calling complete end-to-end, and
        # that obligates either shipping the enabler or withdrawing the
        # completeness claim. Demoted to narration, the EM is never asked and
        # can close a session with an inert feature reading as done.
        reportable=False,
    )


# ---------------------------------------------------------------------------
# Completion-entry residue (census Steps 2.6, 2.6.7, 2.6.8, 2.6b)
# ---------------------------------------------------------------------------


def build_completion_nature_classification_judgment_point() -> dict[str, Any]:
    """Step 2.6 Action (b): when COMPLETION_NATURE is unset, classification
    into {roadmap, bugfix, tech-debt, infra} is explicitly named "a model
    step the CLI does NOT own" -- delegated to a Sonnet sub-dispatch this
    assembler never sees the reasoning behind. Tier 3: a recommendation
    computed over evidence this module doesn't itself hold would pre-empt
    the sub-dispatch's own read, not merely be low-confidence."""
    return build_untrusted_gate_judgment_point(
        id="completion-nature-classification",
        question=(
            "Which COMPLETION_NATURE bucket applies -- roadmap, bugfix, tech-debt, "
            "or infra?"
        ),
        dispositions=[
            build_disposition("roadmap", resolves=[]),
            build_disposition("bugfix", resolves=[]),
            build_disposition("tech-debt", resolves=[]),
            build_disposition("infra", resolves=[]),
        ],
        evidence=(
            "touched paths, commit messages, workstream kind, chain slug -- via the "
            "dedicated Sonnet sub-dispatch classification"
        ),
        reason=(
            "explicitly named 'a model step the CLI does NOT own' -- classification is "
            "delegated to a sub-dispatch reading commit substance this assembler doesn't "
            "itself compute, so a recommendation here would pre-empt evidence this module "
            "never sees directly"
        ),
        revalidate_at_dispatch=False,
    )


def build_completion_entry_prose_judgment_point() -> dict[str, Any]:
    """The one point behind three re-triggers in the source SKILL: Step
    2.6's TITLE + body, Step 2.6.8's one-sentence append, and Step 2.6b's
    one-liner (including its fail-open manual-fallback path) -- all the
    same authorial summarization act, collapsed into one judgment_point
    per the governing plan's instruction."""
    return build_judgment_point(
        {
            "disposition": "drafted",
            "rationale": (
                "the commit/observation content itself is sufficient evidence to draft "
                "a conforming completion-entry title, body, or one-liner; escalate only "
                "if the work genuinely resists a bounded qualitative summary"
            ),
        },
        id="completion-entry-prose",
        question=(
            "What should this completion entry's title, body, or one-liner append say "
            "-- respecting ordering and banned-section rules?"
        ),
        dispositions=[
            build_disposition("drafted", resolves=["d-complete-entry"]),
            build_disposition("needs-second-pass", resolves=[]),
        ],
        evidence="this session's shipped commits and/or the sidecar's Observations content",
        reason="authorial summarization; no computable substitute",
        revalidate_at_dispatch=False,
    )


def build_commit_significance_filter_judgment_point() -> dict[str, Any]:
    """Step 2.6.7, self-declared "Judgment filter" in the source SKILL:
    group related commits, skip trivial ones, skip silently if nothing
    substantive shipped."""
    return build_judgment_point(
        {
            "disposition": "significant",
            "rationale": (
                "a commit only reaches this filter after already clearing the "
                "coverage/review gates; defaulting to record rather than silently drop "
                "preserves the audit trail"
            ),
        },
        id="commit-significance-filter",
        question=(
            "Do this session's commits warrant grouping into one completion entry, or "
            "are they trivial / nothing-substantive?"
        ),
        dispositions=[
            build_disposition("significant", resolves=["d-complete-entry"]),
            build_disposition("trivial-skip", resolves=[]),
        ],
        evidence="this session's commit log + diff substance",
        reason='step is explicitly named "Judgment filter" in the SKILL.md itself -- self-declaring',
        revalidate_at_dispatch=False,
    )


# ---------------------------------------------------------------------------
# Memo lifecycle / scratch disposition (census Steps 2.65, 2.67, 2.7)
# ---------------------------------------------------------------------------


def build_memo_resolution_attribution_judgment_point(
    resolved_resolves_ids: list[str] | None = None,
    attribution_signals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Step 2.65: which open memos did this session's commits resolve.

    Source text: "non-automatable... no reliable programmatic signal
    connects commits to memo resolution." That premise is STALE as of the
    2026-07-30 doe-claude-em cross-repo memo (`cross-repo/archive/2026-07-30-
    doe-claude-em-wsc-review-trail-passthrough-and-memo-attribution.md`):
    three signals this engine itself writes DO connect commits to memo
    resolution — `picked_up_by == sid`, `realized_by` matching a commit SHA
    in this session's range, and an inbox->archive `git mv` landing as an
    `R100` rename in range (`directives_memo_lifecycle.
    compute_memo_resolution_attribution` computes the union). This is
    therefore now `build_judgment_point` (tier 2, a `recommendation` is
    permitted), NOT `build_untrusted_gate_judgment_point` — the untrusted-
    gate classification rested on the evidence being another session's memo
    prose; it is now this engine's own computation over fields this engine
    itself wrote, which is exactly the trusted-caller case
    (`coordinator_core.contract.decision_object.judgment`'s own docstring:
    a trusted caller "is allowed to say what it would do"). This narrows the
    question, it does NOT remove it: a memo resolved without any of the
    three signals firing (no archive move, no stamp) is exactly the
    residual the EM must still be asked to confirm, so the point still
    fires and still asks — see the not-resolved branch below.

    `resolved_resolves_ids` MUST be the caller's `directives_memo_lifecycle.
    memo_flip_resolves_ids(dispositions)` — the basename-suffixed
    `d-flip-memo-status:<basename>` ids. See the lesson point above for why
    the unsuffixed base can never open the gate.

    `attribution_signals` MUST be the caller's `directives_memo_lifecycle.
    compute_memo_resolution_attribution(repo_root, sid)` — one dict per
    memo with at least one signal firing, `{"path", "basename", "signals"}`.
    An empty (or `None`) list recommends `not-resolved`; a non-empty list
    recommends `resolved`, with `evidence` naming each memo and which
    signal(s) fired for it.
    """
    signals = list(attribution_signals or [])
    if signals:
        disposition = "resolved"
        rationale = (
            f"{len(signals)} archived memo(s) carry at least one of this session's own "
            "picked_up_by/realized_by/archive-rename signals -- see evidence for the "
            "per-memo breakdown"
        )
        evidence = "memo-resolution signals (picked_up_by/realized_by/archive_rename): " + "; ".join(
            f"{s['basename']}: {', '.join(s['signals'])}" for s in signals
        )
    else:
        disposition = "not-resolved"
        rationale = (
            "no picked_up_by/realized_by/archive-rename signal fired for any archived "
            "memo in this session's commit range -- nothing programmatically ties this "
            "session to a memo resolution"
        )
        evidence = (
            "memo-resolution signals (picked_up_by/realized_by/archive_rename): none fired"
        )
    return build_judgment_point(
        {"disposition": disposition, "rationale": rationale},
        id="memo-resolution-attribution",
        question="Which open memos did this session's commits actually resolve?",
        dispositions=[
            build_disposition("resolved", resolves=list(resolved_resolves_ids or [])),
            build_disposition("not-resolved", resolves=[]),
        ],
        evidence=evidence,
        reason=(
            "computed from three signals this engine itself writes -- picked_up_by, "
            "realized_by, and inbox->archive R100 renames -- over this session's own "
            "commit range; still a judgment point (not auto-resolved) because a memo "
            "resolved without any of the three signals firing is a genuine residual the "
            "EM must confirm"
        ),
        revalidate_at_dispatch=False,
        # `resolves` is resolver-populated: an empty list here means the
        # `decisions` slice is not filled in yet, not that this point gates
        # nothing. Exempts it from the `_emit` unclassified-scan; it is not a
        # `reportable` classification and must never be used as one.
        resolves_computed=True,
    )


def build_do_now_memo_violation_check_judgment_point() -> dict[str, Any]:
    """Step 2.65: scan for an `ask` memo accepted-in-word but not landed
    (evasive phrasing like "will land before X"). Tier 3: the evidence is
    another party's decision-note prose."""
    return build_untrusted_gate_judgment_point(
        id="do-now-memo-violation-check",
        question=(
            "Does an accepted-in-word memo have unlanded work behind evasive phrasing "
            "('will land before X')?"
        ),
        dispositions=[
            build_disposition("violation-found", resolves=[]),
            build_disposition("no-violation", resolves=[]),
        ],
        evidence="the memo's own decision-note prose",
        reason=(
            "requires reading a memo's decision-note prose for evasive phrasing and "
            "then doing substantive work -- the evidence is another party's prose, not "
            "this engine's own computation"
        ),
        revalidate_at_dispatch=False,
    )


def build_scratch_disposition_per_file_judgment_point() -> dict[str, Any]:
    """Step 2.67: for each session-authored scratch file, `git rm` or
    write a one-line justify-keep reason. Source text: "default is
    delete", with example (non-exhaustive) reasons for keeping."""
    return build_judgment_point(
        {
            "disposition": "delete",
            "rationale": (
                "per the source SKILL's own stated default ('default is delete'), "
                "keeping is the justified exception, not the baseline"
            ),
        },
        id="scratch-disposition-per-file",
        question="Delete this session-authored scratch file, or keep it with a justify-keep reason?",
        dispositions=[
            build_disposition("delete", resolves=[]),
            build_disposition("keep-with-justification", resolves=[]),
        ],
        evidence="the file's own content + whether it's referenced by surviving session artifacts",
        reason=(
            "deciding whether a specific scratch file is still useful is not reducible "
            "to a predicate -- the source text gives example reasons, not exhaustive rules"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo, premise-finding sidecar channel 3) action-class: the
        # answer decides whether a file is `git rm`'d -- no directive, no
        # gate, but the EM must still DO something on the answer. Named
        # explicitly by the plan's premise-finding sidecar as evidence the
        # channel-3 gap is real.
        reportable=False,
    )


def build_predecessor_distill_fate_judgment_point() -> dict[str, Any]:
    """Step 2.7: backfill a predecessor's missing `distill_fate:` based on
    how its open loop actually resolved. Mirrors pickup's own
    ratification-escalation shape -- terminal, not round-trip, per that
    precedent's own reasoning (each disposition's CLI args are enumerable
    in advance)."""
    return build_judgment_point(
        {
            "disposition": "commitment",
            "rationale": (
                "most open-loop predecessors resolve into durable follow-on work rather "
                "than evaporating cleanly or reaching a ratified decision; commitment is "
                "the more common outcome absent evidence to the contrary"
            ),
        },
        id="predecessor-distill-fate",
        question=(
            "This predecessor handoff has no distill_fate: to reuse -- how did its open "
            "loop actually resolve: ephemeral, commitment, or ratification?"
        ),
        dispositions=[
            build_disposition("ephemeral", resolves=[]),
            build_disposition("commitment", resolves=[]),
            build_disposition("ratification", resolves=[]),
        ],
        evidence="the predecessor handoff's own body + this session's closing assessment",
        reason=(
            "requires the closing session's own assessment of how the predecessor's "
            "work actually resolved -- explicitly needs 'the most context on how the "
            "predecessor's work actually resolved'"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: the answer is backfilled into the
        # predecessor handoff's own `distill_fate:` frontmatter -- a
        # concrete disk write to another file, with no directive gating it.
        reportable=False,
    )


# ---------------------------------------------------------------------------
# Session hygiene (census Steps 2.8, 2.95, 2.96)
# ---------------------------------------------------------------------------


def build_pinboard_note_content_judgment_point() -> dict[str, Any]:
    """Step 2.8: what, if anything, the next session start MUST see and
    would otherwise lose."""
    return build_judgment_point(
        {
            "disposition": "nothing-to-pin",
            "rationale": (
                "the pinboard is a scarce, high-signal surface; absent something that "
                "would genuinely blindside the next session, silence is the correct "
                "default over noise"
            ),
        },
        id="pinboard-note-content",
        question=(
            "Is there anything the next session start MUST see and would otherwise "
            "lose -- and if so, what should the pinboard note say?"
        ),
        dispositions=[
            build_disposition("drafted", resolves=["d-append-orientation-pinboard"]),
            build_disposition("nothing-to-pin", resolves=[]),
        ],
        evidence="this session's decisions/state changes not otherwise captured in a durable artifact",
        reason='genuinely qualitative "would fool the next session too" call',
        revalidate_at_dispatch=False,
    )


def build_orientation_doc_row_updates_judgment_point() -> dict[str, Any]:
    """Step 2.8: project-tracker / action-items / docs-index row updates
    -- the same identify-affected-rows-and-write-the-update shape, three
    times over."""
    return build_judgment_point(
        {
            "disposition": "rows-identified-and-drafted",
            "rationale": (
                "a session that changed durable state almost always has at least one "
                "stale row somewhere across these three surfaces; the default "
                "expectation is a diff exists, not that none does"
            ),
        },
        id="orientation-doc-row-updates",
        question=(
            "Which project-tracker / action-items / docs-index rows did this session "
            "affect, and how should they now read?"
        ),
        dispositions=[
            build_disposition("rows-identified-and-drafted", resolves=[]),
            build_disposition("no-rows-affected", resolves=[]),
        ],
        evidence="this session's touched surfaces against the tracker/action-items/docs-index's own row set",
        reason=(
            "same shape three times over: identify affected rows, write the update -- "
            "not derivable from a fixed rule"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: the recommended disposition itself is
        # "rows-identified-and-drafted" -- the EM must write those drafted
        # rows into the tracker/action-items/docs-index, a concrete edit
        # with no directive behind it.
        reportable=False,
    )


def build_cross_cutting_check_judgment_point() -> dict[str, Any]:
    """Step 2.95: "anything cross-cutting Step 2.9 wouldn't have
    surfaced?" -- a self-check across install-surface/security/docs/lessons
    and any other affected surface."""
    return build_judgment_point(
        {
            "disposition": "nothing-found",
            "rationale": (
                "this is a backstop scan, not a primary detection mechanism; absent a "
                "specific recalled gap, nothing-found is the honest default rather than "
                "manufacturing a finding to justify the check"
            ),
        },
        id="cross-cutting-check",
        question=(
            "Is there anything cross-cutting (install-surface, security, docs, lessons, "
            "or otherwise) that the review pass wouldn't have surfaced?"
        ),
        dispositions=[
            build_disposition("nothing-found", resolves=[]),
            build_disposition("cross-cutting-issue-found", resolves=[]),
        ],
        evidence="self-check across install-surface/security/docs/lessons and any other affected surface",
        reason=(
            "open-ended recall/assessment across five example (non-exhaustive) areas -- "
            "the definition of judgment residue"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) acknowledgement-class: same shape as the plan's own
        # motivating example (the archived-ancestor-batons "if that wasn't
        # right, check on it" self-check) -- the recommended answer
        # ("nothing-found") is a pure backstop scan result the EM merely
        # notes; demoting it loses nothing.
        reportable=True,
    )


def build_inline_waiver_recognition_judgment_point() -> dict[str, Any]:
    """Step 2.96: recognize "the EM has explicitly waived it with a
    one-line rationale" as satisfying a completeness-checklist item."""
    return build_judgment_point(
        {
            "disposition": "no-waiver-found",
            "rationale": (
                "a waiver is a deliberate act that should be easy to recall if it "
                "happened; absent a specific recollection, treat the item as unwaived "
                "rather than assume one occurred"
            ),
        },
        id="inline-waiver-recognition",
        question="Was this completeness-checklist item actually waived this session, with an explicit one-line rationale?",
        dispositions=[
            build_disposition("waiver-recognized", resolves=[]),
            build_disposition("no-waiver-found", resolves=[]),
        ],
        evidence="this session's own transcript for an explicit inline waiver statement",
        reason=(
            "recalling whether an ad hoc inline waiver was actually given this session "
            "is not a structured-data lookup as currently specified"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) acknowledgement-class: this is a recall check over the
        # session's own transcript ("did this actually happen"), not a
        # decision that triggers a distinct EM action -- the checklist item
        # itself is resolved elsewhere regardless of this answer.
        reportable=True,
    )


# ---------------------------------------------------------------------------
# Review residue (census Step 2.9/2.9b -- D-3's 8-point set, PM-ratified
# "go hard" mechanical extraction, these 8 the only survivors)
# ---------------------------------------------------------------------------


def build_review_partition_strategy_judgment_point(
    partition_resolves_ids: list[str] | None = None,
) -> dict[str, Any]:
    """No mechanical rule is given for slicing a diff into coherent review
    chunks -- the genuine architectural-partitioning call.

    `partition_resolves_ids` MUST be the caller's `directives_review.
    review_partition_resolves_ids(review_partition)` — the per-slice
    suffixed ids. See `build_lesson_worth_capturing_judgment_point` for why
    the unsuffixed base can never open the gate."""
    return build_judgment_point(
        {
            "disposition": "by-package-boundary",
            "rationale": (
                "package/module boundaries are the most stable proxy for reviewable "
                "concern boundaries, and are what this fleet's own fan-out dispatch "
                "doctrine already assumes when scoping disjoint chunks"
            ),
        },
        id="review-partition-strategy",
        question=(
            "How should this diff be sliced into coherent review chunks -- by package "
            "boundary, by concern, or by directory cluster?"
        ),
        dispositions=[
            build_disposition("by-package-boundary", resolves=list(partition_resolves_ids or [])),
            build_disposition("by-concern", resolves=list(partition_resolves_ids or [])),
            build_disposition("by-directory-cluster", resolves=list(partition_resolves_ids or [])),
        ],
        evidence="the diff's own touched-path shape (directories, packages, concern boundaries touched)",
        reason=(
            "no mechanical rule is given for how to divide the diff into coherent "
            "slices -- the genuine architectural-partitioning call"
        ),
        revalidate_at_dispatch=False,
        # `resolves` is resolver-populated: an empty list here means the
        # `decisions` slice is not filled in yet, not that this point gates
        # nothing. Exempts it from the `_emit` unclassified-scan; it is not a
        # `reportable` classification and must never be used as one.
        resolves_computed=True,
    )


def build_reviewer_count_on_oracle_disagreement_judgment_point(
    partition_resolves_ids: list[str] | None = None,
) -> dict[str, Any]:
    """On tier B/none oracle disagreement, the EM must record an actual
    reviewer-count decision cross-checked against findings artifacts.

    `partition_resolves_ids`: see `build_review_partition_strategy_
    judgment_point`."""
    return build_judgment_point(
        {
            "disposition": "multi-reviewer-fan-out",
            "rationale": (
                "oracle disagreement is itself evidence the diff is not obviously "
                "low-risk; erring toward more coverage costs little against fan-out's "
                "cheap marginal reviewer"
            ),
        },
        id="reviewer-count-on-oracle-disagreement",
        question="The oracle tier disagreement (tier B/none) -- how many reviewers should actually dispatch?",
        dispositions=[
            build_disposition("single-reviewer", resolves=[]),
            build_disposition("multi-reviewer-fan-out", resolves=list(partition_resolves_ids or [])),
        ],
        evidence="the three oracles' disagreeing tier reads + the diff's own size/shape",
        reason=(
            "explicitly requires 'a recorded EM reviewer-count decision' when the three "
            "oracles disagree -- genuinely no single correct computed answer"
        ),
        revalidate_at_dispatch=False,
        # `resolves` is resolver-populated: an empty list here means the
        # `decisions` slice is not filled in yet, not that this point gates
        # nothing. Exempts it from the `_emit` unclassified-scan; it is not a
        # `reportable` classification and must never be used as one.
        resolves_computed=True,
    )


def build_shared_schema_touch_check_judgment_point() -> dict[str, Any]:
    """Row 3's third disjunct: judging whether a touched file counts as a
    "shared schema/seam" -- semantic classification, not a path-pattern
    predicate as written."""
    return build_judgment_point(
        {
            "disposition": "shared-seam-touched",
            "rationale": (
                "treating an ambiguous touch as shared-seam is the conservative read; "
                "a false positive costs one extra reviewer pass, a false negative risks "
                "an unreviewed cross-cutting break"
            ),
        },
        id="shared-schema-touch-check",
        question="Does this diff touch a shared schema/seam that widens the review's blast radius?",
        dispositions=[
            build_disposition("shared-seam-touched", resolves=[]),
            build_disposition("no-shared-seam", resolves=[]),
        ],
        evidence="touched paths against the repo's known shared-schema/seam surfaces",
        reason='semantic classification of a file\'s role, not a path-pattern predicate as written',
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: a "shared-seam-touched" answer widens the
        # review's blast radius (e.g. drives reviewer-count-on-oracle-
        # disagreement's own fan-out call) -- the EM must actually widen
        # dispatch/coverage on this answer, not merely record a fact.
        reportable=False,
    )


def build_governing_spec_identification_judgment_point() -> dict[str, Any]:
    """Row 3-6 sessions: name the spec(s) that govern this session's work
    in the reviewer brief -- a recall/matching judgment, not a computed
    lookup as stated."""
    return build_judgment_point(
        {
            "disposition": "identified",
            "rationale": (
                "a governing spec almost always exists once a plan is in play; the "
                "default expectation is a citation is findable, not that none applies"
            ),
        },
        id="governing-spec-identification",
        question="Which spec(s), if any, govern this session's work -- to name in the reviewer brief?",
        dispositions=[
            build_disposition("identified", resolves=[]),
            build_disposition("none-applicable", resolves=[]),
        ],
        evidence="the session's plan/spec backlinks and touched-path domain",
        reason=(
            "identifying which spec(s) govern a given session's diff is a "
            "recall/matching judgment, not a computed lookup as stated"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: when torn, the plan directs choosing
        # False. This answer is copied verbatim into the reviewer dispatch
        # brief the EM authors -- the EM must actually write that citation
        # into the brief on the answer, not merely record an opinion.
        reportable=False,
    )


def build_finding_tradeoff_escalation_check_judgment_point() -> dict[str, Any]:
    """Fix every finding including nitpicks; escalate to the PM only on a
    "real tradeoff" -- the break-vs-direction discriminator applied to one
    review finding."""
    return build_judgment_point(
        {
            "disposition": "fix-now",
            "rationale": (
                "per the break-class-is-fix-by-default doctrine, escalation is the "
                "exception (a named no-correct-answer tradeoff), not the default "
                "posture on a finding"
            ),
        },
        id="finding-tradeoff-escalation-check",
        question="Is this review finding a plain fix, or a genuine tradeoff that needs PM escalation?",
        dispositions=[
            build_disposition("fix-now", resolves=[]),
            build_disposition("escalate-to-pm", resolves=[]),
        ],
        evidence="the specific review finding's own content",
        reason=(
            "distinguishing a tradeoff-free fix from a genuine tradeoff is exactly the "
            "judgment call coordinator doctrine names throughout -- not mechanizable here"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo, premise-finding sidecar channel 3) action-class: the
        # answer decides whether the PM is escalated to at all -- no
        # directive, no gate, but the EM must still DO something on the
        # answer. Named explicitly by the plan's premise-finding sidecar.
        reportable=False,
    )


def build_shallow_row3_waive_check_judgment_point() -> dict[str, Any]:
    """The EM retains waive authority on "genuinely shallow" row-3 diffs;
    test is diff shape, deliberately left un-mechanized as a backstop
    against ceremony-avoidance bias."""
    return build_judgment_point(
        {
            "disposition": "proceed-with-tier",
            "rationale": (
                "this point exists specifically as a backstop against "
                "ceremony-avoidance bias; the default should require evidence of "
                "genuine shallowness before waiving, not assume it"
            ),
        },
        id="shallow-row3-waive-check",
        question="Is this row-3 diff genuinely shallow enough to waive the extra review tier?",
        dispositions=[
            build_disposition("waive", resolves=[]),
            build_disposition("proceed-with-tier", resolves=[]),
        ],
        evidence="the diff's own shape (size, touched-path count, concern spread)",
        reason=(
            "explicitly a diff-shape judgment call, deliberately left un-mechanized as "
            "a backstop against ceremony-avoidance"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: "waive" means the EM skips dispatching the
        # extra review tier -- a concrete behavioral decision (does the
        # extra review happen or not), no directive behind it.
        reportable=False,
    )


def build_review_dispatch_vehicle_choice_judgment_point(
    partition_resolves_ids: list[str] | None = None,
) -> dict[str, Any]:
    """The EM MAY run the partition via the review-wave background
    Workflow instead of hand-dispatching -- a discretionary vehicle
    choice, bounded and optional but still a real "which mechanism" call.

    The recommendation is `hand-dispatch` because the Workflow vehicle
    bypasses sidecar provisioning: the provisioner runs in a `PreToolUse`
    hook matched on the `Agent` tool, and a Workflow-internal ``agent()``
    spawn is not an `Agent` tool call, so a ``report_sidecar``-eligible
    reviewer arrives with no ``sidecar_path`` and correctly refuses to
    review. Recommending a vehicle that silently strips context an agent
    type declares as spawn-provided is the failure this default exists to
    avoid -- see `test_review_dispatch_vehicle_choice_does_not_recommend_
    the_provisioning_bypassing_vehicle`.

    `partition_resolves_ids`: see `build_review_partition_strategy_
    judgment_point`."""
    return build_judgment_point(
        {
            "disposition": "hand-dispatch",
            "rationale": (
                "the background-Workflow vehicle bypasses the Agent-tool PreToolUse "
                "hook that provisions report sidecars, so report_sidecar-eligible "
                "reviewers spawn without the sidecar_path their own contract requires "
                "and return unable to review; choose the Workflow vehicle only after "
                "explicitly pre-provisioning each eligible agent's sidecar path and "
                "injecting it into the workflow brief"
            ),
        },
        id="review-dispatch-vehicle-choice",
        question="Dispatch this review partition by hand, or via the review-wave background Workflow?",
        dispositions=[
            build_disposition("hand-dispatch", resolves=list(partition_resolves_ids or [])),
            build_disposition("review-wave-workflow", resolves=list(partition_resolves_ids or [])),
        ],
        evidence="the partition's wave count and this session's remaining context budget",
        reason=(
            "a discretionary vehicle choice, not itself derivable -- bounded and "
            "optional, but still a real 'which mechanism' call"
        ),
        revalidate_at_dispatch=False,
        # `resolves` is resolver-populated: an empty list here means the
        # `decisions` slice is not filled in yet, not that this point gates
        # nothing. Exempts it from the `_emit` unclassified-scan; it is not a
        # `reportable` classification and must never be used as one.
        resolves_computed=True,
    )


def build_quota_retry_vs_escalate_judgment_point() -> dict[str, Any]:
    """"The EM decides retry vs escalate based on retry budget" -- bounded
    but genuinely situational."""
    return build_judgment_point(
        {
            "disposition": "retry",
            "rationale": (
                "a single quota hit is usually transient; retrying once is cheap and "
                "preserves momentum, with escalation reserved for a second consecutive "
                "hit"
            ),
        },
        id="quota-retry-vs-escalate",
        question="A dispatch hit a quota/rate-limit signal -- retry, or escalate?",
        dispositions=[
            build_disposition("retry", resolves=[]),
            build_disposition("escalate", resolves=[]),
        ],
        evidence="remaining retry budget + how time-sensitive the current work is",
        reason=(
            "'the EM decides retry vs escalate based on retry budget' -- bounded but "
            "genuinely situational"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: the answer decides whether the EM retries
        # the dispatch or escalates -- a concrete next action either way, no
        # directive gates it.
        reportable=False,
    )


# ---------------------------------------------------------------------------
# Concurrent-EM / commit / tail residue (census Steps 3.0, 3, 3.5, 4)
# ---------------------------------------------------------------------------


def build_concurrent_peer_attribution_judgment_point() -> dict[str, Any]:
    """Step 3.0: before disposing of a case-(c) file, judge whether an
    active peer session plausibly owns it, using ambiguous signals. Source
    text: "ask... default to treating the path as case (c)... rather than
    guessing peer-vs-orphan" -- an acknowledged-ambiguous call over
    another session's activity. Tier 3 (evidence is peer-authored, not
    this engine's own computation) AND freshness-sensitive: a peer can
    claim-and-push between brief-compute time and dispatch time, mirroring
    pickup's own positive-liveness predicate -- the one point in this
    module carrying `revalidate_at_dispatch=True`."""
    return build_untrusted_gate_judgment_point(
        id="concurrent-peer-attribution",
        question="Does an active peer session plausibly own this ambiguous file?",
        dispositions=[
            build_disposition("peer-owned", resolves=[]),
            build_disposition("orphan-default-case-c", resolves=[]),
        ],
        evidence="recent commits from other topics + unrecognized plan/roadmap paths on the shared branch",
        reason=(
            "the source text is explicit: 'ask... default to treating the path as case "
            "(c)... rather than guessing peer-vs-orphan' -- an acknowledged-ambiguous "
            "call over another session's activity, not this engine's own computation"
        ),
        revalidate_at_dispatch=True,
    )


def build_unattributable_file_disposition_judgment_point() -> dict[str, Any]:
    """Step 3.0's case-(c) disposition ladder: for a genuinely
    unattributable file, choose commit-with-provenance /
    stash-with-provenance / explicit-leave-it-owned-by-X."""
    return build_judgment_point(
        {
            "disposition": "leave-it-owned-by-x",
            "rationale": (
                "an unattributable file on a shared branch is safest left untouched "
                "with a named-owner note; committing or stashing another session's "
                "in-flight work risks destroying it"
            ),
        },
        id="unattributable-file-disposition",
        question=(
            "For this genuinely unattributable file, which remedy fits -- "
            "commit-with-provenance, stash-with-provenance, or "
            "explicit-leave-it-owned-by-X?"
        ),
        dispositions=[
            build_disposition("commit-with-provenance", resolves=[]),
            build_disposition("stash-with-provenance", resolves=[]),
            build_disposition("leave-it-owned-by-x", resolves=[]),
        ],
        evidence="the file's own diff coherence/risk + concurrent-peer-attribution's resolved disposition",
        reason=(
            "choosing among three remedies requires assessing the change's coherence "
            "and risk -- not reducible to a rule"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: every disposition here is itself a
        # concrete disk action (commit, stash, or leave a note) the EM must
        # carry out -- no directive gates any of the three.
        reportable=False,
    )


def build_commit_message_authoring_judgment_point() -> dict[str, Any]:
    """Step 3's mandatory-commit-shape: EM-authored commit subject/body
    prose -- "same bar as any commit"."""
    return build_judgment_point(
        {
            "disposition": "drafted",
            "rationale": (
                "the diff itself is sufficient evidence to draft a conforming "
                "subject/body; escalate only if the change's intent genuinely can't be "
                "summarized from the diff alone"
            ),
        },
        id="commit-message-authoring",
        question="What should this commit's subject and body prose say?",
        dispositions=[
            build_disposition("drafted", resolves=["d-run-wsc-tail"]),
            build_disposition("needs-second-pass", resolves=[]),
        ],
        evidence="this session's actual diff + the commit-message conventions this repo already enforces",
        reason='explicitly named "same bar as any commit" -- authorial',
        revalidate_at_dispatch=False,
    )


def build_session_work_summary_judgment_point() -> dict[str, Any]:
    """Step 4: 1-2 sentence "work done" summary -- authorial synthesis of
    the whole session."""
    return build_judgment_point(
        {
            "disposition": "drafted",
            "rationale": (
                "the session's own commit and decision history is sufficient evidence "
                "for a synthesis; escalate only on a genuinely multi-threaded session "
                "that resists a 1-2 sentence summary"
            ),
        },
        id="session-work-summary",
        question="What 1-2 sentence summary captures what this session actually accomplished?",
        dispositions=[
            build_disposition("drafted", resolves=["d-emit-cadence", "d-render-final-summary"]),
            build_disposition("needs-second-pass", resolves=[]),
        ],
        evidence="the session's full commit/decision history",
        reason="authorial synthesis of the whole session",
        revalidate_at_dispatch=False,
    )


def build_flag_severity_classification_judgment_point() -> dict[str, Any]:
    """Step 4: classify any flagged item as break-class (fix-by-default)
    vs direction-class (ask PM) before listing it -- applying the
    discriminator requires reading the specific finding."""
    return build_judgment_point(
        {
            "disposition": "break-class",
            "rationale": (
                "per this fleet's own flag-severity doctrine, break-class is "
                "fix-by-default and is the wider of the two buckets; direction-class "
                "is the named exception (product/prioritization/irreversible/genuine-"
                "tradeoff), not the default read"
            ),
        },
        id="flag-severity-classification",
        question="Is this flagged item break-class (fix-by-default) or direction-class (ask the PM)?",
        dispositions=[
            build_disposition("break-class", resolves=[]),
            build_disposition("direction-class", resolves=[]),
        ],
        evidence="the flagged item's own content against the break-vs-direction discriminator",
        reason=(
            "applying the break/direction discriminator to this session's specific "
            "findings requires reading them, not a fixed rule"
        ),
        revalidate_at_dispatch=False,
        # (C2 redo) action-class: per fleet doctrine, break-class is
        # fix-by-default -- the recommended answer directly obligates the EM
        # to fix the item, the same shape as
        # finding-tradeoff-escalation-check (fix-now vs escalate-to-pm).
        reportable=False,
    )


def build_stage_paths_missing_judgment_point(
    candidate_paths: list[str] | None = None,
    known_concurrent_paths: "frozenset[str] | set[str] | tuple[str, ...] | None" = None,
) -> dict[str, Any]:
    """Blocks `d-run-wsc-tail` when `decisions['stage_paths']` did not
    resolve to a non-empty value. `wsc-tail.py --stage-paths` is OPTIONAL
    at the CLI layer (unlike `--subject`, which argparse hard-requires) --
    but its omission is not benign: `build_wsc_tail_directive`
    (`directives_commit_tail.py`, C2e) simply drops the flag, and the tail
    then stages/commits only whatever its own dirty-tree gates independently
    sweep, silently under-committing the session's own completion entry,
    lessons, and review-trail records while still reporting `landed`
    (state/bug-backlog/2026-07-29-workstream-complete-silently-under-commi-
    33e5cdf24112.yaml, DoE-claude).

    `candidate_paths` is the CALLER's already-derived candidate set --
    `resolve_session_start_time` -> `classify_session_authored_files` ->
    `accumulate_session_paths` (`directives_memo_lifecycle`/
    `directives_commit_tail`), with `known_concurrent_paths` supplied by
    `directives_commit_tail.resolve_known_concurrent_paths` (the Step 3.0
    case-(b) peer-exclusion producer) -- this function does not read disk or
    call the chain itself (this module's own negative-spec: no subprocess,
    no git, purely declarative), it only formats what the caller already
    computed into this gate's `evidence` string. The candidate set is a
    BETTER-INFORMED set now that a real producer backs it, but it is still
    only advisory evidence for this gate -- the EM reviews and narrows it
    before handing back `decisions['stage_paths']`, never auto-staged.

    `known_concurrent_paths` is the CALLER's own already-computed exclusion
    set (the SAME `directives_commit_tail.resolve_known_concurrent_paths`
    result the caller already threaded into `classify_session_authored_
    files` to PRODUCE `candidate_paths` above) -- passed through here purely
    so `evidence` can report the real set that was actually applied, rather
    than a hardcoded `frozenset()` literal that claimed no exclusion ever
    ran. This function still does not compute it, call it, or read disk for
    it -- it only echoes what the caller already has.

    Offered as EVIDENCE only, in a structurally unresolvable gate (its one
    disposition's `resolves` list is deliberately empty, mirroring
    `build_commit_subject_missing_judgment_point`'s own contract) -- never
    auto-staged, never auto-committed. Trading a silent under-commit for a
    silent over-commit (staging a live peer session's untracked files on a
    shared branch) would be strictly worse, so the EM must review the
    candidate list, exclude any concurrent peer's paths, and hand the
    narrowed result back as `decisions['stage_paths']` before this gate
    clears."""
    if candidate_paths:
        candidate_line = "; ".join(candidate_paths)
    else:
        candidate_line = "(none derived -- no dirty-tree path matched the session-authored predicate)"

    if known_concurrent_paths:
        known_concurrent_line = "{" + ", ".join(sorted(known_concurrent_paths)) + "}"
    else:
        known_concurrent_line = "frozenset()"

    return build_untrusted_gate_judgment_point(
        id="jp-stage-paths-missing",
        question=(
            "d-run-wsc-tail needs an explicit stage-paths pathspec -- supply it as "
            "decisions['stage_paths'] (a list of paths for `git add`/`git commit`), or the "
            "tail will silently commit only whatever its own gates happen to sweep."
        ),
        dispositions=[build_disposition("stage-paths-not-yet-supplied", resolves=[])],
        evidence=(
            "decisions['stage_paths'] absent. CANDIDATE session-authored path set "
            "(resolve_session_start_time -> classify_session_authored_files -> "
            f"accumulate_session_paths, known_concurrent_paths={known_concurrent_line}): {candidate_line}"
        ),
        reason=(
            "directives_commit_tail.resolve_known_concurrent_paths is the Step 3.0 case-(b) "
            "known_concurrent_paths exclusion-set producer, wired into this candidate set "
            "above; the EM must still review and narrow this candidate list before "
            "supplying it back as decisions['stage_paths'] -- that producer is best-effort "
            "and conservative, not a guarantee, so the candidate list above may still "
            "include a live peer session's untracked files this pass's exclusion set did "
            "not catch. Auto-staging it unreviewed would trade a silent under-commit for a "
            "silent over-commit onto another session's work; this gate never stages or "
            "commits anything itself."
        ),
    )


# ---------------------------------------------------------------------------
# No-commit row disposition (DoE-claude docs/plans/2026-07-29-pm-approved-
# provenance-write-time-closure-gate.md, chunk C13 -- NOT part of the C2f
# census above; a later addition, its own judgment point, kept out of
# `JUDGMENT_POINT_BUILDERS` deliberately -- see that tuple's own docstring
# for why it is scoped to the 29-point C2f census and this builder is not
# one of those 29)
# ---------------------------------------------------------------------------


#: Plain-language reason strings for every join-provenance value the
#: `close_out_and_stamp._determine_shipped` oracle can report OTHER than
#: `"joined"` -- deliberately a LOCAL mirror of that module's own
#: `_JOIN_PROVENANCE_REASON` mapping (this module's negative-spec forbids
#: importing/reading/calling git-adjacent machinery from `close_out_and_
#: stamp.py`; these are the four literal provenance-value strings that
#: module's own docstring documents as its stable contract, not live code
#: this module reaches into). Used by `build_no_commit_row_disposition_
#: judgment_point` to reframe its own `evidence` text when the join itself
#: -- not the underlying work -- is why a row reads as no-covering-commit.
_JOIN_PROVENANCE_UNATTRIBUTABLE_REASON = {
    "no_join_key": (
        "the governing plan's own frontmatter carries no deliverable_id: "
        "field, so the commit-coverage join was never attempted"
    ),
    "no_join_candidates": (
        "no commit in the oracle's search range carries a Deliverable-Id "
        "trailer at all, so there was nothing to join against"
    ),
    "key_mismatch": (
        "commits in range carry a Deliverable-Id trailer, but never one "
        "equal to the governing plan's own frontmatter value, so the join "
        "could not match them"
    ),
    "no_evidence_source": (
        "the governing plan has neither a ## Tasks spine nor a "
        "## Dispatch Ledger heading, so no evidence source existed to "
        "consult at all -- 'shipped' reflects nothing to check, not a "
        "verified delivery"
    ),
}


def build_no_commit_row_disposition_judgment_point(
    no_commit_row_ids: list[str] | None = None,
    join_provenance: str = "joined",
) -> dict[str, Any] | None:
    """AC21/AC21b: a task-spine row this session's commit-coverage oracle
    found no covering commit for must not resolve to a silent "it's
    deferred" -- the PM's own words: "right the way at workstream-complete
    there are guards to say 'hey what about this thing without a commit'
    and the EM thinks 'of course no commit, it's deferred, ugh, what is
    this stupid guard anyway'." Verification against the running code
    found no such guard anywhere in `workstream_complete/{apply,
    judgments,__init__}.py` before this chunk -- this is BUILD, not harden.

    Returns `None` when `no_commit_row_ids` is empty/`None` -- mirrors
    every other conditionally-emitted point in this module (e.g.
    `build_stage_paths_missing_judgment_point`'s caller-side `if not
    decisions.get(...)` gate): a caller only invokes this builder once it
    already knows there is something to ask about, so an empty list here
    is the "nothing to surface" case, not an error.

    `no_commit_row_ids` is the CALLER's already-computed list of
    commit-required spine-row ids (disposition `open`/`coded`, per
    `coordinator_core.execute_plan_assemble.close_out_and_stamp.
    _commit_required_chunk_ids`) that the SAME module's `_determine_shipped`
    /`_committed_chunk_shas` commit-coverage oracle found no covering
    commit for -- reused, not reimplemented (this module's own
    negative-spec forbids reading disk or calling git; the oracle that
    does both lives in `close_out_and_stamp.py` and is the `/execute-plan`
    Phase 4 close-out's own completeness check, already proven against a
    multi-repo, multi-plan corpus).

    `join_provenance` (cross-repo memo fix -- see `_determine_shipped`'s own
    widened docstring for the four values it can carry) is the SAME
    oracle's own join-provenance verdict for the plan `no_commit_row_ids`
    was computed against, threaded straight through by the caller. This
    reframes `evidence` ONLY -- a reporting separation, never a sixth
    silent exit: the judgment still fires unconditionally whenever
    `no_commit_row_ids` is non-empty, regardless of `join_provenance`, and
    the five named dispositions below are unchanged. Default `"joined"`
    preserves this builder's pre-existing evidence wording for every
    caller that does not (yet) thread the value through.

    Five named exits, matching the five-exit ruling
    (cross-repo/inbox/2026-08-05-doe-claude-em-plan-tasks-five-exits-
    ruling.md, ask 3; `python3 coordinator/bin/plan-tasks-resolve --id <row-id>
    ...`) rather than the pre-ruling three -- ships (a commit lands,
    `disposition: coded`, via `--coded <sha>` -- no PM word), spun-off (a
    row moved off this plan -- to a new plan via `--spun-off <ref>` or an
    existing one via `--moved-to <plan-path>` -- `disposition: spun_off`,
    relaxed at DoE `bd0475fd5`/schema 1.4.0 -- no PM word), backlogged
    (`disposition: backlogged` via `--backlogged` -- a PM word is
    required, retained by PM ruling), wont-do (`disposition: wont_do` via
    `--wont-do` -- a PM word is required, retained by PM ruling), or
    carried-forward (written into the successor baton's `carried_items[]`
    with a `carry_id`, `handoff_carry_gate.evaluate_gate`'s existing
    undeclared-state refusal applying unchanged -- carry DEPTH is not
    refused, see DR-278). "Deferred, ignore the guard"
    is deliberately not a sixth disposition -- that silent exit is exactly
    what this point exists to close off, per the plan's own Anti-scope:
    this is not a hard block on finishing a workstream with rows
    genuinely `open` and carried (AC22), it only removes the SILENT sixth
    exit. A non-`"joined"` `join_provenance` does NOT introduce a sixth
    disposition either -- the EM still answers shipped/spun-off/
    backlogged/wont-do/carried-forward, now with evidence text that
    correctly names an unjoinable key rather than implying the rows are
    unshipped."""
    if not no_commit_row_ids:
        return None
    row_line = ", ".join(sorted(no_commit_row_ids))

    if join_provenance == "joined":
        evidence = (
            "governing plan's ## Tasks spine, commit-required rows (disposition "
            "open/coded) cross-referenced against this session's commit-coverage "
            "oracle (close_out_and_stamp._determine_shipped / "
            "_committed_chunk_shas) -- row(s) with no covering commit found: "
            f"{row_line}"
        )
    else:
        reason_text = _JOIN_PROVENANCE_UNATTRIBUTABLE_REASON.get(
            join_provenance,
            "the commit-coverage oracle's Deliverable-Id join could not "
            "attribute any commit to the governing plan",
        )
        evidence = (
            "governing plan's ## Tasks spine, commit-required rows (disposition "
            "open/coded) cross-referenced against this session's commit-coverage "
            "oracle (close_out_and_stamp._determine_shipped / "
            "_committed_chunk_shas) -- the oracle's Deliverable-Id join is "
            f"UNATTRIBUTABLE ({join_provenance}: {reason_text}), so these row(s) "
            "read as no-covering-commit for THAT reason, not because the work "
            f"is known to be unshipped: {row_line}"
        )

    return build_judgment_point(
        {
            "disposition": "carried-forward",
            "rationale": (
                "absent evidence the row shipped or was closed with a current PM-"
                "approved grouping, carrying it forward on the successor baton is "
                "the safe default -- it keeps the row visible rather than letting "
                "it silently evaporate, and costs nothing but one more carry"
            ),
        },
        id="jp-no-commit-row-disposition",
        question=(
            "Task-spine row(s) with no covering commit this pass -- "
            f"{row_line} -- five exits via `python3 coordinator/bin/plan-tasks-resolve "
            "--id <row-id> ...`: did they ship (--coded <sha>, no PM word), get "
            "spun off or moved to an existing plan (--spun-off/--moved-to, no "
            "PM word), get backlogged (--backlogged, PM word required), get "
            "ruled won't-do (--wont-do, PM word required), or should they be "
            "explicitly carried forward on the successor baton?"
        ),
        dispositions=[
            build_disposition("shipped", resolves=[]),
            build_disposition("spun-off", resolves=[]),
            build_disposition("backlogged", resolves=[]),
            build_disposition("wont-do", resolves=[]),
            build_disposition("carried-forward", resolves=[]),
        ],
        evidence=evidence,
        reason=(
            "resolving a no-commit row to 'it's deferred' is a scope decision, "
            "not a fixed predicate this engine can pick for itself -- shipped "
            "requires a real commit the oracle can find, spun-off requires a "
            "recorded `disposition_ref` (new or existing plan -- no PM word, "
            "relaxed at DoE bd0475fd5/schema 1.4.0), backlogged and wont-do "
            "each require a PM word (retained by PM ruling -- cross-repo/inbox/"
            "2026-08-05-doe-claude-em-plan-tasks-five-exits-ruling.md), and "
            "carried-forward requires an explicit handoff_carry_gate carry_id, "
            "so none of the five resolves without an actual decision"
        ),
        revalidate_at_dispatch=False,
    )


#: Every builder in this module, in census emission order (lessons/plan ->
#: completion -> memo/scratch -> session hygiene -> review -> commit/tail).
#: `__init__.py` (C3) imports this tuple to populate `judgment_points[]`
#: alongside the two pre-existing standing points it already builds
#: directly. Exactly 29 entries -- kept in sync with this module's own
#: docstring roster; a mismatch here is an authoring bug, not a intentional
#: subset. `build_no_commit_row_disposition_judgment_point` (C13, a later
#: plan) is DELIBERATELY excluded -- see its own docstring's section
#: banner for why.
JUDGMENT_POINT_BUILDERS: tuple[Callable[[], dict[str, Any]], ...] = (
    build_lesson_worth_capturing_judgment_point,
    build_lesson_scope_classification_judgment_point,
    build_plan_doc_content_update_judgment_point,
    build_plan_vs_reality_reconcile_judgment_point,
    build_enablement_vs_opportunistic_deferral_judgment_point,
    build_completion_nature_classification_judgment_point,
    build_completion_entry_prose_judgment_point,
    build_commit_significance_filter_judgment_point,
    build_memo_resolution_attribution_judgment_point,
    build_do_now_memo_violation_check_judgment_point,
    build_scratch_disposition_per_file_judgment_point,
    build_predecessor_distill_fate_judgment_point,
    build_pinboard_note_content_judgment_point,
    build_orientation_doc_row_updates_judgment_point,
    build_cross_cutting_check_judgment_point,
    build_inline_waiver_recognition_judgment_point,
    build_review_partition_strategy_judgment_point,
    build_reviewer_count_on_oracle_disagreement_judgment_point,
    build_shared_schema_touch_check_judgment_point,
    build_governing_spec_identification_judgment_point,
    build_finding_tradeoff_escalation_check_judgment_point,
    build_shallow_row3_waive_check_judgment_point,
    build_review_dispatch_vehicle_choice_judgment_point,
    build_quota_retry_vs_escalate_judgment_point,
    build_concurrent_peer_attribution_judgment_point,
    build_unattributable_file_disposition_judgment_point,
    build_commit_message_authoring_judgment_point,
    build_session_work_summary_judgment_point,
    build_flag_severity_classification_judgment_point,
)

assert len(JUDGMENT_POINT_BUILDERS) == 29, (
    f"JUDGMENT_POINT_BUILDERS must carry exactly 29 entries per the governing plan's "
    f"explicit enumeration, got {len(JUDGMENT_POINT_BUILDERS)}"
)

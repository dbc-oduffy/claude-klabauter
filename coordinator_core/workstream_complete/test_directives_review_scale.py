"""test_directives_review_scale — direct unit coverage of
`coordinator_core.workstream_complete.directives_review.decide_review_scale`
(the diff-shape row-selection table, SKILL.md:413-426).

Spec backlink: docs/plans/2026-08-03-chain-end-review-scale-wiring.md,
chunk C1(b). Pins the tri-state-input contract that chunk introduces: every
row-4/5/6 input (`gross_loc`, `code_loc`, `commit_count`, `surface_count`,
`executor_dispatched`, `shared_schema_touched`, `chain_partition_verdict`)
is independently representable as "not yet resolved" (`None`), distinct
from a resolved falsy value — and an unresolved input that could still
change the selected row must yield the explicit unresolved outcome
(`resolved=False`, `row=None`, `partition_mandatory=False`), never a
silent per-session row. This is the regression pin for the defect the
plan's Problem section names: `chain_diff_trivial=None` used to resolve
toward LESS review on a chain terminal while its own docstring called that
"conservative" — the opposite of true.

C4 (docs/plans/2026-08-03-chain-end-review-scale-wiring.md) owns the
end-to-end coverage through `wsc.brief()`; this file only exercises the
pure function directly, per that plan's own chunk split.
"""

from __future__ import annotations

from coordinator_core.ops.ceremony.wsc_disposition import (
    LEGACY_PREDECESSOR_CONSUMED,
    PREDECESSOR_CONSUMED,
    SINGLE_SESSION,
)
from coordinator_core.workstream_complete.directives_review import (
    _CHAIN_VERDICT_PARTITION_MANDATORY,
    _CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    decide_review_scale,
)

_SINGLE_SESSION = SINGLE_SESSION
_CHAIN_TERMINAL = PREDECESSOR_CONSUMED
_LEGACY_CHAIN_TERMINAL = LEGACY_PREDECESSOR_CONSUMED

# A fully-resolved, small/non-triggering baseline for every row-3/4 input —
# individual tests override only the field(s) under test.
_RESOLVED_SMALL = dict(
    gross_loc=10,
    code_loc=10,
    commit_count=1,
    surface_count=1,
    executor_dispatched=False,
    shared_schema_touched=False,
)


# ---------------------------------------------------------------------------
# Row 1/2 — per-session, no chain-end, no brightline, no row-3 trigger
# ---------------------------------------------------------------------------


def test_row1_doc_only_no_code_touched():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 0},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is True
    assert decision.row == 1
    assert decision.scale == "none"
    assert decision.partition_mandatory is False


def test_row2_small_fix_under_ceiling():
    decision = decide_review_scale(**_RESOLVED_SMALL, chain_disposition=_SINGLE_SESSION)
    assert decision.resolved is True
    assert decision.row == 2
    assert decision.scale == "none"
    assert decision.commit_message_names_change is True


# ---------------------------------------------------------------------------
# Row 3 — executor dispatched / >50 LOC code / shared schema touched
# ---------------------------------------------------------------------------


def test_row3_executor_dispatched():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "executor_dispatched": True},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 3
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_row3_code_loc_over_small_fix_ceiling():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 51},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 3


def test_row3_shared_schema_touched():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "shared_schema_touched": True},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 3


# ---------------------------------------------------------------------------
# Row 4 — session-scoped big-diff brightline, fires regardless of chain
# disposition, and outranks row 3.
# ---------------------------------------------------------------------------


def test_row4_gross_loc_over_brightline():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 500},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 4
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_row4_commit_count_over_brightline():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "commit_count": 5},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 4


def test_row4_surface_count_over_brightline():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "surface_count": 4},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 4


def test_row4_outranks_row3_even_when_row3_would_also_fire():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 500, "executor_dispatched": True},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.row == 4


# ---------------------------------------------------------------------------
# Row 5/6 — chain-terminal rows, keyed on the already-emitted chain
# brightline-gate verdict (chain_partition_verdict), not raw chain metrics.
# ---------------------------------------------------------------------------


def test_row6_chain_terminal_partition_mandatory_verdict():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.resolved is True
    assert decision.row == 6
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_row6_fires_on_legacy_chain_terminal_spelling():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_LEGACY_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row == 6


def test_row6_outranks_session_scoped_row4_brightline():
    """Chain-scoped mandatory partition wins even when the closing
    session's own diff is small — this is the exact gap the plan's
    Problem section describes: a big diff accumulated across many
    individually-small sessions, no single session ever tripping the
    session-scoped brightline."""
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row == 6


def test_row6_outranks_row4_when_both_fire_on_the_same_call():
    """(review-integrator finding 1) The one adversarial combination the
    dispatch brief asked to be challenged hardest: chain_partition_verdict
    is PARTITION-MANDATORY (row 6 would fire) AND the session's own diff
    is simultaneously over the row-4 brightline. Precedence is 6 > 4
    (SKILL.md:426) -- the chain-terminal MANDATORY branch returns
    unconditionally before `brightline_known_true` is ever evaluated. Pins
    that precedence so a future refactor hoisting the brightline check
    above the chain-terminal check cannot silently flip it."""
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 500},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row == 6
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_row4_outranks_row6_when_session_diff_itself_is_big():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 500},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.row == 4


def test_row5_chain_terminal_resolved_non_mandatory_verdict():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.resolved is True
    assert decision.row == 5
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_single_session_disposition_never_reaches_row5_or_6():
    """A non-chain-terminal disposition must never select rows 5/6 even
    when a chain_partition_verdict is (incorrectly) supplied — the
    predicate is gated on disposition first."""
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_SINGLE_SESSION,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
    )
    assert decision.row not in (5, 6)


# ---------------------------------------------------------------------------
# Unresolved outcome — the silent-lenient-fallback kill switch. Every
# assertion below must yield resolved=False, row=None, scale="unresolved",
# partition_mandatory=False — NEVER a silent per-session row.
# ---------------------------------------------------------------------------


def test_unresolved_when_chain_terminal_and_verdict_is_none():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=None,
    )
    assert decision.resolved is False
    assert decision.row is None
    assert decision.scale == "unresolved"
    assert decision.partition_mandatory is False


def test_unresolved_when_chain_terminal_and_verdict_is_unrecognized_string():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict="garbage-not-a-real-verdict",
    )
    assert decision.resolved is False
    assert decision.row is None
    assert decision.partition_mandatory is False


def test_unresolved_never_defaults_to_row5_staff_eng_finding_3():
    """The exact staff-engineer finding-3 case: the chain-scoped verdict HAS
    resolved (and is non-mandatory) but a row-4 input (gross_loc) has not
    — row 4 outranks row 5 and cannot be ruled out, so the outcome must be
    unresolved, never row 5."""
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": None},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.resolved is False
    assert decision.row is None
    assert decision.row != 5
    assert decision.partition_mandatory is False


def test_unresolved_never_defaults_to_row5_when_commit_count_absent():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "commit_count": None},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.resolved is False
    assert decision.row != 5


def test_unresolved_never_defaults_to_row5_when_surface_count_absent():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "surface_count": None},
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_SINGLE_REVIEWER_OK,
    )
    assert decision.resolved is False
    assert decision.row != 5


def test_known_true_brightline_input_still_resolves_row4_even_with_other_inputs_absent():
    """A single input that is ALREADY known to trip the brightline
    (gross_loc >= threshold) resolves row 4 even though a sibling input
    (surface_count) is unresolved — short-circuit on a known-true OR
    branch, not a blanket "any None anywhere means unresolved" rule."""
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 500, "surface_count": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is True
    assert decision.row == 4


def test_unresolved_when_row4_metrics_absent_on_single_session():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": None, "commit_count": None, "surface_count": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is False
    assert decision.row is None


def test_unresolved_when_row3_inputs_absent_after_row4_ruled_out():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "executor_dispatched": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is False
    assert decision.row is None


def test_known_true_row3_input_still_resolves_even_with_code_loc_absent():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "executor_dispatched": True, "code_loc": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert decision.resolved is True
    assert decision.row == 3


def test_unresolved_reason_names_the_unresolved_inputs():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": None, "commit_count": None, "surface_count": None},
        chain_disposition=_SINGLE_SESSION,
    )
    assert "gross_loc" in decision.reason
    assert "commit_count" in decision.reason
    assert "surface_count" in decision.reason


# ---------------------------------------------------------------------------
# `baton_count` — 2026-08-04 sizing (`state/sizings/2026-08-04-mise-run-
# record-should-carry-baton-count.yaml`), source memo
# `cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-
# mandatory-does-not-halt.md`. `None` (every existing caller's implicit
# default) must leave every row selection above byte-identical; a resolved
# `>= 2` is a MULTIPLIER on the row-4 brightline, never a forced partition,
# and FLOORS the outcome away from the no-review rows (1/2).
# shell-doc-ok: the backticked comparison above is a Python boolean
# expression, not a shell version constraint.
# ---------------------------------------------------------------------------


def test_baton_count_none_leaves_row1_unchanged():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "code_loc": 0},
        chain_disposition=_SINGLE_SESSION,
        baton_count=None,
    )
    assert decision.row == 1
    assert decision.scale == "none"


def test_baton_count_none_leaves_row2_unchanged():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_SINGLE_SESSION,
        baton_count=None,
    )
    assert decision.row == 2
    assert decision.scale == "none"


def test_baton_count_none_leaves_row4_brightline_unchanged():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 499},
        chain_disposition=_SINGLE_SESSION,
        baton_count=None,
    )
    assert decision.row != 4, "499 alone must not trip the brightline with no multiplier"


def test_baton_count_one_behaves_identically_to_none():
    with_none = decide_review_scale(**_RESOLVED_SMALL, chain_disposition=_SINGLE_SESSION, baton_count=None)
    with_one = decide_review_scale(**_RESOLVED_SMALL, chain_disposition=_SINGLE_SESSION, baton_count=1)
    assert with_none.row == with_one.row
    assert with_none.scale == with_one.scale


def test_baton_count_multiplier_trips_brightline_that_raw_metrics_do_not():
    # 260 * 2 = 520 >= 500 -- would NOT trip on its own.
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 260},
        chain_disposition=_SINGLE_SESSION,
        baton_count=2,
    )
    assert decision.row == 4
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True
    assert "baton_count=2" in decision.reason


def test_baton_count_multiplier_does_not_force_partition_on_a_trivial_run():
    # Multiplier, not force-partition: a genuinely tiny 2-baton run must
    # still be able to land below the brightline once multiplied.
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 1, "commit_count": 1, "surface_count": 1},
        chain_disposition=_SINGLE_SESSION,
        baton_count=2,
    )
    assert decision.row != 4, "a trivial 2-baton run must not be force-partitioned by the multiplier alone"


def test_baton_count_floors_row1_to_code_reviewer():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 1, "commit_count": 1, "surface_count": 1, "code_loc": 0},
        chain_disposition=_SINGLE_SESSION,
        baton_count=2,
    )
    assert decision.row not in (1, 2), "a resolved baton_count>=2 must never resolve to a no-review row"
    assert decision.row == 3
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_baton_count_floors_row2_to_code_reviewer():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 1, "commit_count": 1, "surface_count": 1},
        chain_disposition=_SINGLE_SESSION,
        baton_count=3,
    )
    assert decision.row not in (1, 2)
    assert decision.row == 3
    assert decision.scale == "code-reviewer"
    assert "baton_count=3" in decision.reason


def test_baton_count_does_not_alter_an_already_resolved_row3():
    decision = decide_review_scale(
        **{**_RESOLVED_SMALL, "gross_loc": 1, "commit_count": 1, "surface_count": 1, "executor_dispatched": True},
        chain_disposition=_SINGLE_SESSION,
        baton_count=2,
    )
    assert decision.row == 3
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_baton_count_does_not_alter_chain_terminal_row6():
    decision = decide_review_scale(
        **_RESOLVED_SMALL,
        chain_disposition=_CHAIN_TERMINAL,
        chain_partition_verdict=_CHAIN_VERDICT_PARTITION_MANDATORY,
        baton_count=4,
    )
    assert decision.row == 6

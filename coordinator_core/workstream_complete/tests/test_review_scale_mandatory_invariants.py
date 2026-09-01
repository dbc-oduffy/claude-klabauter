"""
coordinator_core.workstream_complete.tests.test_review_scale_mandatory_invariants

The gate state doe-claude-em reported as having no legal reader-side move:
`partition_mandatory: true` alongside `resolved: false`, shipping no
`commit_slices` (`cross-repo/archive/2026-08-29-doe-claude-em-review-scale-
ships-no-brightline-inputs.md`, asks (a) and (c)).

WHAT IT COST, since that is the argument for pinning rather than trusting the
current shape: told "mandatory" and handed nothing to read, an EM hand-measured
gross LOC across a shared-branch commit range and reported an inflated number
that reached the PM before it was caught — see `directives_review.py`'s own
"HOW BOTH ARE MEASURED" comment (the 33,246-vs-16,037 incident) for the full
telling; not restated here.

WHY THESE ARE PINS AND NOT A FIX. Ask (a) landed on 2026-08-31 at `b0ab8b1129`
("the unresolved review-scale arm stops reporting a measured negative"),
independently and for a different report — `_unresolved()` now returns
`partition_mandatory=None` rather than `False`. Ask (c) then follows
STRUCTURALLY: `partition_mandatory=True` is reachable only from
`_row4_decision`, and the payload attaches `commit_slices` whenever the
measurement ran.

WHAT `resolved` ACTUALLY MEANS, recorded because writing these tests got it
wrong first. It is NOT "every input was measured" — it is "the row selection is
DETERMINED". `brightline_known_true` is an OR of three independently sufficient
arms, so row 4 is legitimately reachable with one input still `None`: a
resolved proxy that trips is dispositive, and no later measurement can un-trip
it. `_row4_inputs_unresolved` fires only when the brightline is not known true
AND not fully resolved — precisely when a missing input could still change the
answer. That is coherent, and a test asserting `resolved is False` whenever any
input is `None` would be asserting a contract this module does not have and
should not adopt: it would force the gate to withhold a verdict it has already
correctly reached.
"""

from __future__ import annotations

import itertools

import pytest

from coordinator_core.workstream_complete.directives_review import (
    ReviewScaleDecision,
    decide_review_scale,
)

#: Above every row-4 brightline (500 LOC / 5 commits / 4 surfaces).
_BIG = {"gross_loc": 4000, "code_loc": 4000, "commit_count": 26, "surface_count": 9}

_BASE = {
    "executor_dispatched": False,
    "shared_schema_touched": False,
    "chain_disposition": "single-session",
}


def _decide(**over) -> ReviewScaleDecision:
    kwargs = {**_BIG, **_BASE}
    kwargs.update(over)
    return decide_review_scale(**kwargs)


# ---------------------------------------------------------------------------
# ask (a) -- the incoherent state
# ---------------------------------------------------------------------------


def test_every_mandatory_decision_is_a_resolved_one() -> None:
    """The invariant stated directly and swept over the input space rather than
    over the arms that happen to exist today: whatever combination produces
    `partition_mandatory is True` must also be `resolved`. This is the test
    that survives someone adding a fifth row."""
    for code_loc, commit_count, surface_count in itertools.product(
        (None, 0, 4000), (None, 0, 26), (None, 0, 9)
    ):
        decision = _decide(
            code_loc=code_loc, commit_count=commit_count, surface_count=surface_count
        )
        if decision.partition_mandatory is True:
            assert decision.resolved is True, (
                "a mandatory partition was asserted over an undetermined selection "
                f"(code_loc={code_loc}, commit_count={commit_count}, "
                f"surface_count={surface_count}): {decision}"
            )


def test_an_unresolved_decision_asserts_neither_a_row_nor_a_partition() -> None:
    """The specimen's own shape: all three row-4 inputs unmeasured, so nothing
    is determined. Naming a row here would assert a scope the gate never
    measured, and a reader who trusts `row` does not go on to check
    `resolved`."""
    decision = _decide(code_loc=None, commit_count=None, surface_count=None)
    assert decision.resolved is False
    assert decision.partition_mandatory is not True
    assert decision.row is None
    assert decision.scale == "unresolved"


def test_mandatory_is_still_reachable_when_every_input_resolves() -> None:
    """The negative half — the invariant must not be satisfied by never
    asserting mandatory at all, which would silently retire row 4."""
    decision = _decide()
    assert decision.resolved is True
    assert decision.partition_mandatory is True
    assert decision.row == 4


def test_a_tripped_arm_is_dispositive_even_with_an_unmeasured_input() -> None:
    """Deliberate, and the reason the invariant above is phrased as
    mandatory-implies-resolved rather than mandatory-implies-fully-measured: 26
    commits over 9 surfaces trips row 4 whatever `code_loc` turns out to be, so
    withholding the verdict pending a measurement that cannot change it would
    fail toward LESS review, which is the direction this module never fails
    in."""
    decision = _decide(code_loc=None)
    assert decision.row == 4
    assert decision.partition_mandatory is True
    assert decision.resolved is True


# ---------------------------------------------------------------------------
# the reason string -- what actually sent the EM hand-measuring
# ---------------------------------------------------------------------------


def test_the_reason_names_the_arm_that_tripped() -> None:
    decision = _decide(code_loc=4000, commit_count=2, surface_count=1)
    assert "hit on code_loc" in decision.reason
    assert "commits+" not in decision.reason


def test_an_unmeasured_commits_or_surfaces_input_is_named_as_unable_to_change_the_verdict() -> None:
    """`commits`/`surfaces` are pure OR-arms with no veto power: an unmeasured
    reading of either genuinely cannot change a verdict already tripped by
    another arm, and the reason string may say so."""
    decision = _decide(commit_count=None)
    assert "commit_count=None" not in decision.reason
    assert "commits=None" not in decision.reason
    assert "commits not measured, and cannot change this verdict" in decision.reason


def test_an_unmeasured_code_loc_input_is_named_but_not_told_it_cannot_change_the_verdict() -> None:
    """Review: coordinator:code-reviewer (a67271301efadc596) Finding 1 —
    `code_loc` is not a peer of `commits`/`surfaces`: it alone carries veto
    power via `code_loc_resolved_zero`. When `commits`/`surfaces` already
    tripped row 4 while `code_loc` is still unmeasured, a later
    `code_loc == 0` measurement WOULD flip the decision away from mandatory
    partition — so the reason string must not claim it "cannot change this
    verdict" the way it correctly does for `commits`/`surfaces`."""
    decision = _decide(code_loc=None)
    assert "code_loc=None" not in decision.reason
    assert "code_loc not measured" in decision.reason
    assert "code_loc not measured, and cannot change this verdict" not in decision.reason
    assert "code_loc==0 measurement could still suppress this verdict" in decision.reason


def test_code_loc_unmeasured_with_commits_and_surfaces_also_unmeasured_states_both_claims() -> None:
    """When `code_loc` is unmeasured alongside a genuinely-unmeasured
    `commits`/`surfaces` arm, both the narrower `code_loc` claim and the
    unqualified `commits`/`surfaces` claim appear — neither one drowns out
    the other."""
    decision = _decide(code_loc=None, surface_count=None)
    assert "surfaces not measured, and cannot change this verdict" in decision.reason
    assert "code_loc==0 measurement could still suppress this verdict" in decision.reason


def test_a_fully_measured_hit_carries_no_unmeasured_clause() -> None:
    """The clause must not become boilerplate — it appears only when something
    genuinely was not measured."""
    decision = _decide()
    assert "not measured" not in decision.reason
    assert "code_loc=4000" in decision.reason


@pytest.mark.parametrize(
    "missing", ["code_loc", "commit_count", "surface_count"]
)
def test_no_arm_prints_a_none_valued_input(missing: str) -> None:
    """Swept over all three, because the reported instance was one of them and
    a fix aimed at that one leaves two."""
    decision = _decide(**{missing: None})
    assert "=None" not in decision.reason, decision.reason

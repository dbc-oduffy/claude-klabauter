"""`fold-into-plan` — the disposition for a memo that kills a LIVE plan's premise.

PM-directed 2026-07-27 (`cross-repo/archive/2026-07-27-example-cockpit-repo-em-
pickup-fold-into-plan-disposition.md`), landed 2026-09-01.

The gap it closes is an information-destroying reflex, not a wording nit.
`re-plan`'s guidance used to end "not a direct edit", so an EM whose inbound
memo invalidated a plan another session was executing recorded the finding in
a reply and declined the edit. The reply reaches the SENDER's inbox; our
inbound memo gets actioned and archived; the executing EM reads neither --
they read their chunk. The fact evaporates on receipt and execution continues
from a premise the sender already said was dead.

Negative-spec:
  - `fold-into-plan` must resolve `d-action-memo` on BOTH kinds it is offered
    on, and map to `accepted` so `realized_by` (the fold commit's SHA) is
    required -- a fold nobody can find from the memo record is the audit gap
    the memo's point 3 asked to close.
  - `re-plan` must survive as a distinct option. The two are responses to
    different MAGNITUDES; collapsing them would trade one lost response for
    another.
  - `re-plan`'s guidance must no longer forbid the direct edit.
  - The staging discipline must ship WITH the disposition: it invites writes
    into files other sessions hold open, so shipping the invitation without
    the mechanic trades an information-loss bug for a collision bug.

Run: python -m pytest coordinator_core/pickup_assemble/tests/test_fold_into_plan_disposition.py -q
"""
from __future__ import annotations

import coordinator_core.pickup_assemble as pa


def _by_value(kind: str) -> dict:
    return {d["value"]: d for d in pa._KIND_DISPOSITIONS[kind]}


def test_offered_on_both_fyi_and_proposal():
    for kind in ("fyi", "proposal"):
        assert "fold-into-plan" in _by_value(kind), kind


def test_resolves_the_action_directive_on_both_kinds():
    for kind in ("fyi", "proposal"):
        assert _by_value(kind)["fold-into-plan"]["resolves"] == ["d-action-memo"]


def test_maps_to_accepted_so_realized_by_is_required():
    # `accepted` is the channel `_build_action_memo_args` routes through
    # `--decision`, and it is what makes `--realized-by` mandatory. A
    # disposition that resolved `d-action-memo` with no map row would reach
    # `cs_action_memo` with neither `--decision` nor `--actioned-note` and
    # fail loud at dispatch.
    assert pa._MEMO_ACTION_DECISION_MAP[("fyi", "fold-into-plan")] == "accepted"
    assert pa._MEMO_ACTION_DECISION_MAP[("proposal", "fold-into-plan")] == "accepted"


def test_re_plan_survives_as_a_distinct_option():
    assert "re-plan" in _by_value("fyi")


def test_re_plan_no_longer_forbids_the_direct_edit():
    guidance = _by_value("fyi")["re-plan"]["guidance"]
    assert "not a direct edit" not in guidance
    # and it must point at the smaller response rather than leaving the EM
    # to conclude there isn't one
    assert "fold-into-plan" in guidance


def test_guidance_ships_the_staging_discipline():
    for kind in ("fyi", "proposal"):
        guidance = _by_value(kind)["fold-into-plan"]["guidance"]
        assert "apply --cached" in guidance, kind
        assert "stash" in guidance, kind
        # the annotate-don't-rewrite half, which is what keeps a fold from
        # becoming a re-scope of someone else's work
        assert "superseded-in-part" in guidance, kind
        assert "re-scope" in guidance, kind


def test_guidance_says_why_the_reply_is_not_enough():
    # The reasoning is the load-bearing part: an EM who does not understand
    # WHY declining the edit loses the finding will decline it again.
    guidance = _by_value("fyi")["fold-into-plan"]["guidance"]
    assert "chunk" in guidance

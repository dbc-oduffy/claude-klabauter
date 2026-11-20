"""Pins the closed landing refusal-kind table through its public functions."""

import pytest

from coordinator_core.ops.dispatch_emit import landing_refusal_kinds as k


def test_refusal_kinds_are_exactly_the_irreversible_three():
    assert k.LANDING_REFUSAL_KINDS == frozenset({"destructive-git", "secret-in-diff", "lost-definition"})


def test_collapsed_leftover_reasons_are_provenance_never_refusals():
    for reason in ("run-touched-left", "review-edit-unlanded", "unreviewed", "xs-over-budget"):
        assert reason in k.PROVENANCE_KINDS
        assert k.is_refusal(reason) is False


def test_collapsed_reasons_are_no_engine_bug_in_the_registry():
    from coordinator_core.ops.dispatch_emit.land_refusal_registry import LAND_REFUSAL_REGISTRY

    for reason in ("run-touched-left", "review-edit-unlanded", "unreviewed", "xs-over-budget"):
        entry = LAND_REFUSAL_REGISTRY[reason]
        assert not entry.engine_bug, reason
    assert LAND_REFUSAL_REGISTRY["xs-over-budget"].denies is False


@pytest.mark.parametrize("reason", sorted(k.LANDING_REFUSAL_KINDS))
def test_refusal_kinds_refuse(reason):
    assert k.is_refusal(reason) is True


@pytest.mark.parametrize("reason", sorted(k.PROVENANCE_KINDS) + ["", "unknown-reason"])
def test_every_other_reason_is_not_a_refusal(reason):
    assert k.is_refusal(reason) is False


def test_named_demotions_are_provenance():
    assert {"peer-hunks-co-declared", "targeted-tests-failed"} <= k.PROVENANCE_KINDS


def test_tables_are_disjoint():
    assert not (k.LANDING_REFUSAL_KINDS & k.PROVENANCE_KINDS)

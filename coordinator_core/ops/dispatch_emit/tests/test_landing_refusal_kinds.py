"""Pins the closed landing refusal-kind table through its public functions."""

import pytest

from coordinator_core.ops.dispatch_emit import landing_refusal_kinds as k


def test_refusal_kinds_are_exactly_the_two():
    assert k.LANDING_REFUSAL_KINDS == frozenset({"destructive-git", "secret-in-diff"})


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

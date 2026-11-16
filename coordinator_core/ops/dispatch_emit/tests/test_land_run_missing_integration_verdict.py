"""``_reviewed_files``: what an absent integration verdict does and does not credit.

An absent integration verdict (key missing or null) is not a pass, even beside a PASS delivery
verdict; neither are a missing or non-PASS delivery verdict or an explicit integration finding.
"""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run

_FILES = ["a.py", "b.py"]


def _review(**legs) -> dict:
    return {"prep": {"slice_files": list(_FILES)}, **legs}


@pytest.mark.parametrize("need_pass", [True, False])
def test_absent_integration_without_any_delivery_verdict_credits_nothing(need_pass):
    assert land_run._reviewed_files(_review(), need_pass=need_pass) is None


@pytest.mark.parametrize("verdict", ["FAIL", "WARN", None, ""])
@pytest.mark.parametrize("need_pass", [True, False])
def test_absent_integration_with_a_non_pass_delivery_credits_nothing(verdict, need_pass):
    review = _review(delivery={"verdict": verdict})
    assert land_run._reviewed_files(review, need_pass=need_pass) is None


@pytest.mark.parametrize("need_pass", [True, False])
def test_missing_integration_key_with_a_pass_delivery_credits_nothing(need_pass):
    review = _review(delivery={"verdict": "PASS"})
    assert "integration" not in review
    assert land_run._reviewed_files(review, need_pass=need_pass) is None


@pytest.mark.parametrize("need_pass", [True, False])
def test_null_integration_with_a_pass_delivery_credits_nothing(need_pass):
    review = _review(delivery={"verdict": "PASS"}, integration=None)
    assert land_run._reviewed_files(review, need_pass=need_pass) is None


def test_a_clean_integration_with_a_pass_delivery_credits_the_slice():
    review = _review(delivery={"verdict": "PASS"}, integration={"unresolved": [], "confinement_violations": 0})
    assert land_run._reviewed_files(review, need_pass=True) == set(_FILES)
    assert land_run._reviewed_files(review, need_pass=False) == set(_FILES)


@pytest.mark.parametrize(
    "integration",
    [{"unresolved": ["open finding"]}, {"confinement_violations": ["x.py"]}],
)
def test_an_explicit_integration_finding_bars_credit_even_with_a_pass_delivery(integration):
    review = _review(delivery={"verdict": "PASS"}, integration=integration)
    assert land_run._reviewed_files(review, need_pass=True) is None
    assert land_run._reviewed_files(review, need_pass=False) is None


def test_a_clean_integration_leg_still_credits_without_a_delivery_verdict_when_pass_not_needed():
    review = _review(integration={"unresolved": [], "confinement_violations": []})
    assert land_run._reviewed_files(review, need_pass=False) == set(_FILES)
    assert land_run._reviewed_files(review, need_pass=True) is None

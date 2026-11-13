from __future__ import annotations

import json

import pytest

from coordinator_core.contract import run_budget as rb
from coordinator_core.contract.run_budget import RunBudget, UnitCost


def test_vendored_file_loads_for_both_ceremonies():
    for ceremony in ("distill", "learn-lessons"):
        b = rb.load(ceremony)
        assert b == RunBudget(agents=10, tokens=200000, minutes=15)
        assert b.source == "vendored"
    assert rb.whole_backlog_bar() == 1_000_000


def test_unlisted_ceremony_raises():
    with pytest.raises(KeyError):
        rb.load("update-docs")


@pytest.mark.parametrize(
    "content",
    [None, "not json", "{}", json.dumps({"slice": {"agents": 0, "tokens": 1, "minutes": 1}}),
     json.dumps({"slice": {"agents": True, "tokens": 1, "minutes": 1}})],
)
def test_missing_or_malformed_falls_back_to_pinned_default(tmp_path, content):
    p = tmp_path / "b.json"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    b = rb.load("distill", path=p)
    assert b == rb.PINNED_DEFAULT
    assert b.source == "pinned-default"
    assert rb.whole_backlog_bar(path=p) == 1_000_000


def test_custom_slice_is_read(tmp_path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"slice": {"agents": 4, "tokens": 50, "minutes": 2},
                             "whole_backlog_token_bar": 99}), encoding="utf-8")
    assert rb.load("learn-lessons", path=p) == RunBudget(4, 50, 2)
    assert rb.whole_backlog_bar(path=p) == 99


def test_vendored_file_is_valid_json_with_contract_name():
    data = json.loads(rb.VENDORED_PATH.read_text(encoding="utf-8"))
    assert data["contract"] == "ceremony-budget"


B = RunBudget(10, 200_000, 15)


def test_fit_units_binds_on_the_tightest_axis():
    fixed = UnitCost(2, 20_000, 1)
    per = UnitCost(1, 30_000, 1)
    # agents: 8, tokens: 6, minutes: 14 -> tokens binds
    assert rb.fit_units(B, fixed, per, wanted=50) == 6


def test_fit_units_never_exceeds_wanted():
    assert rb.fit_units(B, UnitCost(), UnitCost(1, 1, 1), wanted=3) == 3


def test_fit_units_zero_per_unit_axis_does_not_bind():
    assert rb.fit_units(B, UnitCost(), UnitCost(agents=2), wanted=100) == 5


def test_fit_units_minimum_one_when_fixed_over_cap():
    fixed = UnitCost(agents=11)
    assert rb.over_cap_fixed(B, fixed) is True
    assert rb.fit_units(B, fixed, UnitCost(1, 1, 1), wanted=5) == 1


def test_fit_units_zero_wanted_is_zero():
    assert rb.fit_units(B, UnitCost(), UnitCost(1, 1, 1), wanted=0) == 0


def test_fit_result_stays_within_cap_when_fixed_fits():
    fixed = UnitCost(3, 10_000, 2)
    per = UnitCost(1, 17_000, 2)
    n = rb.fit_units(B, fixed, per, wanted=1000)
    assert fixed.agents + n * per.agents <= B.agents
    assert fixed.tokens + n * per.tokens <= B.tokens
    assert fixed.minutes + n * per.minutes <= B.minutes
    n1 = n + 1
    assert (fixed.agents + n1 * per.agents > B.agents
            or fixed.tokens + n1 * per.tokens > B.tokens
            or fixed.minutes + n1 * per.minutes > B.minutes)


def test_budget_report_shape():
    r = rb.budget_report(B, UnitCost(9, 200_001, 15), over_cap_fixed_flag=False)
    assert r["cap"] == {"agents": 10, "tokens": 200000, "minutes": 15}
    assert r["within"] == {"agents": True, "tokens": False, "minutes": True}
    assert r["over_cap_fixed"] is False
    assert "over_cap_fixed" not in rb.budget_report(B, UnitCost())

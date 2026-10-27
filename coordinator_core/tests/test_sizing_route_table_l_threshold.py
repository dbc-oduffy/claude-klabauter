"""Pins the L plan-threshold raise (research row 'Plan threshold (principle)').

Reverted: L routes plan again (#77); spec-dispatch at L needs the mechanically-uniform
flag.
"""

from coordinator_core import sizing_assemble as sa
from coordinator_core.sizing_route_table import _BASE_ROUTE_BY_TSHIRT

L_ROUTE = "plan"


def test_plan_threshold_principle_row_raised_at_l_only():
    assert _BASE_ROUTE_BY_TSHIRT["L"] == L_ROUTE
    assert _BASE_ROUTE_BY_TSHIRT["S"] == "spec-dispatch"
    assert _BASE_ROUTE_BY_TSHIRT["M"] == "plan"
    assert _BASE_ROUTE_BY_TSHIRT["XL"] == "pm-decision"
    assert _BASE_ROUTE_BY_TSHIRT["XXL"] == "goal-setting"


def test_plan_threshold_principle_row_production_route_l_and_xl():
    assert sa.route(estimate={"tshirt": "L"})["route"] == L_ROUTE
    assert sa.route(estimate={"tshirt": "XL"})["route"] == "pm-decision"

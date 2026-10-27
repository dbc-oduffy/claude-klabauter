"""sizing_fire's size-to-arm mapping derives from sizing_route_table, so the two cannot drift."""

import pytest

from coordinator_core.ops.dispatch_emit import sizing_fire as sf
from coordinator_core.sizing_route_table import _BASE_ROUTE_BY_TSHIRT


def _sizing(tshirt, route):
    return {
        "estimate": {"tshirt": tshirt},
        "route": route,
        "status": "routed",
        "interaction_mode": "pm",
        "intent": "ask",
        "premise": {"evidence": "looked", "provenance": "verified"},
        "exit_criterion": {"statement": "done", "accepted": {"by": "pm"}},
    }


def _route_refusals(sizing):
    arm = sf.resolve_arm(sizing)
    out = sf.collect_fire_refusals(sizing, sizing_rel="state/sizings/x.yaml", arm=arm, writes=["a.py"])
    return [m for m in out if m.startswith("`route` is")]


def test_l_sizing_routed_spec_dispatch_passes_route_validation():
    assert _route_refusals(_sizing("L", "spec-dispatch")) == []


def test_l_sizing_routed_plan_fires_m_plus_without_a_route_refusal():
    sizing = _sizing("L", "plan")
    assert sf.resolve_arm(sizing) == sf.ARM_M_PLUS
    assert _route_refusals(sizing) == []


def test_s_sizing_routed_plan_is_still_refused():
    assert _route_refusals(_sizing("S", "plan")) != []


def test_every_size_with_a_dispatch_arm_resolves_to_an_arm_whose_route_equals_its_table_route():
    for tshirt, route in _BASE_ROUTE_BY_TSHIRT.items():
        arm = sf._TSHIRT_ARM[tshirt]
        assert sf.ARM_ROUTE[arm] == route or route in ("pm-decision", "goal-setting"), tshirt


def test_arms_per_size():
    got = {t: sf._TSHIRT_ARM[t] for t in _BASE_ROUTE_BY_TSHIRT}
    assert got == {
        "XS": sf.ARM_XS,
        "S": sf.ARM_S,
        "M": sf.ARM_M_PLUS,
        "L": sf.ARM_S,
        "XL": sf.ARM_M_PLUS,
        "XXL": sf.ARM_M_PLUS,
    }

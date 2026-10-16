"""ask_compose: the gate reply's design_route rides every _gate carrier and both terminal returns."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.design_route import DESIGN_ROUTE_KEY
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

_CARRY = f"{DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null"


def test_gate_schema_carries_the_key():
    assert ask_compose._GATE_SCHEMA["properties"][DESIGN_ROUTE_KEY] == {"type": ["object", "null"]}


def test_pre_sized_script_names_the_key_on_prompts_and_both_returns():
    script = _compose(stop_after="plan")
    assert script.count("design_brief and design_route when present") >= 1
    assert script.count(_CARRY) == 3
    full = _compose()
    assert full.count("design_brief and design_route when present") >= 1
    assert f"return {{ arm: _gate.arm, {_CARRY}" in full


def test_in_run_sized_script_carries_reword_and_scout_hops():
    script = _compose(prompt="do a thing", sizing_rel=None)
    assert f"{DESIGN_ROUTE_KEY}: _reworded.{DESIGN_ROUTE_KEY} ?? null" in script
    assert f"{DESIGN_ROUTE_KEY}: _scouted.{DESIGN_ROUTE_KEY} ?? null" in script
    assert "design_brief and design_route" in script

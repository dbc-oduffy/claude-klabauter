"""A pure research sizing (research block, no writes) chains into the research run in-run; the handoff is not a failure."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_RESEARCH
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_START = "await workflow({ scriptPath: _chainPath })"
_GUARD_HEAD = f"_halted && _halted.halted === '{HALT_RESEARCH}' && !_halted.split"


def _script(sizing_rel, **over) -> str:
    kw = dict(
        repo_root="REPO", prompt=None if sizing_rel else "survey four repos and rank them", sizing_rel=sizing_rel,
        run_id="run-pure-1", session_id=None, wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub", in_run_commit=True, **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _leg(script: str) -> str:
    start = script.index(_GUARD_HEAD)
    start = script.rindex("\n", 0, start) + 1
    end = script.index("chain_error", start)
    return script[start : script.index("\n  }", end) + 4]


@pytest.fixture(params=[None, "state/sizings/x.yaml"], ids=["sized-in-run", "pre-sized"])
def script(request) -> str:
    return _script(request.param)


def test_pure_research_halt_chains_its_own_sizing_through_the_research_route(script):
    leg = _leg(script)
    assert "from_sizing: _sizingRel" in leg
    assert "sizing_path" not in leg and "ask: true" not in leg
    assert leg.count(_START) == 1
    assert "chained_from: _runId" in leg


def test_guard_requires_a_nonempty_fire_and_excludes_a_split(script):
    guard = _leg(script).split("\n", 1)[0]
    assert "!_halted.split" in guard
    assert "Array.isArray(_halted.fire) && _halted.fire.length > 0" in guard
    assert " || " not in guard


def test_chain_failure_keeps_the_fire_and_adds_chain_error(script):
    leg = _leg(script)
    fail = leg[leg.index("_halted = {") :]
    assert fail.startswith("_halted = { ..._halted, chain_error: ")
    assert "fire:" not in fail and "halted:" not in fail.split("chain_error")[0]


def test_leg_runs_after_the_split_leg_and_before_the_terminal_return(script):
    split = script.index(f"_halted.halted === '{HALT_RESEARCH}' && _halted.split")
    pure = script.index(_GUARD_HEAD)
    assert script.index("if (_gate.halt || !_gate.arm)") < split < pure < script.rindex("  return { arm:")


def test_a_launcher_child_hands_the_run_off_and_holds_no_start():
    child = _script("state/sizings/x.yaml", launcher_child=True)
    start = child.index(_GUARD_HEAD)
    line = child[child.rindex("\n", 0, start) + 1 : child.index("\n", start)]
    assert f"return {{ {CHAIN_HANDOFF_KEY}: {{ from_sizing: _sizingRel" in line
    assert "sizing_path" not in line and "ask: true" not in line
    assert "workflow(" not in child


def test_research_handoff_next_action_is_not_a_failure_kind(script):
    assert "kind: 'fire_research'" in script
    handoff = script.index("kind: 'fire_research'")
    revise = script.index("kind: 'revise_and_refire'")
    window = script[script.rindex("_nextAction =", 0, handoff) : handoff]
    assert f"_halted?.halted === '{HALT_RESEARCH}'" in window
    assert revise > handoff  # the failure kind is the fallthrough branch of the same expression
    assert f"if ((_halted?.halted === '{HALT_RESEARCH}' && Array.isArray(_halted.fire) && _halted.fire.length > 0)) {{ _reviseAndRefire = null; }}" in script


def test_a_genuine_halt_still_selects_revise_and_refire(script):
    na = script[script.index("  _nextAction = "):]
    na = na[: na.index(";\n")]
    assert "kind: 'revise_and_refire'" in na

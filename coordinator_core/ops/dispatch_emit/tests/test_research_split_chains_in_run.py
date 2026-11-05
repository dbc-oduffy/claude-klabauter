"""A ready research/build split's research sibling is chained in-run; a no-split or failed-chain research halt keeps the EM wake."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_RESEARCH
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import repo  # noqa: F401

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_SIBLING = "_halted.split.research.sizing"
_START = "await workflow({ scriptPath: _chainPath })"


def _script(sizing_rel, **over) -> str:
    kw = dict(
        repo_root="REPO", prompt=None if sizing_rel else "survey four repos and rank them", sizing_rel=sizing_rel,
        run_id="run-split-1",
        session_id=None, wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        in_run_commit=True, **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _leg(script: str) -> str:
    """The research-split leg: from its guard line to the closing brace that follows the chain_error assignment."""
    start = script.index(f"_halted.halted === '{HALT_RESEARCH}' && _halted.split")
    start = script.rindex("\n", 0, start) + 1
    end = script.index("chain_error", start)
    end = script.index("\n  }", end) + 4
    return script[start:end]


def _leg_line(script: str) -> str:
    start = script.index(f"_halted.halted === '{HALT_RESEARCH}' && _halted.split")
    start = script.rindex("\n", 0, start) + 1
    return script[start : script.index("\n", start)]


@pytest.fixture(params=[None, "state/sizings/x.yaml"], ids=["sized-in-run", "pre-sized"])
def script(request) -> str:
    return _script(request.param)


def test_leg_chains_the_split_research_sizing_and_carries_the_split(script):
    leg = _leg(script)
    assert f"from_sizing: {_SIBLING}" in leg
    assert "sizing_path" not in leg and "ask: true" not in leg
    assert _START in leg
    assert "chained_from: _runId, split: _halted.split" in leg


def test_guard_requires_a_ready_split_a_sizing_and_a_nonempty_fire(script):
    guard = _leg(script).split("\n", 1)[0]
    assert f"_halted.halted === '{HALT_RESEARCH}'" in guard
    assert "_halted.split && _halted.split.research" in guard
    assert "_halted.split.research.status === 'ready'" in guard
    assert f"typeof {_SIBLING} === 'string'" in guard
    assert "Array.isArray(_halted.fire) && _halted.fire.length > 0" in guard


def test_a_no_split_research_halt_does_not_satisfy_the_guard(script):
    guard = _leg(script).split("\n", 1)[0]
    assert "_halted.split &&" in guard and "&& _halted.split.research" in guard
    # Every conjunct is anded; none is an `||` that a bare research halt could satisfy.
    assert " || " not in guard


def test_failure_keeps_the_research_halt_and_its_fire(script):
    leg = _leg(script)
    fail = leg[leg.index("_halted = {") :]
    assert re.match(r"_halted = \{ \.\.\._halted, chain_error: ", fail)
    assert "halted:" not in fail.split("chain_error")[0]
    assert "fire:" not in fail
    assert "fire it from halted_by.fire" in fail


def test_leg_sits_after_the_gate_halt_assignment_and_before_the_terminal_return(script):
    gate = script.index("if (_gate.halt || !_gate.arm)")
    leg = script.index(f"_halted.halted === '{HALT_RESEARCH}' && _halted.split")
    ret = script.rindex("  return { arm:")
    assert gate < leg < ret


def test_the_build_sibling_is_never_chained(script):
    leg = _leg(script)
    assert "split.build" not in leg
    assert leg.count(_START) == 1


def test_a_launcher_child_hands_the_research_sibling_off_and_holds_no_start():
    child = _script("state/sizings/x.yaml", launcher_child=True)
    leg = _leg_line(child)
    assert f"return {{ {CHAIN_HANDOFF_KEY}: {{ from_sizing: {_SIBLING}" in leg
    assert "sizing_path" not in leg and "ask: true" not in leg
    assert f"sizing: {_SIBLING}, split: _halted.split, run_id: _runId" in leg
    assert "workflow(" not in child
    assert "_halted.fire.length > 0" in leg and "status === 'ready'" in leg


def test_leg_params_reach_the_research_route_and_the_ask_door_is_refused(repo):
    from coordinator_core.ops.dispatch_emit.ask_compose import _chain_emit_params_js
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
    from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing

    rel = _write_sizing(repo, "--research-class", "corpus", "--research-source", "web")
    rendered = _chain_emit_params_js("SIB", research=True)
    keys = set(re.findall(r"(?:^|[{,]\s*)(\w+):", rendered))
    assert keys == {"from_sizing", "session_id", "target_root", "cross_repo_approved"}
    reply = _dispatch_emit({"from_sizing": rel, "session_id": "sess", "target_root": str(repo)}, repo)
    assert reply["path"] and "research" in reply["run_id"]
    with pytest.raises(Exception):
        _dispatch_emit({"ask": True, "sizing_path": rel, "from_sizing": rel, "session_id": "sess"}, repo)

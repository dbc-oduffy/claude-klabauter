"""compose_ask_script: a re-gate pull on the same blocking set halts without a refire; other cases keep the old halt.

The halt fires only when planBlitz copies the op reply's blocking into pulled[]; that
forwarding lives in the plan-blitz source, outside this suite.
"""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_RESEARCH
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

pytestmark = pytest.mark.usefixtures("m_plus_known_arm")


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _halt_clause(script: str) -> str:
    start = script.index("if (_replanCapped && !_pullSplit && !_halted.next_action)")
    return script[start : script.index("\n", start)]


def test_repeat_halt_is_a_research_halt_carrying_the_findings():
    script = _script()
    clause = _halt_clause(script)
    assert f"halted: '{HALT_RESEARCH}'" in clause
    assert "pulled twice on the same blocking set" not in script
    assert "blocking: _pullFindings" in clause
    assert "fire: [_researchFire()]" in clause
    assert "plan: _pullPlan, rulings: _pullRulings" in clause
    assert "if (_repeatBlocking || _pullAttempts() >= " in script


def test_old_halt_string_and_emission_unchanged():
    script = _script()
    assert "('plan-blitz reported no ready plan' + ((_blitz?.regate ?? _blitz)?.pulled ?? [])" in script
    assert ", blitz: _blitz, sizing: _sizingRel, run_id: _runId };" in script
    assert script.index("plan-blitz reported no ready plan") < script.index("if (_replanCapped && !_pullSplit && !_halted.next_action)")

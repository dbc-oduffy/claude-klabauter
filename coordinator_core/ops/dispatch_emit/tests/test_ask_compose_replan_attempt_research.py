"""compose_ask_script: below REPLAN_ATTEMPT_CAP a pull re-plans or patches; at the cap it dispatches research."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import REPLAN_ATTEMPT_CAP, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_RESEARCH
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


@pytest.fixture(autouse=True)
def _m_plus_arm(m_plus_known_arm):
    """Only a known M+ arm embeds plan-blitz, so every test here composes as an M+ re-fire."""


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1",
        session_id=None, wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )


def _pulled_block() -> str:
    script = _script()
    return script[script.index("_pulledEntry = ") : script.index("if (!_planRel) { _halted")]


def test_cap_is_one_named_constant_defaulting_to_two():
    assert REPLAN_ATTEMPT_CAP == 2
    assert f"_replanCapped = _pullAttempts() >= {REPLAN_ATTEMPT_CAP};" in _pulled_block()


def test_below_the_cap_unsaveable_revises_and_other_pulls_patch():
    block = _pulled_block()
    gate = block.index("if (!_replanCapped) {")
    revise = block.index("if (_unsaveable) { _revised = await")
    patch = block.index("else { _revised = await")
    assert gate < revise < patch
    assert block.count("label: 'plan-revise'") == 1
    assert block.count("label: 'plan-patch'") == 1


def test_at_the_cap_no_further_reblitz_and_research_halt_carries_the_findings():
    script = _script()
    block = _pulled_block()
    assert "if (!_planRel && !_pullSplit && !_replanCapped) {" in block
    assert block.index("if (!_replanCapped) {") < block.index("await planBlitz(")
    start = script.index("if (_replanCapped && !_pullSplit && !_halted.next_action)")
    clause = script[start : script.index("\n", start)]
    assert f"halted: '{HALT_RESEARCH}'" in clause
    assert "fire: [_researchFire()]" in clause
    assert "blocking: _pullFindings" in clause
    fire = script[script.index("const _researchFire"): start]
    assert "JSON.stringify(_pullFindings ?? [])" in fire


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "regate,pulled,legs,capped",
    [
        ([], [{"planPath": "p"}], 0, False),
        ([{"planPath": "p"}], [{"planPath": "p"}], 0, True),
        ([], [{"planPath": "p"}], 1, True),
        ([{"planPath": "other"}], [{"planPath": "p"}], 0, False),
    ],
)
def test_attempt_count_runs_over_the_runs_own_record(regate, pulled, legs, capped):
    block = _pulled_block()
    start = block.index("const _pullAttempts")
    decl = block[start : block.index(";\n", start) + 1] if ";\n" in block[start:] else block[start : block.index("_replanCapped =", start)]
    js = (
        f"const _blitz = {json.dumps({'regate': {'pulled': regate}, 'pulled': pulled})};"
        f"const _pulledEntry = {{ planPath: 'p' }}; let _replanLegs = {legs};"
        f"{decl}\nconsole.log(JSON.stringify(_pullAttempts() >= {REPLAN_ATTEMPT_CAP}));"
    )
    out = subprocess.run(
        ["node", "-"], input=js, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    assert json.loads(out) is capped

"""A research.close reply halted criterion_not_met returns as halted_by, and the pure-research leg chains only an advancing refire."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import CLOSE_REFIRE_ADVANCES_JS, compose_ask_script
from coordinator_core.ops.dispatch_emit.pipeline_compose import _CLOSE_HALT_JS, compose_chain_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import (
    GATE_SCHEMA,
    _segment,
    _stage,
)

KW = {"run_id": "r1", "agent_type_host": None}
CLOSE = {"op": "research.close", "params": {"run_id": "r1"}}
HALT = {
    "committed": True, "halted": "criterion_not_met", "open_rows": ["T18"],
    "next_action": {"op": "engine-ask refire", "fire": "fire-it", "next_stage": "synthesis"},
}
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _node(js: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _close_halt(closed):
    return _node(f"const closed = {json.dumps(closed)};\n{_CLOSE_HALT_JS}\nconsole.log(JSON.stringify(closeHalt));")


def test_both_close_tails_spread_the_close_halt_into_the_return(tmp_path: Path) -> None:
    scoped = compose_chain_script(
        [_segment(tmp_path, "pre", [_stage("gate", halts_unless="ok", schema=GATE_SCHEMA)]),
         _segment(tmp_path, "main", [_stage("work")])],
        next_action=CLOSE, **KW,
    )
    plain = compose_chain_script([_segment(tmp_path, "main", [_stage("work")])], next_action=CLOSE, **KW)
    for script in (scoped, plain):
        assert script.count(_CLOSE_HALT_JS) == 1
        assert script.index(_CLOSE_HALT_JS) < script.index("...closeHalt")
    assert "closeHalt" not in compose_chain_script(
        [_segment(tmp_path, "main", [_stage("work")])], next_action={"op": "other.op", "params": {}}, **KW
    )


def test_a_criterion_not_met_close_returns_halted_by_with_its_refire() -> None:
    halted = _close_halt(HALT)
    assert halted == {"halted_by": HALT}
    assert halted["halted_by"]["next_action"]["fire"] == "fire-it"


def test_a_met_or_absent_close_halt_adds_nothing() -> None:
    assert _close_halt({"committed": True}) == {}
    assert _close_halt({"committed": True, "halted": "other"}) == {}
    assert _close_halt(None) == {}


def _advances(halted) -> bool:
    return _node(
        f"const _halted = {json.dumps(halted)};\nconsole.log(JSON.stringify({CLOSE_REFIRE_ADVANCES_JS}));"
    )


def test_an_advancing_refire_chains_and_a_same_stage_refire_does_not() -> None:
    advancing = {"halted": "criterion_not_met", "closed_stage": "questions", "next_action": HALT["next_action"]}
    assert _advances(advancing) is True
    assert _advances({**advancing, "closed_stage": "synthesis"}) is False
    assert _advances({**advancing, "next_action": {"op": "x", "fire": "f"}}) is False
    assert _advances({**advancing, "next_action": {**HALT["next_action"], "fire": ""}}) is False
    assert _advances({**advancing, "next_action": "refire it"}) is False
    assert _advances({**advancing, "halted": "research"}) is False


def _ask(**over) -> str:
    kw = dict(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-pure-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", in_run_commit=True, **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def test_the_pure_research_leg_carries_a_second_guarded_chain_for_the_close_halt() -> None:
    script = _ask()
    start = script.index(CLOSE_REFIRE_ADVANCES_JS)
    leg = script[script.rindex("\n", 0, start) + 1 : script.index("chain_error", start)]
    assert "_chain_emit(_sizingRel)" in leg
    assert "_halted?.halted === 'research'" not in leg
    assert script.count(CLOSE_REFIRE_ADVANCES_JS) == 1


def test_a_launcher_child_hands_the_close_halt_refire_off_and_a_final_run_does_not_chain() -> None:
    child = _ask(launcher_child=True)
    assert CLOSE_REFIRE_ADVANCES_JS in child
    final = _ask(chain_final=True)
    assert "a chained run cannot chain the next stage of the research run again" in final

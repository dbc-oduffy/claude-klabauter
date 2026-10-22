"""ask_compose: the gate reply's design_route rides every _gate carrier and both terminal returns."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, design_route
from coordinator_core.ops.dispatch_emit.design_route import DESIGN_ROUTE_KEY
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

_CARRY = f"{DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null"


def test_gate_schema_carries_the_key():
    assert ask_compose._GATE_SCHEMA["properties"][DESIGN_ROUTE_KEY] == {"type": ["object", "null"]}


def test_pre_sized_script_names_the_key_on_prompts_and_both_returns():
    script = _compose(stop_after="plan")
    assert script.count("design_brief and design_route when present") >= 1
    # halted_by return + XS dispatch return; the parked-plan arm halts, it has no separate planned return.
    assert script.count(_CARRY) == 2
    full = _compose()
    assert full.count("design_brief and design_route when present") >= 1
    assert f"return {{ arm: _gate.arm, {_CARRY}" in full


_HALT_HEAD = "if (_manifest.error && /^(?:stage refused: )?design-review:/.test(String(_manifest.error))) { _halted = { "


def test_stage_refusal_maps_to_a_design_review_halt_ahead_of_the_refusal_halt():
    script = _compose(prompt=None, sizing_rel="state/sizings/s.yaml")
    at = script.index(_HALT_HEAD)
    halt = script[at : script.index("}; }", at)]
    assert "halted: 'design-review', kind: 'design-review'" in halt
    assert "board_url: _gate?.design_route?.board?.url ?? null" in halt
    assert "design_route: _gate?.design_route ?? null" in halt
    assert ask_compose._lit(design_route.next_action("state/sizings/s.yaml")) in halt
    assert at < script.index("else if (!_parked && (_manifest.error || (!(_manifest.rows ?? []).length")


def test_manifest_error_quoting_the_prefix_mid_text_is_not_a_design_review_halt():
    script = _compose(prompt=None, sizing_rel="state/sizings/s.yaml")
    assert "/^(?:stage refused: )?design-review:/.test(String(_manifest.error))" in script
    assert "String(_manifest.error).includes(\"design-review:\")" not in script


@pytest.mark.spawns_process
def test_design_review_check_matches_the_stage_refused_spelling_only_at_the_start():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = _compose(prompt=None, sizing_rel="state/sizings/s.yaml")
    head = "/^(?:stage refused: )?design-review:/"
    at = script.index(head)
    regex = script[at : script.index("/", at + 1) + 1]
    probe = "const r = %s; console.log(JSON.stringify(['design-review: x', 'stage refused: design-review: x', 'no manifest: design-review: x', 'stage refused: other'].map((e) => r.test(e))));" % regex
    proc = subprocess.run([node, "-"], input=probe, capture_output=True, text=True, timeout=60, **no_console_creationflags())
    assert json.loads(proc.stdout) == [True, True, False, False]


def test_any_ready_op_error_but_the_did_not_run_sentinel_becomes_a_stage_refusal():
    script = _compose(prompt=None, sizing_rel="state/sizings/s.yaml")
    assert "_adoptReadyError(_ready)" in script and "_adoptReadyError(_reready)" in script
    assert "_re !== '' && !/did not run/.test(_re)) _stageRefusal = _re;" in script


def test_ready_error_adoption_never_overrides_a_staged_manifest_or_prior_refusal():
    script = _compose(prompt=None, sizing_rel="state/sizings/s.yaml")
    assert "if (!_stagedManifest && !_stageRefusal && _re !== ''" in script


def test_in_run_next_action_is_built_from_sizing_rel_and_agrees_with_the_python_text():
    script = _compose(prompt="do a thing", sizing_rel=None)
    expr = ask_compose._design_next_action_js(None)
    marker = "\u0000"
    head, tail = design_route.next_action(marker).split(marker)
    assert expr == f"({ask_compose._lit(head)} + (_sizingRel ?? '') + {ask_compose._lit(tail)})"
    assert f"next_action: {expr}" in script
    assert head + "state/sizings/s.yaml" + tail == design_route.next_action("state/sizings/s.yaml")


def test_halt_kind_is_not_touchpoint_or_refusal():
    assert ask_compose.HALT_DESIGN_REVIEW == "design-review"
    assert ask_compose.HALT_DESIGN_REVIEW not in (ask_compose.HALT_TOUCHPOINT, ask_compose.HALT_REFUSAL)


@pytest.mark.spawns_process
@pytest.mark.parametrize("over", [{"prompt": None, "sizing_rel": "state/sizings/s.yaml"}, {"prompt": "do a thing", "sizing_rel": None}])
def test_script_with_the_design_review_halt_parses_through_asyncfunction(over, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "s.js"
    path.write_text(_compose(**over), encoding="utf-8")
    program = (
        "const src = require('fs').readFileSync(process.argv[2], 'utf8')"
        ".replace(/^export const meta/m, 'const meta');"
        "new (Object.getPrototypeOf(async function () {}).constructor)("
        "'args', 'agent', 'parallel', 'pipeline', 'phase', src);"
    )
    out = subprocess.run([node, "-", str(path)], input=program, capture_output=True, text=True, **no_console_creationflags())
    assert out.returncode == 0, out.stderr


def test_in_run_sized_script_carries_reword_and_scout_hops():
    script = _compose(prompt="do a thing", sizing_rel=None)
    assert f"{DESIGN_ROUTE_KEY}: _reworded.{DESIGN_ROUTE_KEY} ?? null" in script
    assert f"{DESIGN_ROUTE_KEY}: _scouted.{DESIGN_ROUTE_KEY} ?? null" in script
    assert "design_brief and design_route" in script

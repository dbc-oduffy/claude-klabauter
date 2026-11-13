"""No composed script dispatches a per-row or per-wave verify test-runner leg where a terminal test leg runs."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_wave_barrier import _script as _ask_script
from coordinator_core.ops.dispatch_emit.tests.test_grind_pm_adjudication import _script as _grind_script
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW

_FLAG = "_terminalTestsWanted"
_EMIT_PUSH = "label: 'verify:' + id"
_TERMINAL_LABELS = ("test:terminal", "verify-tests", "verify:wave-")


def _wave_gate_body(script: str) -> str:
    start = script.index("async function _waveTestsGreen(")
    return script[start: script.index("\n  }\n", start)]


def _emit_script(tmp_path, *, with_test: bool) -> str:
    target = tmp_path / "pkg" / "c1.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    if with_test:
        test_target = tmp_path / "pkg" / "tests" / "test_c1.py"
        test_target.parent.mkdir(parents=True)
        test_target.write_text("def test_x(): pass\n", encoding="utf-8")
    row = WaveRow(id="C1", title="t", surface="pkg", writes=["pkg/c1.py"], reads=[], depends_on=[])
    return compose_script([[row]], name="wf", description="d", repo_root=tmp_path, **REVIEW_KW)


def test_ask_wave_gate_returns_before_its_agent_call_when_the_terminal_tests_run():
    body = _wave_gate_body(_ask_script())

    assert f"if ({_FLAG}) return true;" in body
    assert body.index(f"if ({_FLAG}) return true;") < body.index("agent(")


def test_ask_terminal_tests_flag_is_a_top_level_binding_the_wave_gate_can_read():
    script = _ask_script()
    decl = f"\n  let {_FLAG} = false;"

    assert decl in script
    assert script.index(decl) < script.index("async function _waveTestsGreen(")
    assert f"const {_FLAG}" not in script
    assert re.search(rf"const _rowVerifyWanted = [^;]*{_FLAG}", script)


def test_ask_flag_stays_false_on_the_xs_arm_and_on_empty_test_targets_so_the_wave_leg_still_dispatches():
    script = _ask_script()
    assign = re.search(rf"[\n;{{]\s*{_FLAG} = ([^;]*);", script)

    assert assign, "the flag is never assigned"
    assert "_gate.arm !== 'xs'" in assign.group(1)
    assert "!_xsTail" in assign.group(1)
    assert "(_manifest.test_targets ?? []).length" in assign.group(1)

    body = _wave_gate_body(script)
    assert "label: 'verify:wave-' + n" in body
    assert body.index("if (!scope.length) return true;") < body.index("agent(")


def test_emit_composes_the_per_row_verify_push_only_when_no_terminal_test_leg_composes(tmp_path):
    with_terminal = _emit_script(tmp_path / "a", with_test=True)
    without_terminal = _emit_script(tmp_path / "b", with_test=False)

    assert "test:terminal" in with_terminal
    assert _EMIT_PUSH not in with_terminal
    assert "test:terminal" not in without_terminal
    assert _EMIT_PUSH in without_terminal


def test_grind_has_no_terminal_test_leg_so_its_verify_node_is_not_redundant():
    script = _grind_script()

    assert "label: 'verify-op'" in script
    assert "label: 'verify-agent'" in script
    for label in _TERMINAL_LABELS:
        assert label not in script, label
